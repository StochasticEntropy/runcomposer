"""`robot-pool` Runner (DESIGN.md §6.2a): in-process Robot Framework execution
on a shared process pool, with partition fan-out, duration-balanced chunking,
per-dispatch artifact isolation, and listener-based live status.

Facts stated up front (per §6.2a):
- Duration history comes from the RunStore keyed by item id over the last N
  completed dispatches matching ``history_selector``. Cold start: a fresh
  store has no durations — chunking degrades to round-robin, and the plan
  output says so.
- Live per-item status comes from the injected runcomposer Robot listener
  (``live_status: true``, the default). Without it, only planned items and
  final results are visible — a documented degradation, not a hidden one.
- As an executor, this runner honors the §3.3 contract: it executes exactly
  ``selection.materialized.item_ids``; on corpus drift it refuses unless
  ``allow_drift`` is set, in which case it executes the intersection and
  reports the difference as SKIP verdicts with reason ``drift``.
"""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Mapping

from runcomposer.core.ids import new_ulid
from runcomposer.core.model import Verdict
from runcomposer.core.ports import DispatchHandle, DispatchRefused, RunnerInfo

__all__ = ["RobotPoolRunner"]


def _run_chunk(payload: dict) -> dict:
    """Pool-worker entry: run one chunk of one partition with real Robot."""
    import robot

    from runcomposer.plugins.robot_listener import LiveStatusListener
    from runcomposer.plugins.sqlite_store import SqliteRunStore

    out_dir = Path(payload["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    listeners: list = list(payload.get("extra_listeners") or [])  # §6.2a `listener` option
    if payload.get("store_path"):
        store = SqliteRunStore(payload["store_path"])
        listeners.append(
            LiveStatusListener(store, payload["run_id"], payload["dispatch_id"], payload["shard"])
        )
    variables = [f"{key}:{value}" for key, value in payload["variables"].items()]
    variables.append(f"PARTITION:{payload['partition']}")
    with open(os.devnull, "w", encoding="utf-8") as devnull:
        rc = robot.run(
            payload["suite_root"],
            test=payload["item_ids"],
            variable=variables,
            listener=listeners,
            outputdir=str(out_dir),
            output="output.xml",
            log="NONE",
            report="NONE",
            consolecolors="off",
            stdout=devnull,
        )
    return {"shard": payload["shard"], "rc": rc, "output": str(out_dir / "output.xml")}


class RobotPoolRunner:
    runner_id = "robot-pool"

    def __init__(
        self,
        suite_root: str | None = None,
        max_workers: int = 2,
        partitions: list[str] | None = None,
        variables: Mapping[str, str] | None = None,
        live_status: bool = True,
        allow_drift: bool = False,
        listener: str | list[str] | None = None,
        pre_run_hooks: list[str] | None = None,
        history_selector: Mapping[str, Any] | None = None,
        history_depth: int = 5,
        output_root: str | None = None,
    ):
        self._defaults: dict[str, Any] = {
            "suite_root": suite_root,
            "partitions": partitions,
            "variables": dict(variables or {}),
            "live_status": live_status,
            "allow_drift": allow_drift,
            "listener": listener,
            "pre_run_hooks": list(pre_run_hooks or []),
        }
        self.max_workers = max_workers
        self.history_selector = dict(history_selector or {})
        self.history_depth = history_depth
        self.output_root = output_root
        self.last_plan: str = ""
        self._store = None
        self._source = None
        self._artifact_root: Path | None = Path(output_root) if output_root else None

    # Optional in-process binding (the core hands the document only; a bound
    # store/source enables live status, duration history, and drift checks).
    def bind(self, *, store=None, source=None, artifact_root=None) -> None:
        self._store = store
        self._source = source
        if artifact_root is not None and self.output_root is None:
            self._artifact_root = Path(artifact_root)

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id=self.runner_id, capabilities=("live_status", "partitions"))

    # -- dispatch ---------------------------------------------------------------

    def dispatch(self, spec: Mapping[str, Any]) -> DispatchHandle:
        opts = {**self._defaults, **(spec.get("runner", {}).get("robot-pool") or {})}
        suite_root = opts.get("suite_root")
        if not suite_root:
            raise DispatchRefused(
                "robot-pool needs runner.robot-pool.suite_root (in the spec or runner config)"
            )
        run_id = spec["run"]["id"]
        item_ids = list(spec["selection"]["materialized"]["item_ids"])
        dispatch_id = new_ulid()

        item_ids, drift_skips = self._check_drift(spec, item_ids, opts)
        self._run_hooks(opts)
        partitions = list(opts.get("partitions") or ["default"])
        chunks, plan = self._plan(item_ids, partitions)
        self.last_plan = plan
        user_listener = opts.get("listener")
        extra_listeners = (
            [user_listener] if isinstance(user_listener, str) else list(user_listener or [])
        )

        shards: list[dict] = []
        for partition in partitions:
            for index, chunk in enumerate(chunks, start=1):
                if not chunk:
                    continue
                shard = f"{partition}-{index}" if len(partitions) > 1 or len(chunks) > 1 else partition
                out_dir = self._out_dir(run_id, dispatch_id, shard)
                shards.append(
                    {
                        "suite_root": suite_root,
                        "item_ids": chunk,
                        "variables": dict(opts.get("variables") or {}),
                        "partition": partition,
                        "shard": shard,
                        "run_id": run_id,
                        "dispatch_id": dispatch_id,
                        "out_dir": str(out_dir),
                        "store_path": self._live_store_path(opts),
                        "extra_listeners": extra_listeners,
                    }
                )

        if self._store is not None:
            self._store.set_run_state(run_id, "RUNNING")
        with ProcessPoolExecutor(max_workers=self.max_workers) as pool:
            results = list(pool.map(_run_chunk, shards))

        self._deliver(run_id, dispatch_id, results, drift_skips)
        declared = len(shards) + (1 if drift_skips else 0)
        return DispatchHandle(dispatch_id=dispatch_id, shards=declared)

    def _run_hooks(self, opts) -> None:
        """§6.2a ``pre_run_hooks``: shell commands run once per dispatch,
        before any chunk executes (environment prep, readiness checks). A
        failing hook refuses the dispatch."""
        import subprocess

        for hook in opts.get("pre_run_hooks") or []:
            completed = subprocess.run(hook, shell=True, capture_output=True, text=True)
            if completed.returncode != 0:
                raise DispatchRefused(
                    f"pre_run_hook failed (rc {completed.returncode}): {hook!r} — "
                    f"{(completed.stderr or completed.stdout).strip()[:300]}"
                )

    # -- §3.3 drift -------------------------------------------------------------

    def _check_drift(self, spec, item_ids: list[str], opts) -> tuple[list[str], list[Verdict]]:
        if self._source is None:
            return item_ids, []
        live_snapshot = self._source.snapshot()
        composed = spec.get("source", {}).get("snapshot")
        if live_snapshot == composed:
            return item_ids, []
        if not opts.get("allow_drift"):
            raise DispatchRefused(
                "live corpus snapshot differs from source.snapshot in the spec — refusing "
                f"(§3.3 drift check; composed {composed}, live {live_snapshot}). "
                "Set runner option allow_drift to execute the intersection."
            )
        live_ids = {item.id for item in self._source.items()}
        missing = [item_id for item_id in item_ids if item_id not in live_ids]
        kept = [item_id for item_id in item_ids if item_id in live_ids]
        skips = [
            Verdict(item_id=item_id, status="SKIP", message="drift") for item_id in missing
        ]
        return kept, skips

    # -- planning (§6.2a duration balancing) --------------------------------------

    def _plan(self, item_ids: list[str], partitions: list[str]) -> tuple[list[list[str]], str]:
        chunk_count = max(1, min(len(item_ids) or 1, self.max_workers // max(1, len(partitions)) or 1))
        durations: dict[str, float] = {}
        if self._store is not None and hasattr(self._store, "duration_aggregates"):
            durations = self._store.duration_aggregates(
                labels=self.history_selector.get("labels"), last_n=self.history_depth
            )
        known = [i for i in item_ids if durations.get(i)]
        lines = [
            f"robot-pool plan: {len(item_ids)} item(s) × {len(partitions)} partition(s) "
            f"→ {chunk_count} chunk(s)/partition, max_workers={self.max_workers}"
        ]
        chunks: list[list[str]] = [[] for _ in range(chunk_count)]
        if not known:
            # Cold start (§6.2a): a fresh store has no durations.
            lines.append(
                "  cold start: no duration history in the store — falling back to "
                "round-robin chunking; balance improves as completed dispatches accrue"
            )
            for index, item_id in enumerate(item_ids):
                chunks[index % chunk_count].append(item_id)
        else:
            lines.append(
                f"  duration-balanced chunking: history for {len(known)}/{len(item_ids)} item(s), "
                f"last {self.history_depth} completed dispatch(es), "
                f"selector labels {self.history_selector.get('labels') or {}}"
            )
            fallback = sum(durations[i] for i in known) / len(known)
            loads = [0.0] * chunk_count
            for item_id in sorted(item_ids, key=lambda i: durations.get(i, fallback), reverse=True):
                lightest = loads.index(min(loads))
                chunks[lightest].append(item_id)
                loads[lightest] += durations.get(item_id, fallback)
        for index, chunk in enumerate(chunks, start=1):
            estimate = sum(durations.get(i, 0) for i in chunk)
            lines.append(f"  chunk {index}: {len(chunk)} item(s), est {estimate:.0f}ms")
        return [c for c in chunks if c] or [[]], "\n".join(lines)

    # -- delivery ----------------------------------------------------------------

    def _deliver(self, run_id, dispatch_id, results, drift_skips) -> None:
        if self._store is None:
            return
        from runcomposer.plugins.robot_output_parser import RobotOutputXmlParser

        parser = RobotOutputXmlParser()
        for result in results:
            output = Path(result["output"])
            if not output.exists():
                raise RuntimeError(
                    f"robot produced no output.xml for shard {result['shard']} "
                    f"(rc {result['rc']}) — execution environment error"
                )
            verdicts = []
            for parsed in parser.parse(output):
                item_id = self._source.resolve(parsed.native_name) if self._source else parsed.native_name
                if item_id is None:
                    continue  # unknown native name: never invented (§5)
                verdicts.append(
                    Verdict(
                        item_id=item_id,
                        status=parsed.status,
                        duration_ms=parsed.duration_ms,
                        message=parsed.message,
                    )
                )
            self._store.record_delivery(
                run_id,
                dispatch_id=dispatch_id,
                shard=result["shard"],
                content_hash="sha256:" + hashlib.sha256(output.read_bytes()).hexdigest(),
                format="robot-output-xml",
                verdicts=verdicts,
            )
            self._store.add_artifact_ref(
                run_id, dispatch_id, name=f"output.xml ({result['shard']})",
                media_type="application/xml", url_or_path=str(output),
            )
        if drift_skips:
            payload = ",".join(v.item_id for v in drift_skips).encode()
            self._store.record_delivery(
                run_id,
                dispatch_id=dispatch_id,
                shard="drift",
                content_hash="sha256:" + hashlib.sha256(payload).hexdigest(),
                format="robot-output-xml",
                verdicts=drift_skips,
            )
        if hasattr(self._store, "clear_live_verdicts"):
            self._store.clear_live_verdicts(run_id, dispatch_id)

    # -- helpers -------------------------------------------------------------------

    def _out_dir(self, run_id: str, dispatch_id: str, shard: str) -> Path:
        root = self._artifact_root or Path("artifacts")
        return root / run_id / dispatch_id / shard

    def _live_store_path(self, opts) -> str | None:
        """Live streaming needs a store the child process can reach; the P2
        reference wiring is the sqlite file path."""
        if not opts.get("live_status"):
            return None
        path = getattr(self._store, "_path", None)
        return str(path) if path else None
