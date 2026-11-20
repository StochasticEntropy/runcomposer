"""``ConnectorHarness`` — a real runcomposer around your plugin, in a
temporary directory, driven synchronously.

```python
from runcomposer.testing import ConnectorHarness

with ConnectorHarness(runners={"shop": {"plugin": "shop-http"}}) as harness:
    result = harness.run("Checkout", runner="shop", parameters={"region": "eu"})
    assert result.state == "COMPLETE" and result.completion == "PASS"
    assert "run.completed" in result.event_kinds
```

Nothing is simulated: the harness writes a config file, builds a ``Service``
from it (so your plugin's options are validated at startup exactly as
``runcomposer serve`` would), composes a run against the catalog, dispatches
it on the dispatch manager and waits — with a timeout — until the runner's
``dispatch`` has returned and the run has settled. What you read back comes
out of the sqlite store.

Two more ways to drive it, for runners that cannot be tested by waiting for
``dispatch`` to return:

- ``harness.start(...)`` dispatches in the background and returns a
  :class:`DispatchHandle` — wait for a condition on the run
  (``handle.wait_until(lambda run: run.live)``), then cancel, or let it end;
- ``ConnectorHarness(serve=True, ...)`` also serves the HTTP API on a free
  local port (``harness.url``, ``harness.api``), for a connector whose
  executor pushes results or progress itself. The address is known before
  the plugins are built: reference it in an entry as
  ``${env:RUNCOMPOSER_HARNESS_URL}`` (a ``callback_base``, say).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from runcomposer.app.config import load_config
from runcomposer.app.service import Service
from runcomposer.core.lifecycle import TERMINAL_STATES
from runcomposer.core.model import (
    ArtifactRef,
    DispatchRecord,
    Item,
    LiveStatus,
    RunEvent,
    RunRecord,
    Verdict,
)

__all__ = ["ConnectorHarness", "DispatchHandle", "HarnessRun", "plugin"]

#: Set while a serving harness is open: its base URL, for ``${env:…}``
#: references in the plugin entries it is given.
HARNESS_URL_VARIABLE = "RUNCOMPOSER_HARNESS_URL"


@dataclass
class HarnessRun:
    """One run as the harness left it: read back from the store."""

    run: RunRecord
    dispatch: DispatchRecord | None
    verdicts: list[Verdict]
    live: list[LiveStatus]
    events: list[RunEvent]
    artifacts: list[ArtifactRef]
    #: The refusal reason when the runner refused (or raised) before declaring.
    refusal: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def run_id(self) -> str:
        return self.run.id

    @property
    def state(self) -> str:
        return self.run.state

    @property
    def completion(self) -> str | None:
        return self.run.completion

    @property
    def summary(self) -> dict[str, int]:
        return dict(self.dispatch.summary) if self.dispatch is not None else {}

    @property
    def event_kinds(self) -> list[str]:
        return [event.kind for event in self.events]

    @property
    def links(self) -> dict[str, str]:
        """The dispatch's links — given to ``declare`` or set later."""
        return dict(self.dispatch.links) if self.dispatch is not None else {}

    def logs(self, level: str | None = None) -> list[str]:
        """The runner's ``ctx.log`` lines (``level``: info / warning / …)."""
        return [
            str(event.data.get("message", ""))
            for event in self.events
            if event.kind in ("log", "warning") and (level is None or event.data.get("level") == level)
        ]


_SECTIONS = {
    "sources": "runcomposer.sources",
    "runners": "runcomposer.runners",
    "parsers": "runcomposer.parsers",
    "observers": "runcomposer.observers",
    "store": "runcomposer.stores",
}


def plugin(section: str, name: str, **options: Any) -> Any:
    """Build an installed plugin by the name a config file gives it —
    ``plugin("sources", "robotframework", root="suites")``,
    ``plugin("parsers", "junit-xml")`` — without importing the module it
    lives in. For a test that needs a reference plugin beside your own: the
    Robot source as the catalog of ``FakeDispatchContext.from_source``, say.
    ``section`` is the config section (``sources``, ``runners``, ``parsers``,
    ``observers``, ``store``); ``options`` are the entry's options, with
    paths as you give them (nothing is anchored to a config file here)."""
    from runcomposer.core.registry import load_entry_point

    if section not in _SECTIONS:
        raise ValueError(f"unknown section {section!r} (one of {sorted(_SECTIONS)})")
    return load_entry_point(_SECTIONS[section], name)(**options)


class DispatchHandle:
    """A dispatch started with :meth:`ConnectorHarness.start`: the runner is
    executing on the dispatch manager while the test goes on."""

    def __init__(self, harness: ConnectorHarness, run_id: str, outcome: Any):
        self._harness = harness
        self.run_id = run_id
        self.dispatch_id: str = outcome.dispatch_id
        self._outcome = outcome

    @property
    def done(self) -> bool:
        """Has the runner's ``dispatch`` returned (or refused)?"""
        return bool(self._outcome.done.is_set())

    @property
    def refusal(self) -> str | None:
        refusal: str | None = self._outcome.refusal
        return refusal

    def read(self) -> HarnessRun:
        result = self._harness.read(self.run_id)
        result.refusal = self._outcome.refusal
        return result

    def wait_until(
        self, predicate: Callable[[HarnessRun], object], timeout: float = 60.0
    ) -> HarnessRun:
        """Wait until ``predicate(run)`` is true for the run as the store has
        it, and return that reading — ``handle.wait_until(lambda run:
        run.live)`` for "a test has started", ``lambda run: "build" in
        run.links`` for "the build is known". Fails with ``AssertionError``
        after ``timeout`` seconds, saying where the run stood."""
        deadline = time.monotonic() + timeout
        while True:
            result = self.read()
            if predicate(result):
                return result
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"the condition was not met within {timeout:g}s — run {self.run_id} is "
                    f"{result.state}, runner {'returned' if self.done else 'still executing'}; "
                    f"last events: {result.event_kinds[-5:]}"
                )
            # Wakes at once when the runner returns; otherwise looks again shortly.
            self._outcome.done.wait(0.05)

    def wait(self, timeout: float = 120.0) -> HarnessRun:
        """Wait until the runner's ``dispatch`` has returned and the dispatch
        has settled, as :meth:`ConnectorHarness.dispatch` does."""
        if not self._outcome.done.wait(timeout):
            self.cancel()
            self._outcome.done.wait(30)
            raise AssertionError(
                f"the runner did not return within {timeout:g}s — the dispatch was cancelled"
            )
        return self.read()

    def cancel(self) -> DispatchRecord:
        """Cancel the dispatch the way a person would (``runcomposer cancel``)."""
        return self._harness.cancel(self.run_id)


class ConnectorHarness:
    """A real runcomposer deployment in a temporary directory.

    The plugin sections take exactly what a config file would (``runners``,
    ``sources``, ``parsers``, ``observers``, ``connectors``, ``core``). By
    default the catalog is the bundled web-shop demo corpus; pass ``items``
    (``Item`` objects or ``{"id", "tags"}`` mappings) to use a manifest of
    your own instead, or ``sources`` for your own source plugin. The store
    is a sqlite file in the directory, the file-drop inbox is off.

    ``env`` sets environment variables for ``${env:…}`` references while the
    harness is open (and restores them on close).

    ``serve=True`` also serves the HTTP API of this deployment on a free
    port of 127.0.0.1 (``host=`` for another interface): ``harness.url`` is
    its base address, ``harness.api`` the API root. The address exists before
    the plugins are built — an entry refers to it as
    ``${env:RUNCOMPOSER_HARNESS_URL}``.
    """

    __test__ = False

    def __init__(
        self,
        *,
        runners: Mapping[str, Any] | None = None,
        sources: Mapping[str, Any] | None = None,
        items: Sequence[Item | Mapping[str, Any]] | None = None,
        parsers: Mapping[str, Any] | None = None,
        observers: Mapping[str, Any] | None = None,
        connectors: Mapping[str, Any] | None = None,
        core: Mapping[str, Any] | None = None,
        directory: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        serve: bool = False,
        host: str = "127.0.0.1",
    ):
        self.url: str | None = None
        self._socket: socket.socket | None = None
        self._server: Any = None
        self._server_thread: threading.Thread | None = None
        if serve:
            # Bound now, served later: the address is fixed before any plugin
            # is built, so a runner entry can carry it.
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._socket.bind((host, 0))
            self._socket.listen(128)
            shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
            self.url = f"http://{shown}:{self._socket.getsockname()[1]}"
            env = {**dict(env or {}), HARNESS_URL_VARIABLE: self.url}
        self._own_directory = directory is None
        self.directory = Path(directory) if directory is not None else Path(
            tempfile.mkdtemp(prefix="runcomposer-harness-")
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        self._saved_env: dict[str, str | None] = {}
        if env:
            for name, value in env.items():
                self._saved_env[name] = os.environ.get(name)
                os.environ[name] = value
        if items is not None and sources is None:
            manifest = self.directory / "catalog.json"
            manifest.write_text(json.dumps({"items": [_item_json(entry) for entry in items]}, indent=2), encoding="utf-8")
            sources = {"catalog": {"plugin": "manifest", "path": "catalog.json"}}
        core_section: dict[str, Any] = {"ingestion": {"inbox": None}}
        for key, value in (core or {}).items():
            if isinstance(value, Mapping) and isinstance(core_section.get(key), Mapping):
                core_section[key] = {**core_section[key], **value}
            else:
                core_section[key] = value
        config: dict[str, Any] = {
            "core": core_section,
            "store": {"sqlite": {"path": "runcomposer.db"}},
            "sources": dict(sources or {"demo-corpus": {}}),
            "runners": dict(runners or {}),
        }
        for name, section in (("parsers", parsers), ("observers", observers), ("connectors", connectors)):
            if section:
                config[name] = dict(section)
        self.config_path = self.directory / "config.yaml"
        self.config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        self.service = Service(load_config(str(self.config_path)))
        try:
            self.service.validate()  # the startup checks `serve` and `doctor` make
            if serve:
                self._serve()
        except BaseException:
            self.close()
            raise

    @property
    def api(self) -> str | None:
        """The API root of a serving harness (``…/api/v1``)."""
        return f"{self.url}/api/v1" if self.url else None

    def _serve(self) -> None:
        import uvicorn

        from runcomposer.http import create_app

        app = create_app(self.service.config, service=self.service)
        self._server = uvicorn.Server(
            uvicorn.Config(app, log_level="warning", lifespan="off", access_log=False)
        )
        self._server_thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._socket]},
            name="runcomposer-harness-http", daemon=True,
        )
        self._server_thread.start()
        deadline = time.monotonic() + 30
        while not self._server.started:
            if not self._server_thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("the harness could not start serving the HTTP API")
            time.sleep(0.01)

    # -- driving a run ----------------------------------------------------------------

    def compose(
        self,
        selection: Any,
        *,
        runner: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        title: str = "Harness run",
        labels: Mapping[str, str] | None = None,
    ) -> str:
        """Compose a stored run; returns its id. ``selection`` is a filter
        expression (``"Checkout"``, ``{"op": "AND", …}``), a list of item
        ids, or a whole selection mapping."""
        result = self.service.compose_run(
            self._selection(selection),
            title=title,
            labels=dict(labels) if labels else None,
            origin="harness",
            runner_id=runner,
            parameters=dict(parameters) if parameters is not None and runner else None,
        )
        assert result.run is not None
        return result.run.id

    @staticmethod
    def _selection(selection: Any) -> Mapping[str, Any]:
        if isinstance(selection, (list, tuple)):
            return {"item_ids": list(selection)}
        if not isinstance(selection, Mapping) or not (
            {"tag_filter", "item_ids", "history"} & set(selection)
        ):
            return {"tag_filter": selection}
        return selection

    def run(
        self,
        selection: Any,
        *,
        runner: str,
        parameters: Mapping[str, Any] | None = None,
        title: str = "Harness run",
        labels: Mapping[str, str] | None = None,
        timeout: float = 120.0,
    ) -> HarnessRun:
        """Compose → dispatch → wait. Returns once the runner's ``dispatch``
        has returned and the dispatch has settled (``COMPLETE``,
        ``AWAITING_RESULTS``, ``FAILED``, ``CANCELLED``, or refused). A runner
        that has not returned within ``timeout`` seconds is cancelled and the
        call fails with ``AssertionError``."""
        run_id = self.compose(selection, runner=runner, parameters=parameters, title=title, labels=labels)
        return self.dispatch(run_id, runner=runner, parameters=parameters, timeout=timeout)

    def start(
        self,
        selection: Any = None,
        *,
        runner: str,
        run_id: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        title: str = "Harness run",
        labels: Mapping[str, str] | None = None,
    ) -> DispatchHandle:
        """Compose (or take the stored ``run_id``) and dispatch in the
        background; returns at once with a :class:`DispatchHandle`."""
        if run_id is None:
            run_id = self.compose(
                selection, runner=runner, parameters=parameters, title=title, labels=labels
            )
        return DispatchHandle(self, run_id, self.service.dispatch(run_id, runner, parameters))

    def plan(
        self,
        selection: Any = None,
        *,
        runner: str,
        run_id: str | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> Any:
        """Ask the runner for its plan preview, as ``runcomposer plan`` does:
        for a selection (nothing is stored) or a stored ``run_id``. Returns
        the outcome — ``.preview`` (a ``PlanPreview``) or ``.refusal``."""
        if run_id is not None:
            return self.service.plan(runner, run_id=run_id, parameters=parameters)
        return self.service.plan(
            runner, selection=self._selection(selection), parameters=parameters
        )

    def dispatch(
        self,
        run_id: str,
        *,
        runner: str,
        parameters: Mapping[str, Any] | None = None,
        timeout: float = 120.0,
    ) -> HarnessRun:
        """Dispatch a stored run and wait as :meth:`run` does."""
        outcome = self.service.dispatch(run_id, runner, parameters)
        if not outcome.done.wait(timeout):
            self.service.cancel(run_id)
            outcome.done.wait(30)
            raise AssertionError(
                f"runner {runner!r} did not return within {timeout:g}s — the dispatch was cancelled"
            )
        result = self.read(run_id)
        result.refusal = outcome.refusal
        return result

    def wait(self, run_id: str, *, timeout: float = 120.0) -> HarnessRun:
        """Wait until the run's latest dispatch is in a terminal state —
        for results that arrive by ingestion after the runner returned."""
        run = self.service.wait(run_id, timeout=timeout)
        if run.state not in TERMINAL_STATES and run.state != "COMPOSED":
            raise AssertionError(f"run {run_id} is still {run.state} after {timeout:g}s")
        return self.read(run_id)

    def read(self, run_id: str) -> HarnessRun:
        """The run as the store has it now."""
        store = self.service.store
        run = self.service.require_run(run_id)
        dispatch = run.latest_dispatch
        dispatch_id = dispatch.dispatch_id if dispatch is not None else None
        events = store.events(run_id, limit=100_000)
        return HarnessRun(
            run=run,
            dispatch=dispatch,
            verdicts=store.verdicts_for(run_id, dispatch_id) if dispatch_id else [],
            live=store.live_verdicts(run_id, dispatch_id) if dispatch_id else [],
            events=events,
            artifacts=store.artifact_refs(run_id, dispatch_id) if dispatch_id else [],
            warnings=[str(e.data.get("message", "")) for e in events if e.kind == "warning"],
        )

    def artifacts(self, run_id: str) -> list[dict[str, Any]]:
        """The run's artifacts as the API lists them: ``name``, ``kind``
        (``local`` / ``directory`` / ``url`` / ``external-path``), ``shard``,
        ``group`` and ``href`` — for a local file the path it is served
        under, so that with ``serve=True`` its address is
        ``harness.url + href``."""
        listing: list[dict[str, Any]] = self.service.artifacts.list(run_id)
        return listing

    def flush_observers(self, timeout: float = 10.0) -> bool:
        """Wait until every observer was told every event emitted so far
        (observers are notified on a thread of their own)."""
        return bool(self.service.bus.flush(timeout))

    def cancel(self, run_id: str) -> DispatchRecord:
        return self.service.cancel(run_id)

    def ingest(self, bundle: str | Path, **kwargs: Any) -> Any:
        """Ingest a results bundle (``runcomposer ingest``)."""
        return self.service.ingest(bundle, **kwargs)

    def export(self, run_id: str) -> Any:
        """Hand the run out for an external executor (``spec --export``)."""
        return self.service.export(run_id)

    # -- lifecycle ------------------------------------------------------------------------

    def close(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            if self._server_thread is not None:
                self._server_thread.join(timeout=15)
            self._server = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        service = self.__dict__.get("service")
        if service is not None:
            service.close()
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._saved_env.clear()
        if self._own_directory:
            shutil.rmtree(self.directory, ignore_errors=True)

    def __enter__(self) -> ConnectorHarness:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _item_json(entry: Item | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(entry, Item):
        data: dict[str, Any] = {"id": entry.id, "tags": list(entry.tags)}
        if entry.name:
            data["name"] = entry.name
        if entry.hierarchy:
            data["hierarchy"] = list(entry.hierarchy)
        if entry.meta:
            data["meta"] = dict(entry.meta)
        return data
    return dict(entry)
