"""Robot plugin family tests (DESIGN.md §6.1, §6.2a, §5, §3.3).

These need Robot Framework (`pip install '.[robot]'`) and skip
cleanly without it — the extra is a plugin dependency, never a core one.
"""

import json
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

robot = pytest.importorskip("robot", reason="requires the runcomposer[robot] extra")

from runcomposer.config import Config
from runcomposer.core.model import Verdict
from runcomposer.core.ports import DispatchRefused
from runcomposer.plugins.robot_output_parser import ParseError, RobotOutputXmlParser
from runcomposer.plugins.robot_pool import RobotPoolRunner
from runcomposer.plugins.robot_source import RobotFrameworkSource
from runcomposer.plugins.sqlite_store import SqliteRunStore
from runcomposer.service import Service

CORPUS = Path(__file__).parent.parent / "examples" / "robot-shop" / "tests"


def robot_config(tmp_path, corpus=CORPUS, **runner_options):
    return Config(
        data={
            "store": {"sqlite": {"path": str(tmp_path / "robot.db")}},
            "core": {
                "artifact_dir": str(tmp_path / "artifacts"),
                "ingestion": {"inbox": None, "quarantine_dir": str(tmp_path / "q")},
            },
            "sources": {"robotframework": {"root": str(corpus)}},
            "runners": {
                "robot-pool": {
                    "suite_root": str(corpus),
                    "max_workers": 2,
                    "variables": {"STAGE": "test"},
                    **runner_options,
                }
            },
        }
    )


class TestRobotSource:
    def test_several_roots_form_one_catalog(self, tmp_path):
        """A corpus split over sibling suite trees is still one corpus."""
        for area, test in (("Api", "Ping Responds"), ("Ui", "Login Works")):
            directory = tmp_path / area
            directory.mkdir()
            (directory / "s.robot").write_text(
                f"*** Test Cases ***\n{test}\n    [Tags]    {area}    Smoke\n    Log    ok\n",
                encoding="utf-8",
            )
        source = RobotFrameworkSource(roots=[str(tmp_path / "Api"), str(tmp_path / "Ui")])
        # Each root names its own top-level suite, so the ids are exactly the
        # ones a run over those roots reports back — no common-parent segment.
        assert [item.id for item in source.items()] == [
            "Api.S.Ping Responds",
            "Ui.S.Login Works",
        ]
        assert {tag for item in source.items() for tag in item.tags} == {"Api", "Ui", "Smoke"}
        assert source.duplicate_ids == []

    def test_a_filter_reaches_across_roots(self, tmp_path):
        for area in ("Api", "Ui"):
            directory = tmp_path / area
            directory.mkdir()
            (directory / "s.robot").write_text(
                f"*** Test Cases ***\n{area} One\n    [Tags]    Smoke\n    Log    ok\n",
                encoding="utf-8",
            )
        from runcomposer.core.selection import Selection

        source = RobotFrameworkSource(roots=[str(tmp_path / "Api"), str(tmp_path / "Ui")])
        selection = Selection.from_data({"tag_filter": "Smoke"})
        assert len(selection.compile(source.items())) == 2
        # …and a single root sees only its own half, which is the whole point.
        one = RobotFrameworkSource(root=str(tmp_path / "Api"))
        assert len(selection.compile(one.items())) == 1

    def test_two_tests_under_one_id_are_reported_not_raised(self, tmp_path):
        (tmp_path / "Api").mkdir()
        (tmp_path / "Api" / "s.robot").write_text(
            "*** Test Cases ***\nSame\n    Log    ok\nSame\n    Log    ok\n", encoding="utf-8"
        )
        source = RobotFrameworkSource(root=str(tmp_path / "Api"))
        assert len(source.items()) == 2
        assert source.duplicate_ids == ["Api.S.Same"]

    def test_a_missing_root_is_named(self, tmp_path):
        (tmp_path / "Api").mkdir()
        (tmp_path / "Api" / "s.robot").write_text(
            "*** Test Cases ***\nOne\n    Log    ok\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="Gone"):
            RobotFrameworkSource(roots=[str(tmp_path / "Api"), str(tmp_path / "Gone")])

    def test_a_source_without_any_root_fails_loudly(self):
        with pytest.raises(ValueError, match="'root' or 'roots'"):
            RobotFrameworkSource()

    def test_both_root_forms_are_anchored_to_the_config_file(self):
        options = RobotFrameworkSource.resolve_config_paths(
            {"root": "suite", "roots": ["a", "/abs/b"]}, lambda value: value
            if value.startswith("/") else f"/base/{value}",
        )
        assert options == {"root": "/base/suite", "roots": ["/base/a", "/abs/b"]}

    def test_ids_are_longnames_with_tags(self):
        source = RobotFrameworkSource(root=str(CORPUS))
        items = {item.id: item for item in source.items()}
        visa = items["Tests.Payments.Visa Payment Succeeds"]
        assert visa.name == "Visa Payment Succeeds"
        # area + sub-area + suite + sprint + ticket: the corpus carries the same
        # tag world as the bundled demo corpus (DESIGN.md §12).
        assert set(visa.tags) == {"Payments", "Payments-Cards", "Smoke", "Sprint-12", "SHOP-1200"}
        assert visa.hierarchy == ("Tests", "Payments")
        assert any("Quarantine-Flaky" in item.tags for item in items.values())

    def test_a_relative_root_is_found_from_any_working_directory(self, tmp_path, monkeypatch):
        """§8: the source root resolves against the config file's directory.
        It used to resolve against the cwd, so the same `--config` failed with
        `robotframework source root not found` from anywhere else."""
        from runcomposer.config import load_config

        shutil.copytree(CORPUS, tmp_path / "suite" / "tests")
        config_file = tmp_path / "suite" / "config.yaml"
        config_file.write_text(
            "sources: {robotframework: {root: tests}}\n"
            "store: {sqlite: {path: runcomposer.db}}\n",
            encoding="utf-8",
        )
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        source = load_config(str(config_file)).build_source()
        assert "Tests.Payments.Visa Payment Succeeds" in {item.id for item in source.items()}

    def test_corpus_shape(self):
        """The shipped corpus is big enough to be worth demoing: every area
        file contributes, and the deliberate fixtures are all present."""
        items = list(RobotFrameworkSource(root=str(CORPUS)).items())
        assert len(items) == 58
        assert {item.hierarchy[1] for item in items} == {
            "Account",
            "Auth",
            "Cart",
            "Catalog",
            "Checkout",
            "Payments",
        }
        assert len({tag for item in items for tag in item.tags}) == 50

        def by_tag(tag):
            return [item.id for item in items if tag in item.tags]

        assert len(by_tag("SlowLane")) == 2  # the sleeping pair (live status)
        assert by_tag("VarCheck") == ["Tests.Checkout.Stage Variable Reaches The Test"]
        assert len(by_tag("Quarantine-Flaky")) == 3

    def test_snapshot_changes_when_corpus_changes(self, tmp_path):
        copy = tmp_path / "tests"
        shutil.copytree(CORPUS, copy)
        before = RobotFrameworkSource(root=str(copy)).snapshot()
        assert before.startswith("sha256:")
        (copy / "payments.robot").write_text(
            (copy / "payments.robot").read_text() + "\nAdded Test\n    Log    new\n",
            encoding="utf-8",
        )
        assert RobotFrameworkSource(root=str(copy)).snapshot() != before

    def test_resolve_is_exact_plus_strip(self):
        source = RobotFrameworkSource(root=str(CORPUS))
        longname = "Tests.Payments.Visa Payment Succeeds"
        assert source.resolve(longname) == longname
        assert source.resolve(f"  {longname}  ") == longname
        assert source.resolve("Tests.Payments.No Such Test") is None


@pytest.fixture(scope="module")
def sample_output(tmp_path_factory):
    """One real robot execution shared by the parser tests."""
    out_dir = tmp_path_factory.mktemp("robot-out")
    import robot as robot_module

    robot_module.run(
        str(CORPUS),
        test=["Tests.Payments.Visa Payment Succeeds", "Tests.Payments.Expired Card Is Rejected Loudly"],
        outputdir=str(out_dir),
        output="output.xml",
        log="NONE",
        report="NONE",
        stdout=open("/dev/null", "w"),
    )
    return out_dir / "output.xml"


class TestRobotOutputParser:
    def test_parses_longnames_statuses_durations(self, sample_output):
        parsed = {p.native_name: p for p in RobotOutputXmlParser().parse(sample_output)}
        assert parsed["Tests.Payments.Visa Payment Succeeds"].status == "PASS"
        failing = parsed["Tests.Payments.Expired Card Is Rejected Loudly"]
        assert failing.status == "FAIL"
        assert "expired card" in failing.message
        assert all(p.duration_ms >= 0 for p in parsed.values())

    @pytest.mark.parametrize("payload", ["<!DOCTYPE", "<!ENTITY"])
    def test_doctype_and_entity_declarations_are_refused(self, tmp_path, sample_output, payload):
        # §5 defused parsing: reject before any XML machinery runs.
        malicious = tmp_path / "output.xml"
        original = sample_output.read_text(encoding="utf-8")
        evil = original.replace(
            "<robot", f'{payload} robot [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>\n<robot', 1
        ) if payload == "<!DOCTYPE" else f'<!ENTITY x "y">\n{original}'
        malicious.write_text(evil, encoding="utf-8")
        with pytest.raises(ParseError, match="defused"):
            RobotOutputXmlParser().parse(malicious)

    def test_garbage_is_a_parse_error(self, tmp_path):
        bad = tmp_path / "output.xml"
        bad.write_text("<robot><unclosed>", encoding="utf-8")
        with pytest.raises(ParseError, match="not a parsable"):
            RobotOutputXmlParser().parse(bad)


class TestRobotPoolEndToEnd:
    def test_dispatch_runs_real_robot_to_complete(self, tmp_path):
        config = robot_config(tmp_path)
        service = Service(config)
        result = service.compose_run(
            {"tag_filter": "Payments"},
            title="Robot E2E",
            origin="test",
            runner_section={"robot-pool": config.runner_options("robot-pool")},
        )
        dispatch = service.dispatch_runner(result.run.id, "robot-pool")
        run = service.store.get_run(result.run.id)
        assert run.state == "COMPLETE"
        assert run.completion == "FAIL"  # the corpus ships one deliberately failing test
        verdicts = service.store.verdicts_for(run.id, dispatch.dispatch_id)
        assert len(verdicts) == 13  # every Payments-tagged test in the corpus
        statuses = {v.item_id: v.status for v in verdicts}
        assert statuses["Tests.Payments.Expired Card Is Rejected Loudly"] == "FAIL"
        assert statuses["Tests.Payments.Visa Payment Succeeds"] == "PASS"
        # per-dispatch isolated artifacts + robot-output-xml deliveries (§6.2a)
        out_root = tmp_path / "artifacts" / run.id / dispatch.dispatch_id
        outputs = list(out_root.rglob("output.xml"))
        assert len(outputs) == len(run.deliveries) > 0
        assert {d.format for d in run.deliveries} == {"robot-output-xml"}
        assert "cold start" in service.last_dispatch_plan

    def test_partition_fanout_and_variables(self, tmp_path):
        config = robot_config(tmp_path, partitions=["env1", "env2"])
        service = Service(config)
        result = service.compose_run(
            {"tag_filter": "VarCheck"},
            title="Variables via partitions",
            origin="test",
            runner_section={"robot-pool": config.runner_options("robot-pool")},
        )
        dispatch = service.dispatch_runner(result.run.id, "robot-pool")
        run = service.store.get_run(result.run.id)
        # The VarCheck test passes ONLY when STAGE=test overrides the suite's
        # default — one PASS per partition proves variables reach each one.
        verdicts = service.store.verdicts_for(run.id, dispatch.dispatch_id)
        assert [v.status for v in verdicts] == ["PASS", "PASS"]
        assert {d.shard for d in run.deliveries} == {"env1-1", "env2-1"}
        assert run.completion == "PASS"


class TestDriftContract:
    """§3.3: refuse by default; allow-drift executes the intersection and
    reports the difference as SKIP verdicts with reason drift."""

    def _compose_then_mutate(self, tmp_path, **runner_options):
        corpus = tmp_path / "tests"
        shutil.copytree(CORPUS, corpus)
        config = robot_config(tmp_path, corpus=corpus, **runner_options)
        service = Service(config)
        result = service.compose_run(
            {"tag_filter": "Catalog"},
            title="Drift target",
            origin="test",
            runner_section={"robot-pool": config.runner_options("robot-pool")},
        )
        (corpus / "catalog.robot").unlink()  # the corpus drifts after compose
        return service, result

    def test_drift_refused_by_default(self, tmp_path):
        service, result = self._compose_then_mutate(tmp_path)
        with pytest.raises(DispatchRefused, match="drift"):
            service.dispatch_runner(result.run.id, "robot-pool")
        refused = service.store.get_run(result.run.id)
        assert refused.state == "COMPOSED"
        # the drift check runs before the hand-off, so nothing was dispatched:
        # a refusal leaves no dispatch row to masquerade as an execution (§4)
        assert refused.dispatches == ()

    def test_allow_drift_executes_intersection_with_skip_verdicts(self, tmp_path):
        service, result = self._compose_then_mutate(tmp_path, allow_drift=True)
        dispatch = service.dispatch_runner(result.run.id, "robot-pool")
        run = service.store.get_run(result.run.id)
        assert run.state == "COMPLETE"
        verdicts = service.store.verdicts_for(run.id, dispatch.dispatch_id)
        drifted = [v for v in verdicts if v.status == "SKIP" and v.message == "drift"]
        assert len(drifted) == 8  # all Catalog tests lived in the deleted file
        assert {v.item_id for v in drifted} == {i.id for i in result.items}


class TestLiveStatus:
    def test_listener_streams_and_terminal_delivery_reconciles(self, tmp_path, slow_lane_suite):
        """Dispatch the sleeping tests in a thread; mid-run the store must show
        RUNNING with 0 < live verdicts < planned — and the dispatch those live
        verdicts belong to (§4: the hand-off is recorded when it happens, not
        when it finishes); afterwards COMPLETE."""
        import threading

        config = robot_config(tmp_path, corpus=slow_lane_suite)
        service = Service(config)
        result = service.compose_run(
            {"tag_filter": "SlowLane"},
            title="Live status",
            origin="test",
            runner_section={"robot-pool": config.runner_options("robot-pool")},
        )
        planned = len(result.items)
        assert planned == 2
        worker = threading.Thread(
            target=service.dispatch_runner, args=(result.run.id, "robot-pool")
        )
        worker.start()
        observed_partial = None
        observed_dispatch = None
        deadline = time.time() + 30
        while time.time() < deadline and worker.is_alive():
            run = service.store.get_run(result.run.id)
            count = len(service.store.verdicts_for(result.run.id))
            if run.state == "RUNNING" and 0 < count < planned:
                observed_partial = count
                observed_dispatch = run.dispatches[-1] if run.dispatches else None
                break
            time.sleep(0.2)
        worker.join(timeout=60)
        assert observed_partial is not None, "never observed a live partial verdict"
        assert observed_dispatch is not None, "RUNNING run showed no dispatch record"
        assert observed_dispatch.mode == "robot-pool"
        run = service.store.get_run(result.run.id)
        # the in-flight dispatch is the one that completed — same record, and
        # the live verdicts were already attributed to it
        assert [d.dispatch_id for d in run.dispatches] == [observed_dispatch.dispatch_id]
        assert run.state == "COMPLETE" and run.completion == "PASS"
        # live rows were reconciled away: exactly the terminal verdicts remain
        assert len(service.store.verdicts_for(result.run.id)) == planned

    def test_without_listener_no_live_rows_only_final(self, tmp_path):
        config = robot_config(tmp_path, live_status=False)
        service = Service(config)
        result = service.compose_run(
            {"tag_filter": "Smoke"},
            title="Degraded path",
            origin="test",
            runner_section={"robot-pool": config.runner_options("robot-pool")},
        )
        dispatch = service.dispatch_runner(result.run.id, "robot-pool")
        run = service.store.get_run(result.run.id)
        assert run.state == "COMPLETE"
        assert len(service.store.verdicts_for(run.id, dispatch.dispatch_id)) == len(result.items)


class TestListenerPassThroughAndHooks:
    """§6.2a options: a user-supplied `listener` and `pre_run_hooks`."""

    def test_user_listener_receives_events(self, tmp_path):
        listener_file = tmp_path / "SentinelListener.py"
        sentinel = tmp_path / "seen.txt"
        listener_file.write_text(
            "class SentinelListener:\n"
            "    ROBOT_LISTENER_API_VERSION = 3\n"
            "    def __init__(self, out):\n"
            "        self.out = out\n"
            "    def end_test(self, data, result):\n"
            "        with open(self.out, 'a') as fh:\n"
            "            fh.write(result.name + '\\n')\n",
            encoding="utf-8",
        )
        config = robot_config(tmp_path, listener=f"{listener_file}:{sentinel}")
        service = Service(config)
        result = service.compose_run(
            {"tag_filter": "Smoke"}, title="Listener pass-through", origin="test",
            runner_section={"robot-pool": config.runner_options("robot-pool")},
        )
        service.dispatch_runner(result.run.id, "robot-pool")
        seen = sentinel.read_text(encoding="utf-8").splitlines()
        assert sorted(seen) == sorted(item.name for item in result.items)

    def test_pre_run_hooks_run_before_execution(self, tmp_path):
        sentinel = tmp_path / "hook-ran.txt"
        config = robot_config(tmp_path, pre_run_hooks=[f"echo prepared > {sentinel}"])
        service = Service(config)
        result = service.compose_run(
            {"tag_filter": "Smoke"}, title="Hooks", origin="test",
            runner_section={"robot-pool": config.runner_options("robot-pool")},
        )
        service.dispatch_runner(result.run.id, "robot-pool")
        assert sentinel.read_text(encoding="utf-8").strip() == "prepared"

    def test_failing_hook_refuses_the_dispatch(self, tmp_path):
        config = robot_config(tmp_path, pre_run_hooks=["echo doomed >&2; exit 3"])
        service = Service(config)
        result = service.compose_run(
            {"tag_filter": "Smoke"}, title="Failing hook", origin="test",
            runner_section={"robot-pool": config.runner_options("robot-pool")},
        )
        with pytest.raises(DispatchRefused, match="pre_run_hook failed .rc 3.*doomed"):
            service.dispatch_runner(result.run.id, "robot-pool")
        assert service.store.verdicts_for(result.run.id) == []


class TestDurationHistory:
    def _seed_history(self, store, durations, labels=None):
        from runcomposer.core.spec import build_spec

        spec = build_spec(
            title="History seed",
            tag_filter="Payments",
            materialized_ids=list(durations),
            source_provider="robotframework",
            snapshot="sha256:" + "ab" * 32,
            labels=labels or {},
        )
        run = store.create_run(spec, origin="test")
        store.add_dispatch(run.id, dispatch_id=f"D-{run.id}", mode="robot-pool", declared_shards=1)
        store.record_delivery(
            run.id,
            dispatch_id=f"D-{run.id}",
            shard="1",
            content_hash=f"sha256:{run.id}",
            format="robot-output-xml",
            verdicts=[Verdict(i, "PASS", duration_ms=d) for i, d in durations.items()],
        )
        return run

    def test_aggregates_average_over_recent_dispatches(self, tmp_path):
        store = SqliteRunStore(path=tmp_path / "h.db")
        self._seed_history(store, {"A": 100, "B": 300})
        self._seed_history(store, {"A": 200, "B": 500})
        aggregates = store.duration_aggregates(last_n=5)
        assert aggregates == {"A": 150.0, "B": 400.0}

    def test_label_selector_filters_history(self, tmp_path):
        store = SqliteRunStore(path=tmp_path / "h.db")
        self._seed_history(store, {"A": 100}, labels={"suite": "nightly"})
        self._seed_history(store, {"A": 900}, labels={"suite": "adhoc"})
        nightly = store.duration_aggregates(labels={"suite": "nightly"}, last_n=5)
        assert nightly == {"A": 100.0}

    def test_cold_vs_warm_plan_text(self, tmp_path):
        store = SqliteRunStore(path=tmp_path / "h.db")
        runner = RobotPoolRunner(suite_root="unused", max_workers=2)
        runner.bind(store=store, source=None, artifact_root=tmp_path)
        _, cold = runner._plan(["A", "B", "C", "D"], ["default"])
        assert "cold start" in cold and "round-robin" in cold
        self._seed_history(store, {"A": 4000, "B": 100, "C": 100, "D": 100})
        chunks, warm = runner._plan(["A", "B", "C", "D"], ["default"])
        assert "duration-balanced" in warm and "cold start" not in warm
        # LPT: the slow item ends up alone; the three fast ones share a chunk
        assert sorted(len(c) for c in chunks) == [1, 3]


class TestListenerUnit:
    def test_live_rows_written_and_cleared(self, tmp_path):
        from runcomposer.plugins.robot_listener import LiveStatusListener
        from runcomposer.core.spec import build_spec

        store = SqliteRunStore(path=tmp_path / "l.db")
        spec = build_spec(
            title="L", tag_filter="x", materialized_ids=["T.A"],
            source_provider="robotframework", snapshot="sha256:" + "ab" * 32,
        )
        run = store.create_run(spec, origin="test")
        listener = LiveStatusListener(store, run.id, "D1", "1")
        listener.end_test(
            None, SimpleNamespace(longname="T.A", status="PASS", elapsedtime=12, message="")
        )
        assert [v.item_id for v in store.verdicts_for(run.id)] == ["T.A"]
        store.clear_live_verdicts(run.id, "D1")
        assert store.verdicts_for(run.id) == []
