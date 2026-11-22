"""runcomposer.sdk — THE connector surface (DESIGN.md §1).

Everything a connector package or an embedding script may import from
runcomposer is one package: ``runcomposer.sdk`` and its two toolkits,
``runcomposer.sdk.robot`` and ``runcomposer.sdk.jenkins``. These tests pin
that surface: what is in it, that
it is cheap to import in a worker process, that the reference plugins really
have the members the protocols name, and that a whole run can be driven with
nothing but SDK imports.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from importlib.metadata import entry_points

import pytest

import runcomposer.sdk as sdk

from .conftest import write_config

#: The public names, frozen. Removing one breaks every connector that uses it
#: and adding one is a promise to keep it — either way the change belongs in
#: this list and in CHANGELOG.md, deliberately.
PUBLIC_NAMES = {
    # protocols, and what runcomposer hands to a runner
    "TestSource", "Runner", "ResultParser", "RunStore", "RunObserver", "Configurable",
    "DispatchContext", "PlanContext", "LiveChannel", "History", "contract_members",
    # base classes: the defaults for every optional member
    "PluginBase", "SourceBase", "RunnerBase", "ParserBase", "StoreBase", "ObserverBase",
    # records
    "Item", "Verdict", "ParsedVerdict", "RunRecord", "DispatchRecord", "DeliveryRecord",
    "ArtifactRef", "RunEvent", "RunPage", "RunnerInfo", "Parameter", "Health", "ChunkPlan",
    "Selection", "LiveStatus", "PlanPreview",
    # vocabulary
    "RUN_STATES", "ACTIVE_STATES", "TERMINAL_STATES", "COMPLETIONS", "VERDICT_STATUSES",
    "REPORTABLE_STATUSES", "LIVE_STATUSES", "LIFECYCLE_KINDS", "MARKER_FILENAME", "SPEC_VERSION",
    "DISPATCH_ERROR_CODES", "PARAMETER_PROBLEMS",
    # errors
    "RunComposerError", "InvalidRequest", "InvalidParameter", "UnknownRunner", "ParseError",
    "NotFound", "UnknownRun", "Conflict", "RunActive", "InvalidTransition", "SpecConflict",
    "DispatchRefused", "DispatchCancelled", "AuthRequired", "Forbidden", "PayloadTooLarge",
    "IngestError",
    "ConfigError", "PluginError", "StoreError", "FilterError", "SelectionError",
    # helpers
    "plan_chunks", "split_max", "bundle_hash", "verdicts_hash", "canonical_json", "sha256_hex",
    "read_marker", "forbid_dtd", "parse_xml", "completion_of", "effective_verdicts",
    "missing_verdicts", "outstanding_shards", "summarize", "new_ulid", "utc_now", "parse_time",
    "normalize_time", "epoch_ms", "parse_filter", "format_filter", "load_document",
    # the filter's text form: read, quote, and the error that says where
    "parse_filter_text", "read_filter_string", "quote_pattern", "FilterSyntaxError",
    "validate_document", "validate_parameters", "parameters_from_config", "load_import_path",
    "RemoteLiveChannel", "corpus_drifted", "ConfiguredCall", "configured_calls",
    "push_results", "ResultsPushRefused",
    # the callback URLs of a run, for an executor elsewhere
    "callback_urls", "CallbackUrls",
    # worker processes for any framework: lanes, a timeout, the stop protocol
    "supervise_workers", "WorkerPayload", "WorkerOutcome", "STOP_FILE", "ORPHAN_GRACE_S",
    # embedding
    "Service", "load_config", "Config",
}
EMBEDDING = {"Service", "load_config", "Config"}

#: The toolkits, frozen the same way: what the reference `robot-pool` and
#: `jenkins` runners are built from, and what a connector composes.
ROBOT_TOOLKIT = {
    "ExactSelection", "attribute_roots",
    "LiveStatusListener", "RemoteLiveStatusListener",
    "Execution", "ShardPayload", "ShardOutput", "ShardResult", "ShardOutcome",
    "run_shard", "read_shard_result", "supervise", "RESULT_FILE", "STOP_FILE",
    "collect_verdicts", "deliver_shard", "merge_outputs", "MergeNoiseRemover", "robot_version",
    # merging in a spawned process; the single-root check
    "MergePayload", "MergeResult", "run_merge", "spawn_merge", "NameTopSuites", "root_mismatches",
    # an execution's output.xml rewritten without keyword data, in the worker
    "clean_output",
    # one merged result per shard group and one for the run, before the last delivery
    "GroupMerger",
    # the Robot variable names a payload may set (no ':' — Robot splits there)
    "VARIABLE_NAME_PATTERN", "variable_name_problem",
}
JENKINS_TOOLKIT = {
    "JenkinsClient", "JenkinsError", "JenkinsTimeout", "JenkinsQueueCancelled", "encode_spec", "decode_spec", "queue_id_of",
    # what a job declares of each build parameter: kind, choices, default
    "JobParameter",
}


class TestSurface:
    def test_the_public_names_are_exactly_the_frozen_list(self):
        exported = set(sdk.__all__)
        removed, added = PUBLIC_NAMES - exported, exported - PUBLIC_NAMES
        assert not removed, (
            f"removed from runcomposer.sdk: {sorted(removed)} — a breaking change for "
            "connectors; if intended, update PUBLIC_NAMES here and list it in CHANGELOG.md"
        )
        assert not added, (
            f"added to runcomposer.sdk: {sorted(added)} — a promise to keep; add it to "
            "PUBLIC_NAMES here"
        )

    def test_no_name_is_listed_twice(self):
        assert len(sdk.__all__) == len(set(sdk.__all__))

    def test_every_name_resolves(self):
        for name in sdk.__all__:
            assert getattr(sdk, name) is not None, name

    def test_an_unknown_name_is_an_attribute_error(self):
        with pytest.raises(AttributeError, match="no attribute 'ServiceLocator'"):
            sdk.ServiceLocator  # noqa: B018

    def test_names_are_the_core_objects_not_copies(self):
        from runcomposer.core.errors import ParseError
        from runcomposer.core.model import Verdict
        from runcomposer.core.ports import DispatchContext

        assert sdk.Verdict is Verdict and sdk.ParseError is ParseError
        assert sdk.DispatchContext is DispatchContext

    def test_the_error_taxonomy_has_one_root_and_stable_codes(self):
        errors = [
            getattr(sdk, name)
            for name in sdk.__all__
            if inspect.isclass(getattr(sdk, name)) and issubclass(getattr(sdk, name), Exception)
        ]
        assert len(errors) >= 20
        for error in errors:
            assert issubclass(error, sdk.RunComposerError), error
            assert isinstance(error.code, str) and error.code, error
        assert sdk.DispatchRefused.code == "dispatch-refused"
        assert sdk.UnknownRun("x").code == "unknown-run"
        assert sdk.IngestError("x", reason="sha-mismatch").code == "sha-mismatch"

    def test_the_test_source_protocol_is_not_collected_as_a_test(self):
        assert sdk.TestSource.__test__ is False

    def test_the_toolkits_export_exactly_their_frozen_names(self):
        import runcomposer.sdk.jenkins as jenkins_toolkit
        import runcomposer.sdk.robot as robot_toolkit

        for module, frozen in ((robot_toolkit, ROBOT_TOOLKIT), (jenkins_toolkit, JENKINS_TOOLKIT)):
            exported = set(module.__all__)
            assert exported == frozen, (
                f"{module.__name__}: removed {sorted(frozen - exported)}, added "
                f"{sorted(exported - frozen)} — update the frozen list here and CHANGELOG.md"
            )
            for name in module.__all__:
                assert getattr(module, name) is not None, name


class TestOneStopFile:
    """The file the stop protocol leaves in a worker's directory has one
    definition — the SDK's — and the Robot toolkit re-exports that object."""

    def test_the_toolkit_re_exports_the_sdks_stop_file(self):
        import runcomposer.sdk.robot as robot_toolkit
        from runcomposer.core.supervise import STOP_FILE

        assert robot_toolkit.STOP_FILE is sdk.STOP_FILE is STOP_FILE

    def test_the_grace_of_an_orphaned_worker_is_the_sdks_number(self):
        import inspect

        from runcomposer.core import supervise

        assert sdk.ORPHAN_GRACE_S is supervise.ORPHAN_GRACE_S == 5.0
        # The watcher a worker runs waits exactly this long before it kills.
        assert "ORPHAN_GRACE_S" in inspect.getsource(supervise._watch_supervisor)

    def test_no_second_definition_is_written_anywhere_in_the_package(self):
        import re
        from pathlib import Path

        package = Path(sdk.__file__).resolve().parent.parent
        defining = [
            str(path.relative_to(package))
            for path in package.rglob("*.py")
            if re.search(r"^STOP_FILE\s*=", path.read_text(encoding="utf-8"), re.MULTILINE)
        ]
        assert defining == ["core/supervise.py"]

    def test_there_is_one_stop_protocol(self):
        """A second, weaker protocol (terminate one process, leave what it
        started) is not part of the surface: ``supervise_workers`` is."""
        import runcomposer.sdk.robot as robot_toolkit

        assert not hasattr(robot_toolkit, "stop_worker")


class TestLazyEmbedding:
    """A worker process running a plugin imports the SDK; it must not pay for
    (or depend on) the application layer and the web framework."""

    def modules_after(self, statement: str) -> set[str]:
        code = (
            f"import sys\n{statement}\n"
            "import json\n"
            "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in "
            "('runcomposer', 'fastapi', 'starlette', 'uvicorn', 'pydantic'))))"
        )
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
        )
        return set(json.loads(done.stdout.strip().splitlines()[-1]))

    def test_importing_the_sdk_loads_the_core_only(self):
        loaded = self.modules_after("import runcomposer.sdk")
        assert "runcomposer.sdk" in loaded and "runcomposer.core.ports" in loaded
        heavy = {
            module
            for module in loaded
            if module.startswith(("runcomposer.app", "runcomposer.http", "runcomposer.cli",
                                  "runcomposer.plugins", "fastapi", "starlette", "uvicorn",
                                  "pydantic"))
        }
        assert not heavy, sorted(heavy)

    def test_importing_the_sdk_loads_neither_toolkit_nor_robot_framework(self):
        loaded = self.modules_after(
            "import runcomposer.sdk\n"
            "assert not [m for m in sys.modules if m == 'robot' or m.startswith('robot.')]\n"
            "assert 'runcomposer.sdk.robot' not in sys.modules\n"
            "assert 'runcomposer.sdk.jenkins' not in sys.modules"
        )
        assert "runcomposer.sdk" in loaded

    def test_the_robot_toolkit_and_the_runner_built_on_it_import_without_robot_framework(self):
        """Robot Framework is loaded when something is executed — not when
        the toolkit, or the runner built on it, is imported: a deployment
        without the extra still starts, describes the runner and reports its
        health."""
        self.modules_after(
            "import runcomposer.sdk.robot, runcomposer.sdk.jenkins, runcomposer.plugins.robot_pool\n"
            "assert not [m for m in sys.modules if m == 'robot' or m.startswith('robot.')], "
            "'importing the toolkit loaded Robot Framework'\n"
            "from runcomposer.sdk.robot import ExactSelection, ShardPayload, LiveStatusListener\n"
            "ExactSelection(['Shop.Cart.Add Item'])\n"
            "assert not [m for m in sys.modules if m == 'robot' or m.startswith('robot.')]"
        )

    def test_a_plugin_module_does_not_drag_the_app_layer_in(self):
        loaded = self.modules_after("import runcomposer.plugins.robot_pool, runcomposer.plugins.sqlite_store")
        assert not {m for m in loaded if m.startswith(("runcomposer.app", "runcomposer.http", "fastapi"))}

    def test_the_embedding_names_load_on_first_use(self):
        loaded = self.modules_after(
            "import runcomposer.sdk as sdk\n"
            "assert 'runcomposer.app.service' not in sys.modules\n"
            "service_class = sdk.Service\n"
            "assert 'Service' in vars(sdk)  # cached after the first access\n"
            "from runcomposer.sdk import load_config, Config"
        )
        assert {"runcomposer.app.service", "runcomposer.app.config"} <= loaded
        assert not {m for m in loaded if m.startswith(("fastapi", "runcomposer.http"))}

    def test_they_are_the_application_layers_own_objects(self):
        from runcomposer.app.config import Config, load_config
        from runcomposer.app.service import Service

        assert sdk.Service is Service and sdk.load_config is load_config and sdk.Config is Config
        assert set(sdk.__all__) >= EMBEDDING


def protocol_methods(protocol: type) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in vars(protocol).items()
        if inspect.isfunction(member) and not name.startswith("_")
    }


def loaded(group: str) -> dict[str, type]:
    return {
        ep.name: ep.load()
        for ep in entry_points(group=group)
        if ep.dist is not None and ep.dist.name == "runcomposer"
    }


class TestReferencePluginsFitTheProtocols:
    """Structural, not behavioural: every member a protocol names exists on
    every reference plugin, with the same parameter names. (That the
    behaviour is right is what the plugins' own tests are for.)"""

    def assert_fits(self, cls: type, protocol: type, *, exact: bool = True) -> None:
        for name, expected in protocol_methods(protocol).items():
            member = getattr(cls, name, None)
            assert callable(member), f"{cls.__name__} lacks {protocol.__name__}.{name}"
            if exact:
                actual = inspect.signature(member)
                assert list(actual.parameters) == list(expected.parameters), (
                    f"{cls.__name__}.{name}{actual} does not match "
                    f"{protocol.__name__}.{name}{expected}"
                )

    def test_the_store(self):
        (store,) = loaded("runcomposer.stores").values()
        assert len(protocol_methods(sdk.RunStore)) >= 28
        self.assert_fits(store, sdk.RunStore)
        assert isinstance(store.store_id, str)

    def test_the_runners(self):
        runners = loaded("runcomposer.runners")
        assert set(runners) == {"demo", "robot-pool", "jenkins", "command"}
        for cls in runners.values():
            self.assert_fits(cls, sdk.Runner)
            assert issubclass(cls, sdk.RunnerBase)

    def test_the_sources(self):
        sources = loaded("runcomposer.sources")
        assert set(sources) == {"manifest", "robotframework", "demo-corpus"}
        for cls in sources.values():
            self.assert_fits(cls, sdk.TestSource)
            assert isinstance(cls.provider_id, str)
            assert callable(cls.fingerprint)  # the optional staleness probe: all three have it

    def test_the_parsers(self):
        parsers = loaded("runcomposer.parsers")
        assert set(parsers) == {"runcomposer-verdicts", "robot-output-xml", "junit-xml"}
        for format_id, cls in parsers.items():
            self.assert_fits(cls, sdk.ResultParser)
            assert cls.format_id == format_id  # registered under the id it declares

    def test_the_observers(self):
        observers = loaded("runcomposer.observers")
        assert set(observers) == {"webhook", "jsonl", "command"}
        for cls in observers.values():
            self.assert_fits(cls, sdk.RunObserver)

    def test_the_real_dispatch_context_has_every_member_the_protocol_names(self):
        from runcomposer.app.dispatching import _Context

        self.assert_fits(_Context, sdk.DispatchContext)
        attributes = set(sdk.DispatchContext.__annotations__)
        assert {"run_id", "dispatch_id", "runner_id", "spec", "item_ids", "options",
                "parameters", "links", "workdir", "private_dir", "source", "history"} == attributes
        # A plan context is a strict subset of a dispatch context: one
        # planning function serves both.
        assert set(sdk.PlanContext.__annotations__) < attributes
        assert set(protocol_methods(sdk.PlanContext)) < set(protocol_methods(sdk.DispatchContext))
        assert isinstance(_Context.cancelled, property)


class TestEmbedding:
    """The embedding surface: a script drives a whole run with nothing
    imported but the SDK."""

    def test_compose_dispatch_wait_read_in_one_process(self, tmp_path):
        from runcomposer.sdk import Service, effective_verdicts, load_config, summarize

        config = load_config(str(write_config(tmp_path)))
        with Service(config) as service:
            composed = service.compose_run(
                {"tag_filter": {"op": "AND", "items": ["Checkout", {"not": "prefix:Quarantine-"}]}},
                title="Checkout before the release",
                labels={"suite": "release"},
                origin="script",
                runner_id="demo",
                parameters={"seed": "release-1"},
            )
            run = composed.run
            assert run is not None and run.state == "COMPOSED"
            assert run.planned_count == len(composed.items) > 0

            outcome = service.dispatch(run.id, "demo", {"seed": "release-1"}, foreground=True)
            assert outcome.refusal is None
            finished = service.wait(run.id, timeout=30)

            assert finished.state == "COMPLETE"
            assert finished.completion in sdk.COMPLETIONS
            dispatch = finished.latest_dispatch
            assert dispatch is not None and dispatch.dispatch_id == outcome.dispatch_id
            verdicts = service.store.verdicts_for(run.id, dispatch.dispatch_id)
            assert {v.item_id for v in verdicts} == {item.id for item in composed.items}
            assert summarize(verdicts) == dict(dispatch.summary)
            assert len(effective_verdicts(verdicts)) == run.planned_count
            kinds = [event.kind for event in service.store.events(run.id)]
            assert kinds[0] == "run.composed" and kinds[-1] == "run.completed"

            # History is live in the same process: rerun what failed.
            failed, provenance = service.resolve_history("failed@latest", labels={"suite": "release"})
            assert provenance["resolved_run_id"] == run.id
            assert set(failed) == {v.item_id for v in verdicts if v.status == "FAIL"}

    def test_a_refusal_is_reported_on_the_outcome_not_raised(self, tmp_path):
        """Embedders branch on ``outcome.refusal``; the run stays as it was."""
        from runcomposer.sdk import Service, load_config

        config = load_config(
            str(write_config(tmp_path, runners={"refusing": {"module": "tests.test_sdk:RefusingRunner"}}))
        )
        with Service(config) as service:
            run = service.compose_run({"tag_filter": "Payments"}, title="t", origin="script").run
            outcome = service.dispatch(run.id, "refusing", foreground=True)
            assert outcome.refusal == "the staging shop is down for maintenance"
            after = service.require_run(run.id)
            assert after.state == "COMPOSED" and after.dispatches == ()

    def test_request_errors_are_typed(self, tmp_path):
        from runcomposer.sdk import (
            InvalidParameter,
            SelectionError,
            Service,
            UnknownRun,
            UnknownRunner,
            load_config,
        )

        with Service(load_config(str(write_config(tmp_path)))) as service:
            with pytest.raises(UnknownRun):
                service.require_run("01NOSUCHRUN000000000000000")
            with pytest.raises(SelectionError):
                service.compose_run({}, title="t", origin="script")
            with pytest.raises(UnknownRunner):
                service.compose_run({"tag_filter": "Payments"}, title="t", origin="script",
                                    runner_id="nope")
            with pytest.raises(InvalidParameter) as caught:
                service.compose_run({"tag_filter": "Payments"}, title="t", origin="script",
                                    runner_id="demo", parameters={"seed": 7})
            assert caught.value.parameter == "seed"
            assert service.list_runs().total == 0  # none of the refusals left a run behind


class RefusingRunner(sdk.RunnerBase):
    """Written against the SDK alone, as a connector would be."""

    def describe(self) -> sdk.RunnerInfo:
        return sdk.RunnerInfo(id="refusing")

    def dispatch(self, ctx: sdk.DispatchContext) -> None:
        ctx.refuse("the staging shop is down for maintenance")
