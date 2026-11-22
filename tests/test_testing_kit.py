"""runcomposer.testing — the conformance kit, and the proof it is enough.

Two halves:

1. **Every reference plugin passes the kit.** Each source, parser, store,
   observer and runner shipped in ``runcomposer.plugins`` is checked with the
   same calls a connector author makes — and the runners are driven through
   the ``ConnectorHarness`` as well. If the kit could not express what these
   plugins need, these tests could not be written with it.
2. **The kit catches what is broken.** A deliberately broken toy for each
   rule family fails with the rule named in the message.

Plus: the fake context enforces the hand-off order with the very messages
the real one uses; the kit imports without pytest; its names are frozen.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, ClassVar

import pytest

import runcomposer.testing as kit
from runcomposer.plugins.command_runner import CommandRunner
from runcomposer.plugins.demo_runner import DemoRunner
from runcomposer.plugins.jenkins import JenkinsRunner
from runcomposer.plugins.junit_parser import JunitXmlParser
from runcomposer.plugins.manifest_source import DemoCorpusSource, ManifestSource
from runcomposer.plugins.observers import CommandObserver, JsonlObserver, WebhookObserver
from runcomposer.plugins.sqlite_store import SqliteRunStore
from runcomposer.plugins.verdicts_parser import VerdictsParser
from runcomposer.sdk import (
    DispatchContext,
    Health,
    Item,
    ObserverBase,
    ParsedVerdict,
    ParseError,
    ParserBase,
    RunnerBase,
    RunnerInfo,
    SourceBase,
    StoreBase,
    Verdict,
)
from runcomposer.testing import (
    ConnectorHarness,
    FakeDispatchContext,
    check_observer,
    check_parser,
    check_runner,
    check_source,
    check_store,
    sample_events,
    xml_with_dtd,
)

from .conftest import REPO, ROBOT_SHOP, needs_robot, process_gone, wait_until
from .test_command_runner import SHOP_RUNNER
from .test_jenkins import JOB, POLL, FakeJenkins, junit_for, junit_job
from .test_observers import Receiver

PY = sys.executable
CART = ["Shop.Cart.Core.T001", "Shop.Cart.Core.T002", "Shop.Cart.Core.T003"]

#: The kit's public names, frozen like runcomposer.sdk's (tests/test_sdk.py).
KIT_NAMES = {
    "check_source", "check_runner", "check_parser", "check_store", "check_observer",
    "ConnectorHarness", "HarnessRun", "DispatchHandle", "FakeDispatchContext", "FakeHistory", "plugin",
    "ListSource",
    "Delivery", "DEFAULT_ITEM_IDS", "sample_events", "xml_with_dtd",
}


class TestSurface:
    def test_the_public_names_are_exactly_the_frozen_list(self):
        exported = set(kit.__all__)
        assert exported == KIT_NAMES, (
            f"removed: {sorted(KIT_NAMES - exported)}, added: {sorted(exported - KIT_NAMES)} — "
            "a change to the kit's surface is deliberate: update KIT_NAMES and CHANGELOG.md"
        )
        for name in kit.__all__:
            assert getattr(kit, name) is not None

    def test_the_kit_imports_without_pytest(self):
        """A connector may test with any runner: the kit never needs pytest."""
        code = (
            "import sys\n"
            "sys.modules['pytest'] = None  # importing it would now fail\n"
            "import runcomposer.testing as kit\n"
            "ctx = kit.FakeDispatchContext(['Shop.Cart.Core.T001'])\n"
            "print('ok', len(kit.__all__))\n"
        )
        done = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        assert done.stdout.startswith("ok ")

    def test_the_fake_has_every_member_of_the_protocol(self):
        from runcomposer.sdk import DispatchContext as Protocol
        from runcomposer.sdk import contract_members

        fake = FakeDispatchContext(CART)
        missing = [name for name in contract_members(Protocol) if not hasattr(fake, name)]
        assert missing == []

    def test_it_is_a_sibling_of_the_sdk_not_part_of_it(self):
        """Plugins import the SDK at run time (in worker processes too); the
        kit is for tests and pulls in the application layer."""
        import runcomposer.sdk as sdk

        assert not set(kit.__all__) & set(sdk.__all__)


# -- 1. the reference plugins pass the kit ------------------------------------------


class TestReferenceSources:
    def test_the_demo_corpus(self):
        items = check_source(DemoCorpusSource(), rebuild=DemoCorpusSource)
        assert len(items) == 60

    def test_the_manifest_source_with_aliases(self, tmp_path):
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"items": [
            {"id": "tests/test_cart.py::test_total", "tags": ["Cart"],
             "aliases": ["tests.test_cart.test_total"]},
            {"id": "tests/test_cart.py::test_empty", "tags": ["Cart", "Smoke"]},
        ]}), encoding="utf-8")
        check_source(
            ManifestSource(str(manifest)),
            native_names={"tests.test_cart.test_total": "tests/test_cart.py::test_total"},
            rebuild=lambda: ManifestSource(str(manifest)),
        )

    def test_the_bundled_pytest_example_manifest(self):
        check_source(ManifestSource(str(REPO / "examples" / "pytest-shop" / "manifest.json")))

    @needs_robot
    def test_the_robot_framework_source(self):
        from runcomposer.plugins.robot_source import RobotFrameworkSource

        source = RobotFrameworkSource(root=str(ROBOT_SHOP))
        items = check_source(source, rebuild=lambda: RobotFrameworkSource(root=str(ROBOT_SHOP)))
        assert "Tests.Payments.Visa Payment Succeeds" in {item.id for item in items}


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestReferenceParsers:
    def test_the_reference_verdicts_format(self, tmp_path):
        bundle = write(tmp_path / "ok" / "results.json", json.dumps({
            "format": "runcomposer-verdicts",
            "verdicts": [{"name": CART[0], "status": "PASS", "duration_ms": 12},
                         {"name": CART[1], "status": "FAIL", "message": "total off by one"}],
        }))
        check_parser(
            VerdictsParser(), bundle.parent,
            expected={CART[0]: "PASS", CART[1]: "FAIL"},
            bad_bundles=[write(tmp_path / "bad" / "results.json", '{"format": "runcomposer-verdicts", "verdicts": [{"status": 1}]}'),
                         write(tmp_path / "trunc.json", '{"format": "runcomposer-verd')],
        )

    def test_junit_xml(self, tmp_path):
        bundle = tmp_path / "junit"
        write(bundle / "junit.xml", junit_for(CART, {CART[2]}).decode())
        check_parser(
            JunitXmlParser(), bundle,
            expected={CART[0]: "PASS", CART[1]: "PASS", CART[2]: "FAIL"},
            bad_bundles=[xml_with_dtd(tmp_path / "dtd" / "junit.xml"),
                         write(tmp_path / "broken" / "junit.xml", "<testsuite><unclosed>")],
        )

    @needs_robot
    def test_robot_output_xml(self, tmp_path):
        import os

        import robot

        from runcomposer.plugins.robot_output_parser import RobotOutputXmlParser

        with open(os.devnull, "w", encoding="utf-8") as devnull:
            robot.run(str(ROBOT_SHOP), test=["Tests.Payments.Visa Payment Succeeds",
                                             "Tests.Payments.Expired Card Is Rejected Loudly"],
                      outputdir=str(tmp_path / "out"), output="output.xml", log="NONE", report="NONE",
                      stdout=devnull, stderr=devnull)
        check_parser(
            RobotOutputXmlParser(), tmp_path / "out" / "output.xml",
            expected={"Tests.Payments.Visa Payment Succeeds": "PASS",
                      "Tests.Payments.Expired Card Is Rejected Loudly": "FAIL"},
            bad_bundles=[xml_with_dtd(tmp_path / "dtd" / "output.xml", root="robot"),
                         write(tmp_path / "broken" / "output.xml", "<robot><unclosed>")],
        )


class TestReferenceStore:
    def test_sqlite_on_a_file(self, tmp_path):
        made = iter(range(100))
        check_store(lambda: SqliteRunStore(str(tmp_path / f"kit-{next(made)}.db")))

    def test_sqlite_in_memory(self):
        check_store(lambda: SqliteRunStore(":memory:"))


class TestReferenceObservers:
    def test_jsonl(self, tmp_path):
        path = tmp_path / "events.jsonl"

        def lines() -> list[str]:
            return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

        check_observer(JsonlObserver(str(path)), seen=lines)
        assert all(json.loads(line)["run_id"] for line in lines())

    def test_webhook(self):
        receiver = Receiver()
        try:
            check_observer(WebhookObserver(receiver.url, secret="shop-hook-secret"),
                           seen=lambda: receiver.requests)
            assert all(r["headers"].get("X-Runcomposer-Signature", "").startswith("sha256=")
                       for r in receiver.requests)
        finally:
            receiver.close()

    def test_command(self, tmp_path):
        """Each call writes a file of its own: commands run side by side, and
        appending to one shared file loses lines where appends are not
        atomic between processes (Windows)."""
        script = write(tmp_path / "hook.py", (
            "import json, sys, uuid\n"
            "from pathlib import Path\n"
            "event = json.loads(sys.stdin.buffer.read().decode('utf-8'))\n"
            "Path(sys.argv[1], uuid.uuid4().hex).write_text(event['kind'], encoding='utf-8')\n"
        ))
        out = tmp_path / "calls"
        out.mkdir()

        def calls() -> list[str]:
            return [call.read_text(encoding="utf-8") for call in out.iterdir()]

        check_observer(CommandObserver([PY, str(script), str(out)]), seen=calls, threads=2)


@pytest.fixture()
def shop_script(tmp_path: Path) -> Path:
    path = tmp_path / "shop tools" / "shop_runner.py"
    path.parent.mkdir()
    path.write_text(SHOP_RUNNER, encoding="utf-8")
    return path


class TestReferenceRunnersAgainstTheFake:
    def test_demo(self):
        ctx = check_runner(DemoRunner(seed="kit"), expect="complete")
        assert ctx.declared == ("1",) and len(ctx.verdicts) == 3

    def test_command_complete(self, shop_script, monkeypatch):
        monkeypatch.setenv("SHOP_FAILING", CART[1])
        runner = CommandRunner(command=[PY, str(shop_script), "{ids_file}", "{out_dir}"], shards=2)
        ctx = check_runner(runner, FakeDispatchContext(CART), expect="complete")
        assert ctx.summary() == {"PASS": 2, "FAIL": 1}
        assert set(ctx.declared or ()) == {"1", "2"}

    def test_command_refuses_on_drift(self, shop_script):
        runner = CommandRunner(command=[PY, str(shop_script), "{ids_file}", "{out_dir}"])
        drifted = kit.ListSource([Item(id=CART[0], tags=("Cart",))])  # the corpus lost two tests
        spec_ctx = FakeDispatchContext(CART)
        ctx = FakeDispatchContext(spec=spec_ctx.spec, source=drifted)
        check_runner(runner, ctx, expect="refused")
        assert "drift" in (ctx.refusal or "")

    def test_command_cancel(self, shop_script, tmp_path, monkeypatch):
        block = tmp_path / "blocked.pid"
        monkeypatch.setenv("SHOP_BLOCK", str(block))
        runner = CommandRunner(command=[PY, str(shop_script), "{ids_file}", "{out_dir}"])
        ctx = check_runner(runner, FakeDispatchContext(CART), cancel_after_declare=True, expect="cancelled",
                           timeout=30)
        assert ctx.outstanding == ["1"]
        # The kit cancels right after the hand-off, which may reach the shard's
        # worker before it has started the command — then nothing was started.
        # A cancel that meets the command running: test_command_runner.py::TestCancel.
        if block.exists():
            wait_until(lambda: process_gone(int(block.read_text(encoding="utf-8"))), timeout=10,
                       what="the cancelled command to be gone")

    def test_jenkins_poll(self):
        fake = FakeJenkins()
        try:
            fake.add_job(JOB, junit_job({CART[2]}))
            runner = JenkinsRunner(base_url=fake.url, job=JOB, poll_interval_s=0.01, retry_backoff_s=0.01, **POLL)
            ctx = check_runner(runner, FakeDispatchContext(CART), expect="complete")
            assert ctx.summary() == {"PASS": 2, "FAIL": 1}
            assert any(ref.name == "Jenkins build" for ref in ctx.artifacts)
        finally:
            fake.close()

    def test_jenkins_callback_hands_off_and_awaits(self):
        fake = FakeJenkins()
        try:
            fake.add_job(JOB)
            runner = JenkinsRunner(base_url=fake.url, job=JOB, callback_base="http://runcomposer.shop.example:8100",
                                   poll_interval_s=0.01, retry_backoff_s=0.01)
            ctx = check_runner(runner, FakeDispatchContext(CART), expect="awaiting")
            assert ctx.outstanding == list(ctx.declared or ())
            assert ctx.exported is not None and ctx.tokens  # the spec and a token went to the job
            assert check_runner(runner).declared  # and the defaults work too
        finally:
            fake.close()

    def test_jenkins_poll_cancel_stops_the_build(self):
        fake = FakeJenkins()
        try:
            started = threading.Event()

            def endless(build: Any) -> None:
                started.set()
                build.stop.wait(60)

            fake.add_job(JOB, endless)
            runner = JenkinsRunner(base_url=fake.url, job=JOB, poll_interval_s=0.01, retry_backoff_s=0.01, **POLL)
            check_runner(runner, FakeDispatchContext(CART), cancel_after_declare=True, timeout=30,
                         expect="cancelled")
            assert fake.posts("/stop"), "cancel stopped the build"
        finally:
            fake.close()

    def test_jenkins_health_probes_the_server(self):
        fake = FakeJenkins()
        try:
            fake.add_job(JOB)
            runner = JenkinsRunner(base_url=fake.url, job=JOB, callback_base="http://x.shop.example")
            assert runner.health().status == "ok"
        finally:
            fake.close()

    @needs_robot
    def test_robot_pool(self, tmp_path):
        from runcomposer.plugins.robot_pool import RobotPoolRunner
        from runcomposer.plugins.robot_source import RobotFrameworkSource

        source = RobotFrameworkSource(root=str(ROBOT_SHOP))
        chosen = ["Tests.Payments.Visa Payment Succeeds", "Tests.Payments.Expired Card Is Rejected Loudly"]
        items = [item for item in source.items() if item.id in chosen]
        spec = FakeDispatchContext(items=items, source=source, expect_format="robot-output-xml").spec
        ctx = FakeDispatchContext(spec=spec, source=source, expect_format="robot-output-xml")
        runner = RobotPoolRunner(suite_root=str(ROBOT_SHOP), max_workers=2)
        check_runner(runner, ctx, expect="complete", timeout=180)
        assert ctx.summary() == {"PASS": 1, "FAIL": 1}
        assert {ref.name.split(" ")[0] for ref in ctx.artifacts} >= {"output.xml", "log.html"}
        assert ctx.live_updates == [] or all(v.item_id in chosen for v in ctx.live_updates)


class TestReferenceRunnersThroughTheHarness:
    def test_demo_end_to_end(self):
        with ConnectorHarness(runners={"demo": {}}) as harness:
            result = harness.run("Cart", runner="demo", parameters={"seed": "kit"})
            assert result.state == "COMPLETE" and result.completion in ("PASS", "FAIL")
            assert result.event_kinds[0] == "run.composed" and result.event_kinds[-1] == "run.completed"
            assert len(result.verdicts) == result.run.planned_count

    def test_command_end_to_end_with_its_own_catalog(self, shop_script):
        items = [{"id": f"shop.checkout.{n}", "tags": ["Checkout"]} for n in range(4)]
        with ConnectorHarness(
            items=items,
            runners={"shop": {"plugin": "command", "command": [PY, str(shop_script), "{ids_file}", "{out_dir}"],
                              "shards": 2}},
            env={"SHOP_FAILING": "shop.checkout.3"},
        ) as harness:
            result = harness.run("Checkout", runner="shop")
            assert (result.state, result.completion) == ("COMPLETE", "FAIL")
            assert result.summary == {"PASS": 3, "FAIL": 1}
            assert {a.shard for a in result.artifacts} >= {"1", "2"}

    def test_a_refusal_is_reported_not_raised(self, shop_script):
        with ConnectorHarness(runners={"refusing": {"module": "tests.test_testing_kit:RefusingRunner"}}) as harness:
            result = harness.run("Cart", runner="refusing")
            assert result.refusal == "the staging shop is closed"
            assert result.state == "COMPOSED" and result.dispatch is None
            assert "dispatch.refused" in result.event_kinds

    def test_a_runner_that_does_not_return_is_cancelled_and_reported(self):
        with ConnectorHarness(runners={"stuck": {"module": "tests.test_testing_kit:StuckRunner"}}) as harness:
            with pytest.raises(AssertionError, match=r"did not return within 0\.3s"):
                harness.run("Cart", runner="stuck", timeout=0.3)

    def test_startup_validation_happens_when_the_harness_is_built(self):
        from runcomposer.sdk import ConfigError

        with pytest.raises(ConfigError, match="unknown option"):
            ConnectorHarness(runners={"demo": {"sede": "typo"}})

    @needs_robot
    def test_robot_pool_end_to_end(self):
        with ConnectorHarness(
            sources={"shop": {"plugin": "robotframework", "root": str(ROBOT_SHOP)}},
            runners={"pool": {"plugin": "robot-pool", "suite_root": str(ROBOT_SHOP), "max_workers": 2}},
        ) as harness:
            result = harness.run({"item_ids": ["Tests.Payments.Visa Payment Succeeds"]}, runner="pool",
                                 timeout=180)
            assert (result.state, result.completion) == ("COMPLETE", "PASS")

    def test_jenkins_callback_round_trip_through_ingest(self, tmp_path):
        fake = FakeJenkins()
        try:
            fake.add_job(JOB)
            with ConnectorHarness(runners={"ci": {
                "plugin": "jenkins", "base_url": fake.url, "job": JOB,
                "callback_base": "http://runcomposer.shop.example:8100", "poll_interval_s": 0.01,
                "retry_backoff_s": 0.01,
            }}) as harness:
                handed = harness.run(CART, runner="ci")
                assert handed.state == "AWAITING_RESULTS"
                bundle = tmp_path / "bundle"
                write(bundle / "results.json", json.dumps({"format": "runcomposer-verdicts", "verdicts": [
                    {"name": item_id, "status": "PASS"} for item_id in CART]}))
                harness.ingest(bundle, run_id=handed.run_id, shard=handed.dispatch.shards[0])
                done = harness.wait(handed.run_id, timeout=30)
                assert (done.state, done.completion) == ("COMPLETE", "PASS")
        finally:
            fake.close()


class RefusingRunner(RunnerBase):
    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="refusing")

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.refuse("the staging shop is closed")


class StuckRunner(RunnerBase):
    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="stuck", capabilities=("cancel",))

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        wait_until(lambda: ctx.cancelled, timeout=60, what="a cancel")
        ctx.raise_if_cancelled()


# -- 2. broken toys are caught, each with the right rule ------------------------------


class _Source(SourceBase):
    provider_id = "toy"

    def __init__(self, items: list[Item] | None = None):
        self._items = items if items is not None else [Item(id=i, tags=("Cart",)) for i in CART]

    def items(self) -> list[Item]:
        return list(self._items)

    def snapshot(self) -> str:
        return "sha256:toy"

    def resolve(self, native_name: str) -> str | None:
        return native_name if native_name in {i.id for i in self._items} else None


class SourceThatLowercases(_Source):
    def resolve(self, native_name: str) -> str | None:
        return native_name.lower()


class SourceHidingDuplicates(_Source):
    def __init__(self) -> None:
        super().__init__([Item(id=CART[0]), Item(id=CART[0])])


class SourceWithMovingSnapshot(_Source):
    calls = 0

    def snapshot(self) -> str:
        type(self).calls += 1
        return f"sha256:{type(self).calls}"


class SourceWithoutBase:
    provider_id = "bare"

    def items(self) -> list[Item]:
        return []

    def snapshot(self) -> str:
        return "sha256:0"

    def resolve(self, native_name: str) -> str | None:
        return None


class ParserThatRaisesKeyError(ParserBase):
    format_id = "shop-lines"

    def parse(self, path: Any) -> list[ParsedVerdict]:
        text = Path(path).read_text(encoding="utf-8")
        name, status = text.split()  # crashes on anything else
        return [ParsedVerdict(name, {"ok": "PASS"}[status])]


class ParserInventingStatuses(ParserBase):
    format_id = "shop-lines"

    def parse(self, path: Any) -> list[ParsedVerdict]:
        return [ParsedVerdict(CART[0], "MISSING")]


class ParserWithAnImportPathForAnId(ParserBase):
    format_id = "shop_parsers:Lines"

    def parse(self, path: Any) -> list[ParsedVerdict]:
        return [ParsedVerdict(CART[0], "PASS")]


class _Runner(RunnerBase):
    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="toy")


class RunnerDeclaringTwice(_Runner):
    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        ctx.declare(["1"])


class RunnerDeliveringFirst(_Runner):
    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])
        ctx.declare(["1"])


class RunnerRefusingLate(_Runner):
    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        ctx.refuse("changed my mind")


class RunnerDeliveringAnUndeclaredShard(_Runner):
    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["us"])
        ctx.deliver("eu", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])


class RunnerReportingStrangers(_Runner):
    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        ctx.deliver("1", verdicts=[Verdict("Shop.Auth.SSO.T001", "PASS")])


class RunnerForgettingToDeclare(_Runner):
    def dispatch(self, ctx: DispatchContext) -> None:
        return None


class RunnerLeavingShardsOutstanding(_Runner):
    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1", "2"])
        ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])


class RunnerIgnoringCancel(_Runner):
    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        threading.Event().wait(5)


class RunnerDeliveringDespiteCancel(_Runner):
    """Declares cancel, never looks at ctx.cancelled, delivers everything."""

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="toy", capabilities=("cancel",))

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        threading.Event().wait(0.3)
        ctx.deliver("1", verdicts=[Verdict(i, "PASS") for i in ctx.item_ids])


class RunnerReportingHalf(_Runner):
    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        ctx.deliver("1", verdicts=[Verdict(ctx.item_ids[0], "PASS")])


class RunnerWithUnresolvedNames(_Runner):
    """Delivers a bundle whose names its catalog cannot resolve."""

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        bundle = ctx.workdir / "out"
        bundle.mkdir(exist_ok=True)
        (bundle / "results.json").write_text(json.dumps({"format": "runcomposer-verdicts", "verdicts": [
            {"name": i.replace(".", "::"), "status": "PASS"} for i in ctx.item_ids]}), encoding="utf-8")
        ctx.deliver("1", bundle=bundle)


class LinesParser(ParserBase):
    format_id = "shop-lines"

    def parse(self, path: Any) -> list[ParsedVerdict]:
        return [ParsedVerdict(*line.split()) for line in (Path(path) / "out.txt").read_text().splitlines()]


class RunnerWithItsOwnFormat(_Runner):
    """Delivers its native format without naming it: the run expects it."""

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="toy", result_format="shop-lines")

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        (ctx.workdir / "out.txt").write_text("".join(f"{i} PASS\n" for i in ctx.item_ids), encoding="utf-8")
        ctx.deliver("1", bundle=ctx.workdir)


class RunnerWithBadHealth(_Runner):
    def health(self) -> Health:
        return Health(status="fine")

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.refuse("never")


class RunnerRecordingLate(_Runner):
    """Records something after the delivery that completes the dispatch —
    in runcomposer it would arrive after ``run.completed``."""

    def __init__(self, what: str) -> None:
        self.what = what

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        ctx.deliver("1", verdicts=[Verdict(item_id, "PASS") for item_id in ctx.item_ids])
        if self.what == "artifact":
            ctx.add_artifact("report", "https://ci.shop.example/report")
        elif self.what == "link":
            ctx.set_link("build", "https://ci.shop.example/job/12")
        else:
            ctx.log("all shards are in")


class RunnerWithBlockingHealth(_Runner):
    """``health()`` waits on a service that does not answer."""

    def __init__(self) -> None:
        self.answered = threading.Event()

    def health(self) -> Health:
        self.answered.wait(timeout=30)
        return Health()

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.refuse("never")


class RunnerWithoutCancel:
    """Not derived from RunnerBase and lacking ``cancel``."""

    OPTIONS = None

    @classmethod
    def resolve_config_paths(cls, options: dict, resolve: Any) -> dict:
        return options

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="bare")

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.refuse("never")

    def health(self) -> Health:
        return Health()


class ObserverThatChokes(ObserverBase):
    def notify(self, event: Any) -> None:
        if event.kind == "run.stale":
            raise KeyError("stale is not a kind this observer knows")


class ObserverDroppingEvents(ObserverBase):
    def __init__(self) -> None:
        self.seen: list[Any] = []

    def notify(self, event: Any) -> None:
        if event.kind != "warning":
            self.seen.append(event)


class StoreThatForgetsTheShard(StoreBase):
    """The sqlite store with one bug: verdicts come back without their shard."""

    def __init__(self) -> None:
        self._inner = SqliteRunStore(":memory:")
        self.store_id = "forgetful"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def verdicts_for(self, *args: Any, **kwargs: Any) -> list[Verdict]:
        from dataclasses import replace

        return [replace(v, shard="") for v in self._inner.verdicts_for(*args, **kwargs)]


class StoreWithoutAnExclusiveClaim(StoreBase):
    """The sqlite store with the exclusive claim ignored."""

    def __init__(self) -> None:
        self._inner = SqliteRunStore(":memory:")
        self.store_id = "eager"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def add_dispatch(self, run_id: str, **kwargs: Any) -> Any:
        kwargs.pop("exclusive", None)
        return self._inner.add_dispatch(run_id, **kwargs)


def _forward_store_members(cls: type) -> None:
    from runcomposer.sdk import RunStore, contract_members

    for name in contract_members(RunStore):
        if name in vars(cls) or name == "store_id":
            continue

        def forwarded(self: Any, *args: Any, _name: str = name, **kwargs: Any) -> Any:
            return getattr(self._inner, _name)(*args, **kwargs)

        setattr(cls, name, forwarded)


_forward_store_members(StoreThatForgetsTheShard)
_forward_store_members(StoreWithoutAnExclusiveClaim)


def _parser_bundle(tmp_path: Path) -> Path:
    return write(tmp_path / "lines.txt", f"{CART[0]} ok")


BROKEN = [
    pytest.param(lambda tmp: check_source(SourceThatLowercases()), "[source.resolve-own-ids]", id="source-normalizes-ids"),
    pytest.param(lambda tmp: check_source(SourceHidingDuplicates()), "[source.duplicate-ids]", id="source-hides-duplicates"),
    pytest.param(lambda tmp: check_source(SourceWithMovingSnapshot()), "[source.snapshot-stable]", id="source-snapshot-moves"),
    pytest.param(lambda tmp: check_source(SourceWithoutBase()), "[source.contract]", id="source-missing-members"),
    pytest.param(lambda tmp: check_source(_Source(), native_names={"cart-1": CART[0]}), "[source.resolve-native]",
                 id="source-native-name-unresolved"),
    pytest.param(lambda tmp: check_parser(ParserThatRaisesKeyError(), _parser_bundle(tmp),
                                          bad_bundles=[write(tmp / "bad.txt", "garbage")]),
                 "[parser.bad-input-is-parse-error]", id="parser-crashes-on-bad-input"),
    pytest.param(lambda tmp: check_parser(ParserInventingStatuses(), _parser_bundle(tmp)), "[parser.status]",
                 id="parser-invents-a-status"),
    pytest.param(lambda tmp: check_parser(ParserWithAnImportPathForAnId(), _parser_bundle(tmp)), "[parser.format-id]",
                 id="parser-import-path-id"),
    pytest.param(lambda tmp: check_parser(VerdictsParser(), write(tmp / "r" / "results.json", json.dumps(
        {"format": "runcomposer-verdicts", "verdicts": [{"name": CART[0], "status": "PASS"}]})).parent,
        expected={CART[0]: "FAIL"}), "[parser.expected]", id="parser-wrong-result"),
    pytest.param(lambda tmp: check_runner(RunnerDeclaringTwice()), "[runner.order] dispatch", id="runner-declares-twice"),
    pytest.param(lambda tmp: check_runner(RunnerDeliveringFirst()), "[runner.order] deliver() before declare()",
                 id="runner-delivers-first"),
    pytest.param(lambda tmp: check_runner(RunnerRefusingLate()), "[runner.order] refused after the hand-off",
                 id="runner-refuses-late"),
    pytest.param(lambda tmp: check_runner(RunnerDeliveringAnUndeclaredShard()),
                 "[runner.delivers-declared-shards] delivered shard 'eu'", id="runner-undeclared-shard"),
    pytest.param(lambda tmp: check_runner(RunnerReportingStrangers()), "[runner.reports-planned-items]",
                 id="runner-reports-unplanned-items"),
    pytest.param(lambda tmp: check_runner(RunnerForgettingToDeclare()), "[runner.declares]", id="runner-never-declares"),
    pytest.param(lambda tmp: check_runner(RunnerLeavingShardsOutstanding()), "[runner.external-delivery]",
                 id="runner-outstanding-without-capability"),
    pytest.param(lambda tmp: check_runner(RunnerIgnoringCancel(), cancel_after_declare=True, cancel_within=0.5),
                 "[runner.cancel] dispatch() did not return", id="runner-ignores-cancel"),
    pytest.param(lambda tmp: check_runner(RunnerDeliveringDespiteCancel(), cancel_after_declare=True),
                 "[runner.cancel] every declared shard was delivered after the cancellation",
                 id="runner-delivers-despite-cancel"),
    pytest.param(lambda tmp: check_runner(RunnerReportingHalf()), "[runner.reports-every-item]",
                 id="runner-leaves-items-unreported"),
    pytest.param(lambda tmp: check_runner(RunnerWithUnresolvedNames()), "[runner.correlates]",
                 id="runner-names-do-not-resolve"),
    pytest.param(lambda tmp: check_runner(RunnerWithBadHealth()), "[runner.health]", id="runner-bad-health"),
    pytest.param(lambda tmp: check_runner(RunnerWithBlockingHealth(), health_within=0.2),
                 "[runner.health-bounded] health() did not answer within 0.2s", id="runner-health-blocks"),
    pytest.param(lambda tmp: check_runner(RunnerRecordingLate("artifact")),
                 "[runner.records-before-last-delivery]", id="runner-artifact-after-last-delivery"),
    pytest.param(lambda tmp: check_runner(RunnerRecordingLate("link")),
                 "[runner.records-before-last-delivery]", id="runner-link-after-last-delivery"),
    pytest.param(lambda tmp: check_runner(RunnerRecordingLate("log")),
                 "[runner.records-before-last-delivery]", id="runner-log-after-last-delivery"),
    pytest.param(lambda tmp: check_runner(RunnerWithoutCancel()), "[runner.contract]", id="runner-missing-cancel"),
    pytest.param(lambda tmp: check_runner(DemoRunner(), expect="refused"), "[runner.expected-outcome]",
                 id="runner-unexpected-outcome"),
    pytest.param(lambda tmp: check_observer(ObserverThatChokes()), "[observer.accepts-every-kind]",
                 id="observer-chokes-on-a-kind"),
    pytest.param(lambda tmp: (lambda o: check_observer(o, seen=lambda: o.seen))(ObserverDroppingEvents()),
                 "[observer.delivers-each]", id="observer-drops-events"),
    pytest.param(lambda tmp: check_store(StoreThatForgetsTheShard), "[store.verdict-shard]", id="store-forgets-the-shard"),
    pytest.param(lambda tmp: check_store(StoreWithoutAnExclusiveClaim), "[store.exclusive-dispatch]",
                 id="store-without-exclusive-claim"),
]


@pytest.mark.parametrize(("call", "rule"), BROKEN)
def test_a_broken_implementation_is_caught_with_its_rule(call, rule, tmp_path):
    with pytest.raises(AssertionError) as caught:
        call(tmp_path)
    assert str(caught.value).startswith(rule), str(caught.value)


class TestSampleEvents:
    """``sample_events()`` is every kind an observer may be told about,
    taken from the one list of kinds, so that the two cannot drift apart."""

    def test_the_sample_holds_every_kind_of_the_event_stream_once(self):
        from runcomposer.core import events

        assert [event.kind for event in sample_events()] == list(events.ALL_KINDS)

    def test_the_list_of_kinds_is_every_kind_the_events_module_defines(self):
        from runcomposer.core import events

        defined = {
            value for name, value in vars(events).items()
            if name.isupper() and isinstance(value, str)
        }
        assert set(events.ALL_KINDS) == defined and len(events.ALL_KINDS) == len(defined)
        assert {"run.cancel_requested", "delivery.attached"} <= defined

    def test_an_observer_filtering_on_run_kinds_is_held_to_the_cancel_request_too(self):
        class RunKindsOnly(ObserverBase):
            def __init__(self) -> None:
                self.seen: list[Any] = []

            def notify(self, event: Any) -> None:
                if event.kind.startswith("run.") and event.kind != "run.cancel_requested":
                    self.seen.append(event)

        observer = RunKindsOnly()
        with pytest.raises(AssertionError, match=r"^\[observer.delivers-each\]"):
            check_observer(observer, seen=lambda: observer.seen, kinds=["run.*"])


class TestTheFakeNotesLateRecords:
    def test_nothing_recorded_before_the_last_delivery_is_late(self):
        ctx = FakeDispatchContext(CART)
        ctx.declare(["1", "2"])
        ctx.deliver("1", verdicts=[Verdict(CART[0], "PASS")])
        ctx.add_artifact("log (1)", "https://ci.shop.example/1/log")
        ctx.log("shard 2 still running")
        ctx.deliver("2", verdicts=[Verdict(item_id, "PASS") for item_id in CART[1:]])
        assert ctx.late_records == []
        ctx.set_link("portal", "https://ci.shop.example/run")
        assert ctx.late_records == ["link portal"]


# -- the fake enforces the order the real context enforces --------------------------


class TestFakeAndRealAgree:
    """Each misuse raises the same exception, with the same message, from the
    fake and from runcomposer's own context."""

    MISUSES: ClassVar[dict[str, Any]] = {
        "declare-twice": lambda ctx: (ctx.declare(["1"]), ctx.declare(["1"])),
        "deliver-first": lambda ctx: ctx.deliver("1", verdicts=[]),
        "refuse-late": lambda ctx: (ctx.declare(["1"]), ctx.refuse("too late")),
        "one-string": lambda ctx: ctx.declare("1"),
        "duplicate-names": lambda ctx: ctx.declare(["1", "1"]),
        "both-payloads": lambda ctx: (ctx.declare(["1"]), ctx.deliver("1", verdicts=[], bundle="x")),
        "missing-status": lambda ctx: (ctx.declare(["1"]), ctx.deliver("1", verdicts=[Verdict(ctx.item_ids[0], "MISSING")])),
    }

    @pytest.mark.parametrize("misuse", sorted(MISUSES))
    def test_same_error_same_message(self, misuse, make_service):
        def capture(ctx: Any) -> tuple[str, str]:
            try:
                self.MISUSES[misuse](ctx)
            except Exception as exc:
                return type(exc).__name__, str(exc).replace(ctx.dispatch_id, "<dispatch>")
            raise AssertionError(f"{misuse}: no error")

        fake = capture(FakeDispatchContext(CART))
        seen: list[tuple[str, str]] = []
        MISUSE_SCRIPT["fn"] = lambda ctx: seen.append(capture(ctx))
        service = make_service(runners={"misuse": {"module": "tests.test_testing_kit:MisuseRunner"}})
        run_id = service.compose_run({"item_ids": CART}, title="t", origin="test").run.id
        service.dispatch(run_id, "misuse", foreground=True)
        assert seen == [fake]


MISUSE_SCRIPT: dict[str, Any] = {}


class MisuseRunner(RunnerBase):
    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="misuse")

    def dispatch(self, ctx: DispatchContext) -> None:
        MISUSE_SCRIPT["fn"](ctx)


class TestFakeRecords:
    def test_it_records_everything_a_runner_did(self, tmp_path):
        class Busy(RunnerBase):
            def describe(self) -> RunnerInfo:
                return RunnerInfo(id="busy", capabilities=("live_status",))

            def dispatch(self, ctx: DispatchContext) -> None:
                assert ctx.history.durations(labels={"suite": "nightly"}) == {CART[0]: 120.0}
                ctx.declare({"a": CART[:2], "b": CART[2:]}, plan="two shards", links={"build": "https://ci.shop.example/1/"})
                ctx.live("a", CART[0], "PASS", duration_ms=3)
                ctx.live("a", "not-planned", "PASS")  # ignored, as in runcomposer
                ctx.add_artifact("log.html", ctx.workdir / "log.html", shard="a")
                ctx.log("halfway", level="info")
                assert ctx.deliver("a", verdicts=[Verdict(CART[0], "PASS"), Verdict(CART[1], "FAIL")]) == "new"
                assert ctx.deliver("a", verdicts=[Verdict(CART[0], "PASS"), Verdict(CART[1], "FAIL")]) == "duplicate"
                assert ctx.deliver("a", verdicts=[Verdict(CART[0], "PASS"), Verdict(CART[1], "PASS")]) == "replaced"
                ctx.export_spec()
                assert ctx.ingest_token()

        ctx = FakeDispatchContext(CART, durations={CART[0]: 120.0})
        assert ctx.run(Busy()) == "awaiting"
        assert ctx.declared == ("a", "b") and ctx.shard_items == {"a": CART[:2], "b": CART[2:]}
        assert ctx.plan == "two shards" and dict(ctx.links) == {"build": "https://ci.shop.example/1/"}
        assert ctx.outstanding == ["b"] and ctx.summary() == {"PASS": 2}
        assert [d.outcome for d in ctx.deliveries] == ["new", "duplicate", "replaced"]
        # The whole stream stays readable after the shard delivered; what a
        # store would hold at this moment is `live_now`.
        assert [(v.shard, v.item_id, v.status) for v in ctx.live_updates] == [("a", CART[0], "PASS")]
        assert ctx.live_now == []  # replaced by shard a's delivery
        assert ctx.delivered_shards() == {"a"} and ctx.outstanding_shards() == ["b"]
        assert ctx.artifacts[0].media_type == "text/html" and ctx.artifacts[0].shard == "a"
        assert ("info", "halfway") in ctx.logs
        assert ctx.spec_sha256 and ctx.exported and json.loads(ctx.exported)["run"]["id"] == ctx.run_id
        assert ctx.history.calls == [{"labels": {"suite": "nightly"}, "last_n": 5, "statuses": None}]
        ctx.close()

    def test_live_status_from_a_worker_process_arrives(self, tmp_path):
        ctx = FakeDispatchContext(CART)
        channel = ctx.live_channel()
        code = (
            "import pickle, sys\n"
            "channel = pickle.loads(bytes.fromhex(sys.argv[1]))\n"
            f"channel.live('1', {CART[0]!r}, 'PASS', duration_ms=7)\n"
        )
        import pickle

        subprocess.run([PY, "-c", code, pickle.dumps(channel).hex()], check=True, timeout=60)
        assert [(v.shard, v.item_id, v.status, v.duration_ms) for v in ctx.live_updates] == [("1", CART[0], "PASS", 7)]
        ctx.close()

    def test_a_bundle_is_parsed_with_the_configured_parser_and_correlated(self, tmp_path):
        class Shouting(ParserBase):
            format_id = "runcomposer-verdicts"

            def parse(self, path: Any) -> list[ParsedVerdict]:
                return [ParsedVerdict(CART[0], "PASS", message="LOUD"), ParsedVerdict("unknown", "PASS")]

        ctx = FakeDispatchContext(CART, parsers={"runcomposer-verdicts": Shouting()})
        ctx.declare(["1"])
        bundle = tmp_path / "b"
        bundle.mkdir()
        assert ctx.deliver("1", bundle=bundle) == "new"
        assert [(v.item_id, v.message, v.shard) for v in ctx.verdicts] == [(CART[0], "LOUD", "1")]
        assert any("unknown native name 'unknown'" in w for w in ctx.warnings())

    def test_a_bad_bundle_is_refused_as_in_runcomposer(self, tmp_path):
        from runcomposer.sdk import IngestError

        class Broken(ParserBase):
            format_id = "runcomposer-verdicts"

            def parse(self, path: Any) -> list[ParsedVerdict]:
                raise ParseError("not a results file")

        ctx = FakeDispatchContext(CART, parsers={"runcomposer-verdicts": Broken()})
        ctx.declare(["1"])
        with pytest.raises(IngestError, match="not a results file"):
            ctx.deliver("1", bundle=tmp_path)


def test_a_partial_runner_passes_when_partial_is_what_it_means():
    ctx = check_runner(RunnerReportingHalf(), allow_missing=True, expect="complete")
    assert ctx.summary() == {"PASS": 1}


def test_the_fake_expects_the_runner_s_own_result_format_like_runcomposer_does():
    """A run composed for a runner with a fixed result_format expects that
    format; deliver(bundle=…) without format= then parses it — in the fake
    as in runcomposer."""
    ctx = FakeDispatchContext(CART, parsers={"shop-lines": LinesParser()})
    assert ctx.run(RunnerWithItsOwnFormat()) == "complete"
    assert ctx.summary() == {"PASS": 3}
    assert ctx.spec["results"]["expect"] == [{"format": "shop-lines"}]
    check_runner(RunnerWithItsOwnFormat(), FakeDispatchContext(CART, parsers={"shop-lines": LinesParser()}),
                 expect="complete")
    # An explicit expectation is the caller's and is kept.
    explicit = FakeDispatchContext(CART, expect_format="runcomposer-verdicts")
    explicit.adopt_result_format("shop-lines")
    assert explicit.spec["results"]["expect"] == [{"format": "runcomposer-verdicts"}]
