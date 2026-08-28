"""`sqlite` RunStore — the zero-setup reference store (DESIGN.md §6.3).

Persists the normative schema (runs, specs, dispatches, deliveries, verdicts,
artifact_refs) and nothing else — in particular no runner-lifecycle fields.
Connections are opened per operation, so concurrent CLI invocations and the
API server can share one database file without a daemon.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from runcomposer.core.ids import new_ulid
from runcomposer.core.model import (
    ArtifactRef,
    DeliveryRecord,
    DispatchRecord,
    RunRecord,
    Verdict,
)

__all__ = ["SqliteRunStore", "StoreError"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    completion TEXT,
    completed_at TEXT,
    ingest_token_sha256 TEXT
);
CREATE TABLE IF NOT EXISTS specs (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    document TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatches (
    dispatch_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    mode TEXT NOT NULL,
    declared_shards INTEGER,
    spec_sha256 TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    dispatch_id TEXT,
    shard TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    format TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, content_hash)
);
CREATE TABLE IF NOT EXISTS verdicts (
    run_id TEXT NOT NULL,
    dispatch_id TEXT,
    shard TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS artifact_refs (
    run_id TEXT NOT NULL,
    dispatch_id TEXT,
    name TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT '',
    url_or_path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verdicts_run ON verdicts(run_id, dispatch_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_run ON deliveries(run_id, dispatch_id, shard);
CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state);
"""


class StoreError(RuntimeError):
    """Raised for store-level contract violations (e.g. unknown run)."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SqliteRunStore:
    store_id = "sqlite"

    def __init__(self, path: str = "runcomposer.db"):
        self._path = str(path)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- runs & specs ------------------------------------------------------

    def create_run(
        self,
        spec: Mapping[str, Any],
        *,
        origin: str,
        ingest_token_sha256: str | None = None,
    ) -> RunRecord:
        run = spec["run"]
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO runs (id, title, created_at, origin, labels, state, ingest_token_sha256)"
                " VALUES (?, ?, ?, ?, ?, 'COMPOSED', ?)",
                (
                    run["id"],
                    run.get("title", ""),
                    run["created_at"],
                    origin,
                    json.dumps(run.get("labels", {})),
                    ingest_token_sha256,
                ),
            )
            conn.execute(
                "INSERT INTO specs (run_id, document) VALUES (?, ?)",
                (run["id"], json.dumps(spec, ensure_ascii=False)),
            )
        record = self.get_run(run["id"])
        assert record is not None
        return record

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            dispatches = conn.execute(
                "SELECT * FROM dispatches WHERE run_id = ? ORDER BY created_at, dispatch_id",
                (run_id,),
            ).fetchall()
            deliveries = conn.execute(
                "SELECT * FROM deliveries WHERE run_id = ? ORDER BY created_at, delivery_id",
                (run_id,),
            ).fetchall()
        return self._run_record(row, dispatches, deliveries)

    def list_runs(
        self,
        *,
        state: str | None = None,
        labels: Mapping[str, str] | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        """Filters per §9: state, labels (all given pairs must match), time
        window on created_at (ISO-8601 UTC strings compare lexicographically)."""
        conditions, params = [], []
        if state is not None:
            conditions.append("state = ?")
            params.append(state)
        if since is not None:
            conditions.append("created_at >= ?")
            params.append(since)
        if until is not None:
            conditions.append("created_at <= ?")
            params.append(until)
        query = "SELECT * FROM runs"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC, id DESC"
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        records = [self._shallow_run_record(row) for row in rows]
        if labels:
            records = [
                record
                for record in records
                if all(record.labels.get(key) == value for key, value in labels.items())
            ]
        return records[:limit]

    def get_spec_document(self, run_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT document FROM specs WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row["document"]) if row else None

    def latest_completed_run(
        self,
        *,
        completed_before: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> RunRecord | None:
        """§6.3/§7: 'latest' means latest COMPLETED — selected by completion
        status and completed_at, optionally bounded by a cutoff and scoped to
        runs carrying all of ``labels``.

        The label scope is not a nicety: unscoped, "latest" is the latest
        completed run of anything in the store, so on a shared deployment the
        next person's five-test selection silently becomes the reference for
        the nightly rerun. Labels are matched in Python, exactly as
        ``list_runs`` does — they live in a JSON column, and a run's label set
        is small."""
        query = "SELECT id, labels FROM runs WHERE state = 'COMPLETE'"
        params: list[Any] = []
        if completed_before is not None:
            query += " AND completed_at <= ?"
            params.append(completed_before)
        query += " ORDER BY completed_at DESC, id DESC"
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        for row in rows:
            if labels:
                run_labels = json.loads(row["labels"])
                if not all(run_labels.get(key) == value for key, value in labels.items()):
                    continue
            return self.get_run(row["id"])
        return None

    def get_ingest_token_sha256(self, run_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT ingest_token_sha256 FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        return row["ingest_token_sha256"] if row else None

    # -- dispatches --------------------------------------------------------

    def add_dispatch(
        self,
        run_id: str,
        *,
        dispatch_id: str,
        mode: str,
        declared_shards: int | None,
        spec_sha256: str | None = None,
    ) -> DispatchRecord:
        created_at = _utc_now()
        with self._conn() as conn:
            self._require_run(conn, run_id)
            existing = conn.execute(
                "SELECT run_id, created_at FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
            ).fetchone()
            if existing is not None:
                # Re-declaration of the same dispatch (§6.3): a dispatch is
                # recorded when the hand-off happens and refined from the
                # runner's handle afterwards. Same row, original created_at.
                if existing["run_id"] != run_id:
                    raise StoreError(
                        f"dispatch id {dispatch_id!r} already belongs to run "
                        f"{existing['run_id']!r} — refusing to move it to {run_id!r}"
                    )
                created_at = existing["created_at"]
                conn.execute(
                    "UPDATE dispatches SET mode = ?, declared_shards = ?, spec_sha256 = ?"
                    " WHERE dispatch_id = ?",
                    (mode, declared_shards, spec_sha256, dispatch_id),
                )
            else:
                conn.execute(
                    "INSERT INTO dispatches (dispatch_id, run_id, mode, declared_shards, spec_sha256, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (dispatch_id, run_id, mode, declared_shards, spec_sha256, created_at),
                )
        return DispatchRecord(
            dispatch_id=dispatch_id,
            run_id=run_id,
            mode=mode,
            declared_shards=declared_shards,
            created_at=created_at,
            spec_sha256=spec_sha256,
        )

    # -- deliveries & verdicts (§5 idempotency) -----------------------------

    def record_delivery(
        self,
        run_id: str,
        *,
        dispatch_id: str | None,
        shard: str,
        content_hash: str,
        format: str,
        verdicts: Sequence[Verdict],
    ) -> str:
        with self._conn() as conn:
            self._require_run(conn, run_id)
            duplicate = conn.execute(
                "SELECT delivery_id FROM deliveries WHERE run_id = ? AND content_hash = ?",
                (run_id, content_hash),
            ).fetchone()
            if duplicate is not None:
                return "duplicate"  # byte-identical bundle: no-op

            outcome = "new"
            replaced = conn.execute(
                "SELECT delivery_id FROM deliveries WHERE run_id = ? AND dispatch_id IS ? AND shard = ?",
                (run_id, dispatch_id, shard),
            ).fetchall()
            if replaced:
                # Last-writer-wins per shard: corrections may flip FAIL→PASS.
                outcome = "replaced"
                ids = [row["delivery_id"] for row in replaced]
                marks = ",".join("?" for _ in ids)
                conn.execute(f"DELETE FROM verdicts WHERE delivery_id IN ({marks})", ids)
                conn.execute(f"DELETE FROM deliveries WHERE delivery_id IN ({marks})", ids)

            delivery_id = new_ulid()
            conn.execute(
                "INSERT INTO deliveries (delivery_id, run_id, dispatch_id, shard, content_hash, format, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (delivery_id, run_id, dispatch_id, shard, content_hash, format, _utc_now()),
            )
            conn.executemany(
                "INSERT INTO verdicts (run_id, dispatch_id, shard, delivery_id, item_id, status,"
                " duration_ms, message, attempt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        dispatch_id,
                        shard,
                        delivery_id,
                        v.item_id,
                        v.status,
                        v.duration_ms,
                        v.message,
                        v.attempt,
                    )
                    for v in verdicts
                ],
            )
        return outcome

    def record_live_verdict(self, run_id: str, dispatch_id: str, shard: str, verdict: Verdict) -> None:
        """Streamed by a runner's listener DURING execution (§6.2a). Live rows
        use a sentinel delivery id and are cleared when the shard's terminal
        delivery reconciles them — no schema beyond the normative tables."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO verdicts (run_id, dispatch_id, shard, delivery_id, item_id, status,"
                " duration_ms, message, attempt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    dispatch_id,
                    shard,
                    f"live-{dispatch_id}",
                    verdict.item_id,
                    verdict.status,
                    verdict.duration_ms,
                    verdict.message,
                    verdict.attempt,
                ),
            )

    def clear_live_verdicts(self, run_id: str, dispatch_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM verdicts WHERE run_id = ? AND delivery_id = ?",
                (run_id, f"live-{dispatch_id}"),
            )

    def duration_aggregates(
        self, *, labels: Mapping[str, str] | None = None, last_n: int = 5
    ) -> dict[str, float]:
        """§6.3 history surface: average duration per item id over the last N
        completed dispatches of runs matching the label selector. A dispatch
        counts as completed when it has delivered (the deliveries join below);
        the run's *current* state is irrelevant — a re-dispatch of the same
        run must still see its own earlier history."""
        with self._conn() as conn:
            runs = conn.execute(
                "SELECT id, labels FROM runs ORDER BY created_at DESC, id DESC"
            ).fetchall()
            matching_run_ids = []
            for row in runs:
                run_labels = json.loads(row["labels"])
                if labels and not all(run_labels.get(k) == v for k, v in labels.items()):
                    continue
                matching_run_ids.append(row["id"])
            if not matching_run_ids:
                return {}
            marks = ",".join("?" for _ in matching_run_ids)
            dispatches = conn.execute(
                f"SELECT DISTINCT d.dispatch_id, d.created_at FROM dispatches d"
                f" JOIN deliveries dv ON dv.dispatch_id = d.dispatch_id"
                f" WHERE d.run_id IN ({marks}) ORDER BY d.created_at DESC LIMIT ?",
                (*matching_run_ids, last_n),
            ).fetchall()
            if not dispatches:
                return {}
            dispatch_ids = [row["dispatch_id"] for row in dispatches]
            marks = ",".join("?" for _ in dispatch_ids)
            rows = conn.execute(
                f"SELECT item_id, AVG(duration_ms) AS avg_ms FROM verdicts"
                f" WHERE dispatch_id IN ({marks}) AND duration_ms > 0 GROUP BY item_id",
                dispatch_ids,
            ).fetchall()
        return {row["item_id"]: float(row["avg_ms"]) for row in rows}

    def delivered_shards(self, run_id: str, dispatch_id: str | None) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT shard FROM deliveries WHERE run_id = ? AND dispatch_id IS ?",
                (run_id, dispatch_id),
            ).fetchall()
        return {row["shard"] for row in rows}

    def verdicts_for(
        self, run_id: str, dispatch_id: str | None = None, *, shard: str | None = None
    ) -> list[Verdict]:
        """Verdicts of a run, narrowable to one dispatch and one shard. The
        shard column has always been stored; returning it is what makes a
        partition fan-out readable through the port instead of by opening the
        database file (§4 identity layering: run → dispatch → shard)."""
        query = "SELECT * FROM verdicts WHERE run_id = ?"
        params: tuple[Any, ...] = (run_id,)
        if dispatch_id is not None:
            query += " AND dispatch_id = ?"
            params += (dispatch_id,)
        if shard is not None:
            query += " AND shard = ?"
            params += (shard,)
        with self._conn() as conn:
            rows = conn.execute(query + " ORDER BY rowid", params).fetchall()
        return [
            Verdict(
                item_id=row["item_id"],
                status=row["status"],
                duration_ms=row["duration_ms"],
                message=row["message"],
                attempt=row["attempt"],
                shard=row["shard"],
            )
            for row in rows
        ]

    def prune_runs(self, *, before: str | None = None, max_runs: int | None = None) -> list[str]:
        """§6.4 retention: delete runs older than ``before`` and/or beyond the
        newest ``max_runs``, cascading to all dependent tables."""
        with self._conn() as conn:
            rows = conn.execute("SELECT id, created_at FROM runs ORDER BY created_at DESC, id DESC").fetchall()
            doomed = []
            for index, row in enumerate(rows):
                too_old = before is not None and row["created_at"] < before
                overflow = max_runs is not None and index >= max_runs
                if too_old or overflow:
                    doomed.append(row["id"])
            for run_id in doomed:
                for table in ("verdicts", "deliveries", "dispatches", "artifact_refs", "specs"):
                    conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return doomed

    # -- state & artifacts ---------------------------------------------------

    def set_run_state(
        self,
        run_id: str,
        state: str,
        *,
        completion: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        with self._conn() as conn:
            self._require_run(conn, run_id)
            conn.execute(
                "UPDATE runs SET state = ?, completion = ?, completed_at = ? WHERE id = ?",
                (state, completion, completed_at, run_id),
            )

    def add_artifact_ref(
        self,
        run_id: str,
        dispatch_id: str | None,
        *,
        name: str,
        media_type: str,
        url_or_path: str,
    ) -> None:
        with self._conn() as conn:
            self._require_run(conn, run_id)
            conn.execute(
                "INSERT INTO artifact_refs (run_id, dispatch_id, name, media_type, url_or_path)"
                " VALUES (?, ?, ?, ?, ?)",
                (run_id, dispatch_id, name, media_type, url_or_path),
            )

    def artifact_refs(self, run_id: str, dispatch_id: str | None = None) -> list[ArtifactRef]:
        """§6.4: read back the references ``add_artifact_ref`` writes, in
        insertion order, optionally narrowed to one dispatch."""
        query = "SELECT * FROM artifact_refs WHERE run_id = ?"
        params: tuple[Any, ...] = (run_id,)
        if dispatch_id is not None:
            query += " AND dispatch_id = ?"
            params += (dispatch_id,)
        with self._conn() as conn:
            rows = conn.execute(query + " ORDER BY rowid", params).fetchall()
        return [
            ArtifactRef(
                run_id=row["run_id"],
                dispatch_id=row["dispatch_id"],
                name=row["name"],
                media_type=row["media_type"],
                url_or_path=row["url_or_path"],
            )
            for row in rows
        ]

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _require_run(conn: sqlite3.Connection, run_id: str) -> None:
        if conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
            raise StoreError(f"unknown run id {run_id!r}")

    @staticmethod
    def _shallow_run_record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            title=row["title"],
            created_at=row["created_at"],
            origin=row["origin"],
            labels=json.loads(row["labels"]),
            state=row["state"],
            completion=row["completion"],
            completed_at=row["completed_at"],
        )

    @classmethod
    def _run_record(cls, row: sqlite3.Row, dispatches, deliveries) -> RunRecord:
        shallow = cls._shallow_run_record(row)
        return RunRecord(
            **{**shallow.__dict__, "dispatches": tuple(
                DispatchRecord(
                    dispatch_id=d["dispatch_id"],
                    run_id=d["run_id"],
                    mode=d["mode"],
                    declared_shards=d["declared_shards"],
                    created_at=d["created_at"],
                    spec_sha256=d["spec_sha256"],
                )
                for d in dispatches
            ), "deliveries": tuple(
                DeliveryRecord(
                    delivery_id=d["delivery_id"],
                    run_id=d["run_id"],
                    dispatch_id=d["dispatch_id"],
                    shard=d["shard"],
                    content_hash=d["content_hash"],
                    format=d["format"],
                    created_at=d["created_at"],
                )
                for d in deliveries
            )},
        )
