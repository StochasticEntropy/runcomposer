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
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from runcomposer.app.config import load_config
from runcomposer.app.service import Service
from runcomposer.core.lifecycle import TERMINAL_STATES
from runcomposer.core.model import ArtifactRef, DispatchRecord, Item, RunEvent, RunRecord, Verdict

__all__ = ["ConnectorHarness", "HarnessRun"]


@dataclass
class HarnessRun:
    """One run as the harness left it: read back from the store."""

    run: RunRecord
    dispatch: DispatchRecord | None
    verdicts: list[Verdict]
    live: list[Verdict]
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

    def logs(self, level: str | None = None) -> list[str]:
        """The runner's ``ctx.log`` lines (``level``: info / warning / …)."""
        return [
            str(event.data.get("message", ""))
            for event in self.events
            if event.kind in ("log", "warning") and (level is None or event.data.get("level") == level)
        ]


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
    ):
        self._own_directory = directory is None
        self.directory = Path(directory) if directory is not None else Path(
            tempfile.mkdtemp(prefix="runcomposer-harness-")
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        self._saved_env: dict[str, str | None] = {}
        if env:
            import os

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
        except BaseException:
            self.close()
            raise

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
        if isinstance(selection, (list, tuple)):
            selection = {"item_ids": list(selection)}
        elif not isinstance(selection, Mapping) or not (
            {"tag_filter", "item_ids", "history"} & set(selection)
        ):
            selection = {"tag_filter": selection}
        result = self.service.compose_run(
            selection,
            title=title,
            labels=dict(labels) if labels else None,
            origin="harness",
            runner_id=runner,
            parameters=dict(parameters) if parameters is not None and runner else None,
        )
        assert result.run is not None
        return result.run.id

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
        service = self.__dict__.get("service")
        if service is not None:
            service.close()
        import os

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
