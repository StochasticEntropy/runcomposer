"""Dispatch visibility and dispatch failure semantics (DESIGN.md §4, §6.2).

A dispatch is the record of a hand-off to an executor, so it has to exist
*while* the executor executes — not only once ``dispatch()`` has returned.
Runners that block for the whole execution take the ``DispatchReservation``
(optional ``bind_dispatch`` hook) and record the hand-off at the moment they
make it; runners that implement only ``describe()``/``dispatch()`` keep
working exactly as before, with the dispatch recorded from their handle.

The fake runners here are wired the way a third-party connector is wired: an
import path in the config (`module: "<module>:<Class>"`, ADOPTING.md §5), with
their coordination primitives passed as ordinary runner options.
"""

import threading
import time
from pathlib import Path

import pytest

from runcomposer.config import Config
from runcomposer.core.ports import DispatchHandle, DispatchRefused, RunnerInfo
from runcomposer.service import Service, ServiceError

THIS_MODULE = Path(__file__).stem
CORPUS = Path(__file__).parent.parent / "examples" / "robot-shop" / "tests"


# -- fake runners (the Runner port, no framework involved) --------------------


class LegacyRunner:
    """The minimum published contract: describe + dispatch, own dispatch id.
    Third-party connectors written against ADOPTING.md §4 look like this."""

    runner_id = "legacy"

    def __init__(self, handed_off=None, release=None, **_options):
        self._handed_off = handed_off
        self._release = release

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id=self.runner_id)

    def dispatch(self, spec):
        if self._handed_off is not None:
            self._handed_off.set()
            self._release.wait(timeout=30)
        return DispatchHandle(dispatch_id="LEGACY-MINTED-ID", shards=2, spec_sha256="ab" * 32)


class HandOffRunner:
    """A blocking runner that takes the reservation, like ``robot-pool``:
    everything refusable is checked first, then the hand-off is recorded, then
    the work runs (here: blocks until the test releases it)."""

    runner_id = "handoff"

    def __init__(
        self,
        handed_off=None,
        release=None,
        refuse_before=False,
        fail_after=False,
        record_twice=False,
        foreign_handle_id=False,
        shards_at_hand_off=1,
        shards_in_handle=1,
        **_options,
    ):
        self._handed_off = handed_off
        self._release = release
        self._refuse_before = refuse_before
        self._fail_after = fail_after
        self._record_twice = record_twice
        self._foreign_handle_id = foreign_handle_id
        self._shards_at_hand_off = shards_at_hand_off
        self._shards_in_handle = shards_in_handle
        self._reservation = None

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id=self.runner_id, capabilities=("live_status",))

    def bind_dispatch(self, reservation) -> None:
        self._reservation = reservation

    def dispatch(self, spec):
        if self._refuse_before:
            raise DispatchRefused("nothing was handed over")
        self._reservation.record(shards=self._shards_at_hand_off)
        if self._record_twice:
            self._reservation.record(shards=self._shards_at_hand_off)
        if self._handed_off is not None:
            self._handed_off.set()
            self._release.wait(timeout=30)
        if self._fail_after:
            raise RuntimeError("executor died mid-run")
        return DispatchHandle(
            dispatch_id="FOREIGN-ID" if self._foreign_handle_id else self._reservation.dispatch_id,
            shards=self._shards_in_handle,
        )


# -- fixtures -----------------------------------------------------------------


def service_with(tmp_path, runner_id: str, cls: str, **runner_options) -> Service:
    return Service(
        Config(
            data={
                "store": {"sqlite": {"path": str(tmp_path / "runs.db")}},
                "core": {
                    "artifact_dir": str(tmp_path / "artifacts"),
                    "ingestion": {"inbox": None, "quarantine_dir": str(tmp_path / "q")},
                },
                "runners": {runner_id: {"module": f"{THIS_MODULE}:{cls}", **runner_options}},
            }
        )
    )


def composed(service: Service) -> str:
    return service.compose_run({"tag_filter": "Smoke"}, title="Dispatch record", origin="test").run.id


def dispatch_in_background(service: Service, run_id: str, runner_id: str) -> threading.Thread:
    worker = threading.Thread(target=service.dispatch_runner, args=(run_id, runner_id))
    worker.daemon = True
    worker.start()
    return worker


class TestInFlightVisibility:
    def test_blocking_runner_shows_its_dispatch_while_it_executes(self, tmp_path):
        """The regression: an executing run must already carry its dispatch."""
        handed_off, release = threading.Event(), threading.Event()
        service = service_with(
            tmp_path, "handoff", "HandOffRunner", handed_off=handed_off, release=release
        )
        run_id = composed(service)
        worker = dispatch_in_background(service, run_id, "handoff")
        assert handed_off.wait(timeout=30), "runner never reached its hand-off"

        in_flight = service.store.get_run(run_id)  # the runner is still inside dispatch()
        assert worker.is_alive()
        assert len(in_flight.dispatches) == 1
        assert in_flight.dispatches[-1].mode == "handoff"
        assert in_flight.dispatches[-1].declared_shards == 1

        release.set()
        worker.join(timeout=30)
        after = service.store.get_run(run_id)
        assert [d.dispatch_id for d in after.dispatches] == [
            d.dispatch_id for d in in_flight.dispatches
        ]  # settled onto the same record, not a second one

    def test_runner_without_the_hook_is_recorded_on_return_as_before(self, tmp_path):
        """The documented degradation for runners that only describe+dispatch:
        the dispatch appears when execution is over. Unchanged behaviour."""
        handed_off, release = threading.Event(), threading.Event()
        service = service_with(
            tmp_path, "legacy", "LegacyRunner", handed_off=handed_off, release=release
        )
        run_id = composed(service)
        worker = dispatch_in_background(service, run_id, "legacy")
        assert handed_off.wait(timeout=30)
        assert service.store.get_run(run_id).dispatches == ()
        release.set()
        worker.join(timeout=30)
        recorded = service.store.get_run(run_id).dispatches[-1]
        assert recorded.dispatch_id == "LEGACY-MINTED-ID"
        assert (recorded.mode, recorded.declared_shards, recorded.spec_sha256) == (
            "legacy",
            2,
            "ab" * 32,
        )


class TestDispatchFailureSemantics:
    def test_refusal_before_the_hand_off_leaves_no_dispatch(self, tmp_path):
        service = service_with(tmp_path, "handoff", "HandOffRunner", refuse_before=True)
        run_id = composed(service)
        with pytest.raises(DispatchRefused, match="nothing was handed over"):
            service.dispatch_runner(run_id, "handoff")
        run = service.store.get_run(run_id)
        assert run.dispatches == ()  # no hand-off, no dispatch
        assert run.state == "COMPOSED"

    def test_failure_after_the_hand_off_keeps_the_dispatch(self, tmp_path):
        service = service_with(tmp_path, "handoff", "HandOffRunner", fail_after=True)
        run_id = composed(service)
        with pytest.raises(RuntimeError, match="executor died"):
            service.dispatch_runner(run_id, "handoff")
        run = service.store.get_run(run_id)
        assert len(run.dispatches) == 1  # the hand-off is a fact; it stays
        assert run.state == "AWAITING_RESULTS"

    def test_recording_the_same_dispatch_twice_is_refused(self, tmp_path):
        service = service_with(tmp_path, "handoff", "HandOffRunner", record_twice=True)
        run_id = composed(service)
        with pytest.raises(ServiceError, match="already recorded"):
            service.dispatch_runner(run_id, "handoff")

    def test_handle_must_carry_the_reserved_dispatch_id(self, tmp_path):
        service = service_with(tmp_path, "handoff", "HandOffRunner", foreign_handle_id=True)
        run_id = composed(service)
        with pytest.raises(ServiceError, match="reserved id IS the dispatch id"):
            service.dispatch_runner(run_id, "handoff")


class TestDeclarationRefinement:
    def test_handle_refines_the_declaration_made_at_hand_off(self, tmp_path):
        """A runner may record the hand-off before it knows its shard count;
        the returned handle is the final declaration and updates the same
        record (§4/§6.3) — it never adds a second dispatch."""
        service = service_with(
            tmp_path,
            "handoff",
            "HandOffRunner",
            shards_at_hand_off=None,
            shards_in_handle=4,
        )
        run_id = composed(service)
        dispatch = service.dispatch_runner(run_id, "handoff")
        dispatches = service.store.get_run(run_id).dispatches
        assert len(dispatches) == 1
        assert dispatches[-1].dispatch_id == dispatch.dispatch_id
        assert dispatches[-1].declared_shards == 4
        assert dispatches[-1].created_at == dispatch.created_at


class TestRobotPoolInFlight:
    """The reported symptom, end to end: a `robot-pool` run executes inside
    dispatch(), so its dispatch used to appear only after the run finished —
    the UI showed a RUNNING run with "Dispatches: none yet"."""

    def test_dispatch_is_visible_while_the_pool_is_running(self, tmp_path, slow_lane_suite):
        pytest.importorskip("robot", reason="requires the runcomposer[robot] extra")
        config = Config(
            data={
                "store": {"sqlite": {"path": str(tmp_path / "robot.db")}},
                "core": {
                    "artifact_dir": str(tmp_path / "artifacts"),
                    "ingestion": {"inbox": None, "quarantine_dir": str(tmp_path / "q")},
                },
                "sources": {"robotframework": {"root": str(slow_lane_suite)}},
                "runners": {
                    "robot-pool": {"suite_root": str(slow_lane_suite), "max_workers": 2}
                },
            }
        )
        service = Service(config)
        result = service.compose_run(
            {"tag_filter": "SlowLane"},  # the fixture's deliberately sleeping pair
            title="In-flight dispatch",
            origin="test",
            runner_section={"robot-pool": config.runner_options("robot-pool")},
        )
        assert result.items, "corpus has no SlowLane items to keep the run in flight"
        worker = dispatch_in_background(service, result.run.id, "robot-pool")

        observed = None
        deadline = time.time() + 60
        while time.time() < deadline and worker.is_alive():
            run = service.store.get_run(result.run.id)
            if run.state == "RUNNING":
                observed = run
                break
            time.sleep(0.1)
        assert observed is not None, "never observed the run in RUNNING"
        assert observed.dispatches, "run is RUNNING but carries no dispatch record"
        in_flight_id = observed.dispatches[-1].dispatch_id
        assert observed.dispatches[-1].mode == "robot-pool"
        # the live verdicts belong to exactly that dispatch, so the run detail
        # view (which reads verdicts of the latest dispatch) shows progress
        worker.join(timeout=120)
        final = service.store.get_run(result.run.id)
        assert [d.dispatch_id for d in final.dispatches] == [in_flight_id]
        assert final.state == "COMPLETE"
        assert service.store.verdicts_for(final.id, in_flight_id)
