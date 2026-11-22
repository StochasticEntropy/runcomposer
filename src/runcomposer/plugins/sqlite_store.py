"""`sqlite` RunStore — the zero-setup reference store (DESIGN.md §6.3).

Safe to share: several threads of one server, and several processes (a
server, a CLI invocation, an inbox watcher) on one database file.

- WAL journal + a busy timeout, so readers never block writers and a writer
  waits for another instead of failing;
- one connection per thread, never shared;
- every write is one short ``BEGIN IMMEDIATE`` transaction, so a
  check-then-write (``record_delivery``) cannot interleave with another;
- ``atomic()`` widens that to a caller's read-evaluate-write: every store
  call the thread makes inside it is one ``BEGIN IMMEDIATE`` transaction,
  so the ledger's lifecycle decisions hold across processes;
- the schema is versioned in ``PRAGMA user_version`` and migrated in place —
  a database written by 0.1.x opens as it is;
- the SQLite library must be :data:`MIN_SQLITE_VERSION` or newer, with JSON
  functions available; an older one is refused when the store opens.

What the minimum is made of (the SQLite release that introduced each
feature used here): the upsert ``INSERT … ON CONFLICT … DO UPDATE`` 3.24.0 —
the newest feature, hence the minimum; the table-valued ``json_each`` (the
JSON extension) 3.9.0, compiled in by default only from 3.38.0, so its
presence is checked rather than assumed; ``instr`` 3.7.15;
the write-ahead log 3.7.0; ``SAVEPOINT`` 3.6.8; deterministic user functions 3.8.3.
Nothing newer is used — no ``RETURNING`` (3.35), ``STRICT`` tables (3.37),
``->>`` (3.38), window functions (3.25), generated columns (3.31) or
``unixepoch`` (3.38).

``path: ":memory:"`` is supported for tests and throwaway use: one private
in-memory database for the lifetime of the store object, serialised through a
lock (an in-memory database has no second process to be safe against).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from runcomposer.sdk import (
    ACTIVE_STATES,
    ArtifactRef,
    DeliveryRecord,
    DispatchRecord,
    LiveStatus,
    RunActive,
    RunEvent,
    RunPage,
    RunRecord,
    StoreBase,
    StoreError,
    Verdict,
    completion_of,
    missing_verdicts,
    new_ulid,
    normalize_time,
    sha256_hex,
    summarize,
    utc_now,
)

__all__ = ["MIN_SQLITE_VERSION", "SCHEMA_VERSION", "SqliteRunStore", "sqlite_shortfall"]

#: Bumped with every schema change; see ``_migrate``.
SCHEMA_VERSION = 4

#: The oldest SQLite library the store runs on (the upsert; see the module doc).
MIN_SQLITE_VERSION = (3, 24, 0)


def sqlite_shortfall(version: Sequence[int], *, json: bool) -> str | None:
    """What the SQLite library ``version`` (with JSON functions available or
    not) lacks for this store, as a sentence — or ``None`` when it has it all."""
    found = ".".join(str(part) for part in version)
    required = ".".join(str(part) for part in MIN_SQLITE_VERSION)
    if tuple(version) < MIN_SQLITE_VERSION:
        return (
            f"the sqlite store needs SQLite {required} or newer; this Python links "
            f"SQLite {found}"
        )
    if not json:
        return (
            f"the sqlite store needs SQLite's JSON functions (json_each); the "
            f"SQLite {found} this Python links was built without them"
        )
    return None

_BUSY_TIMEOUT_MS = 30_000

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
    ingest_token_sha256 TEXT,
    planned_count INTEGER NOT NULL DEFAULT 0
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
    created_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'AWAITING_RESULTS',
    shards TEXT NOT NULL DEFAULT '[]',
    shard_items TEXT NOT NULL DEFAULT '{}',
    shard_groups TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT '',
    links TEXT NOT NULL DEFAULT '{}',
    parameters TEXT NOT NULL DEFAULT '{}',
    completion TEXT,
    completed_at TEXT,
    summary TEXT NOT NULL DEFAULT '{}',
    owner TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error_code TEXT NOT NULL DEFAULT '',
    error_owner TEXT
);
CREATE TABLE IF NOT EXISTS dispatch_specs (
    dispatch_id TEXT PRIMARY KEY,
    content BLOB NOT NULL,
    media_type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingest_tokens (
    token_sha256 TEXT NOT NULL,
    run_id TEXT NOT NULL,
    dispatch_id TEXT
);
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    dispatch_id TEXT NOT NULL,
    shard TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    format TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, dispatch_id, shard)
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
CREATE TABLE IF NOT EXISTS item_names (
    run_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (run_id, item_id)
);
CREATE TABLE IF NOT EXISTS live_verdicts (
    run_id TEXT NOT NULL,
    dispatch_id TEXT NOT NULL,
    shard TEXT NOT NULL,
    item_id TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (dispatch_id, shard, item_id)
);
CREATE TABLE IF NOT EXISTS artifact_refs (
    run_id TEXT NOT NULL,
    dispatch_id TEXT,
    name TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT '',
    url_or_path TEXT NOT NULL,
    shard TEXT,
    shard_group TEXT
);
CREATE TABLE IF NOT EXISTS duration_seeds (
    item_id TEXT NOT NULL,
    labels TEXT NOT NULL DEFAULT '{}',
    duration_ms REAL NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (item_id, labels)
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    dispatch_id TEXT,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_verdicts_run ON verdicts(run_id, dispatch_id);
CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);
CREATE INDEX IF NOT EXISTS idx_dispatches_run ON dispatches(run_id);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_tokens_run ON ingest_tokens(run_id)
"""

#: Columns ``update_dispatch`` may set, and how each value is stored.
_JSON_COLUMNS = ("shards", "shard_items", "shard_groups", "links", "summary", "parameters")
_PLAIN_COLUMNS = (
    "state", "spec_sha256", "error", "plan", "completion", "completed_at", "owner",
    "error_code", "error_owner",
)


def _fold(value: Any) -> str:
    return value.casefold() if isinstance(value, str) else ""


def _has_json(conn: sqlite3.Connection) -> bool:
    """Is the JSON extension compiled into this SQLite (default from 3.38)?"""
    try:
        conn.execute("SELECT COUNT(*) FROM json_each('{}')").fetchone()
    except sqlite3.OperationalError:
        return False
    return True


class _NoLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> None:
        return None


_NO_LOCK = _NoLock()


class SqliteRunStore(StoreBase):
    store_id = "sqlite"

    #: The version of the SQLite library the store runs on, as checked when a
    #: connection opens — a class attribute, so that a subclass can stand in
    #: for an older library.
    sqlite_version: tuple[int, ...] = sqlite3.sqlite_version_info

    @staticmethod
    def resolve_config_paths(options: dict[str, Any], resolve: Any) -> dict[str, Any]:
        """``path`` is a file next to the config file — except ``:memory:``,
        which names no file at all, which is exactly why this decision is the
        store's and not the core's."""
        path = options.get("path")
        if isinstance(path, str) and path != ":memory:":
            options["path"] = resolve(path)
        return options

    def __init__(self, path: str = "runcomposer.db"):
        self._path = str(path)
        if self._path.startswith("file:"):
            raise StoreError(
                "store.sqlite.path takes a file path or ':memory:' — the 'file:' URI form is "
                "not supported (its options would bypass the journal settings this store relies on)"
            )
        self._memory = self._path == ":memory:"
        if not self._memory:
            # The directory the store lives in is created on first use, like
            # every other directory a config names (artifacts, inbox, quarantine).
            try:
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StoreError(
                    f"cannot create the directory of the sqlite store {self._path!r}: {exc}"
                ) from None
        self._local = threading.local()
        self._lock = threading.RLock()
        self._shared: sqlite3.Connection | None = None
        if self._memory:
            self._shared = self._open()
        with self._write() as conn:
            self._migrate(conn)

    # -- connections ---------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                self._path,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,  # transactions are explicit, below
                check_same_thread=not self._memory,
            )
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open the sqlite store at {self._path!r}: {exc}") from None
        shortfall = sqlite_shortfall(self.sqlite_version, json=_has_json(conn))
        if shortfall is not None:
            conn.close()
            raise StoreError(f"cannot open the sqlite store at {self._path!r}: {shortfall}")
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        if not self._memory:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        conn.create_function("rc_fold", 1, _fold, deterministic=True)
        return conn

    def _connection(self) -> sqlite3.Connection:
        if self._memory:
            if self._shared is None:
                raise StoreError("the in-memory store was closed")
            return self._shared
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._open()
        return conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._lock if self._memory else _NO_LOCK:
            yield self._connection()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One short write transaction. ``BEGIN IMMEDIATE`` takes the write
        lock up front, so what is read inside is still true when written.
        Inside :meth:`atomic` it is a savepoint of the enclosing transaction
        instead: all or nothing on its own, committed with the whole."""
        with self._lock if self._memory else _NO_LOCK:
            conn = self._connection()
            if conn.in_transaction:
                conn.execute("SAVEPOINT rc_write")
                try:
                    yield conn
                except BaseException:
                    conn.execute("ROLLBACK TO rc_write")
                    conn.execute("RELEASE rc_write")
                    raise
                conn.execute("RELEASE rc_write")
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Every store call this thread makes inside is one ``BEGIN
        IMMEDIATE`` transaction: the write lock is taken on entry, so another
        writer — a thread of this process or another process on the same
        file — waits until the block is over, what is read inside is still
        true when written, and an exception rolls the whole block back.
        Nested blocks join the outermost one."""
        with self._lock if self._memory else _NO_LOCK:
            conn = self._connection()
            if conn.in_transaction:
                yield
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def close(self) -> None:
        """Close this thread's connection (and the in-memory database)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
        if self._shared is not None:
            self._shared.close()
            self._shared = None

    # -- schema & migration ----------------------------------------------------

    def _migrate(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StoreError(
                f"{self._path}: database schema version {version} is newer than this "
                f"runcomposer understands ({SCHEMA_VERSION}) — upgrade runcomposer"
            )
        if version == SCHEMA_VERSION:
            return
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if version == 0 and "runs" in tables:
            _migrate_from_0_1(conn)
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        # Columns added after a table first existed (schema 3: shard groups;
        # schema 4: why a dispatch ended with an error).
        _add_columns(conn, "dispatches", {
            "shard_groups": "TEXT NOT NULL DEFAULT '{}'",
            "error_code": "TEXT NOT NULL DEFAULT ''",
            "error_owner": "TEXT",
        })
        _add_columns(conn, "artifact_refs", {"shard_group": "TEXT"})
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # -- runs & specs ----------------------------------------------------------

    def create_run(self, spec: Mapping[str, Any], *, origin: str) -> RunRecord:
        run = spec["run"]
        materialized = (spec.get("selection") or {}).get("materialized") or {}
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM runs WHERE id = ?", (run["id"],)).fetchone():
                raise StoreError(f"run id {run['id']!r} already exists")
            conn.execute(
                "INSERT INTO runs (id, title, created_at, origin, labels, state, planned_count)"
                " VALUES (?, ?, ?, ?, ?, 'COMPOSED', ?)",
                (
                    run["id"],
                    run.get("title", ""),
                    # Stored in the one spelling stored times have (UTC, Z), so
                    # that filters, ordering and retention compare times — a
                    # document may spell its own with an offset.
                    normalize_time(run["created_at"]),
                    origin,
                    json.dumps(run.get("labels", {}), ensure_ascii=False),
                    len(materialized.get("item_ids") or ()),
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
        with self._read() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            dispatches = conn.execute(
                "SELECT * FROM dispatches WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
            deliveries = conn.execute(
                "SELECT * FROM deliveries WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
        return _run_record(row, dispatches, deliveries)

    def list_runs(
        self,
        *,
        state: str | None = None,
        labels: Mapping[str, str] | None = None,
        since: str | None = None,
        until: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> RunPage:
        conditions: list[str] = []
        params: list[Any] = []
        if state is not None:
            conditions.append("state = ?")
            params.append(state)
        if since is not None:
            conditions.append("created_at >= ?")
            params.append(since)
        if until is not None:
            conditions.append("created_at <= ?")
            params.append(until)
        if q:
            conditions.append("instr(rc_fold(title), ?) > 0")
            params.append(q.casefold())
        for key, value in (labels or {}).items():
            # Matched as data, not through a JSON path: a key is compared
            # literally whatever characters it holds, on every SQLite the store
            # runs on (an escaped quote inside a path is not read alike by all).
            conditions.append(
                "EXISTS (SELECT 1 FROM json_each(runs.labels) AS label"
                " WHERE label.key = ? AND label.value = ?)"
            )
            params.extend([key, value])
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        with self._read() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM runs{where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM runs{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, max(0, int(limit)), max(0, int(offset))),
            ).fetchall()
            by_run: dict[str, list[sqlite3.Row]] = {row["id"]: [] for row in rows}
            if rows:
                marks = ",".join("?" for _ in rows)
                for dispatch in conn.execute(
                    f"SELECT * FROM dispatches WHERE run_id IN ({marks}) ORDER BY rowid",
                    list(by_run),
                ):
                    by_run[dispatch["run_id"]].append(dispatch)
        return RunPage(
            runs=tuple(_run_record(row, by_run[row["id"]], ()) for row in rows), total=total
        )

    def set_item_names(self, run_id: str, names: Mapping[str, str]) -> None:
        with self._write() as conn:
            _require_run(conn, run_id)
            conn.executemany(
                "INSERT INTO item_names (run_id, item_id, name) VALUES (?, ?, ?)"
                " ON CONFLICT (run_id, item_id) DO UPDATE SET name = excluded.name",
                [(run_id, str(item_id), str(name)) for item_id, name in names.items()],
            )

    def item_names(self, run_id: str) -> dict[str, str]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT item_id, name FROM item_names WHERE run_id = ?", (run_id,)
            ).fetchall()
        return {row["item_id"]: row["name"] for row in rows}

    def delete_run(self, run_id: str) -> bool:
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
                return False
            _delete_run(conn, run_id)
        return True

    def prune_runs(self, *, before: str | None = None, max_runs: int | None = None) -> list[str]:
        """§6.4 retention: delete runs created before ``before`` and/or beyond
        the newest ``max_runs``, with everything recorded for them."""
        with self._write() as conn:
            rows = conn.execute(
                "SELECT id, created_at FROM runs ORDER BY created_at DESC, id DESC"
            ).fetchall()
            doomed = [
                row["id"]
                for index, row in enumerate(rows)
                if (before is not None and row["created_at"] < before)
                or (max_runs is not None and index >= max_runs)
            ]
            for run_id in doomed:
                _delete_run(conn, run_id)
        return doomed

    def get_spec_document(self, run_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            row = conn.execute("SELECT document FROM specs WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row["document"]) if row else None

    def set_spec_document(self, run_id: str, spec: Mapping[str, Any]) -> None:
        with self._write() as conn:
            _require_run(conn, run_id)
            conn.execute(
                "UPDATE specs SET document = ? WHERE run_id = ?",
                (json.dumps(spec, ensure_ascii=False), run_id),
            )

    def set_run_headline(
        self, run_id: str, state: str, *, completion: str | None, completed_at: str | None
    ) -> None:
        with self._write() as conn:
            _require_run(conn, run_id)
            conn.execute(
                "UPDATE runs SET state = ?, completion = ?, completed_at = ? WHERE id = ?",
                (state, completion, completed_at, run_id),
            )

    def latest_completed_run(
        self,
        *,
        completed_before: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> RunRecord | None:
        query = "SELECT id, labels FROM runs WHERE state = 'COMPLETE'"
        params: list[Any] = []
        if completed_before is not None:
            query += " AND completed_at <= ?"
            params.append(completed_before)
        query += " ORDER BY completed_at DESC, id DESC"
        with self._read() as conn:
            rows = conn.execute(query, params).fetchall()
        for row in rows:
            if labels:
                run_labels = json.loads(row["labels"])
                if not all(run_labels.get(key) == value for key, value in labels.items()):
                    continue
            return self.get_run(row["id"])
        return None

    # -- dispatches --------------------------------------------------------------

    def add_dispatch(
        self,
        run_id: str,
        *,
        dispatch_id: str,
        mode: str,
        state: str,
        owner: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        exclusive: bool = False,
    ) -> DispatchRecord:
        with self._write() as conn:
            _require_run(conn, run_id)
            if conn.execute(
                "SELECT 1 FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
            ).fetchone():
                raise StoreError(f"dispatch id {dispatch_id!r} already exists")
            if exclusive:
                # Checked inside the BEGIN IMMEDIATE transaction that inserts:
                # another process holding the write lock has either committed
                # its dispatch (seen here) or not started (and will see ours).
                latest = conn.execute(
                    "SELECT dispatch_id, state FROM dispatches WHERE run_id = ?"
                    " ORDER BY rowid DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                if latest is not None and latest["state"] in ACTIVE_STATES:
                    raise RunActive(
                        f"run {run_id} already has a dispatch in flight "
                        f"({latest['dispatch_id']}, {latest['state']}) — cancel it or wait for "
                        "it before dispatching again"
                    )
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, run_id, mode, state, owner, parameters,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    dispatch_id,
                    run_id,
                    mode,
                    state,
                    owner,
                    json.dumps(dict(parameters or {}), ensure_ascii=False),
                    utc_now(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
            ).fetchone()
        return _dispatch_record(row)

    def update_dispatch(self, dispatch_id: str, **changes: Any) -> DispatchRecord:
        assignments: list[str] = []
        params: list[Any] = []
        for column, value in changes.items():
            if column in _JSON_COLUMNS:
                if column == "shards":
                    value = list(value)
                elif column == "shard_items":
                    value = {shard: list(items) for shard, items in value.items()}
                else:
                    value = dict(value or {})
                value = json.dumps(value, ensure_ascii=False)
            elif column == "cancel_requested":
                value = int(bool(value))
            elif column not in _PLAIN_COLUMNS:
                raise StoreError(f"update_dispatch cannot set {column!r}")
            assignments.append(f"{column} = ?")
            params.append(value)
        with self._write() as conn:
            if assignments:
                conn.execute(
                    f"UPDATE dispatches SET {', '.join(assignments)} WHERE dispatch_id = ?",
                    (*params, dispatch_id),
                )
            row = conn.execute(
                "SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"unknown dispatch id {dispatch_id!r}")
        return _dispatch_record(row)

    def get_dispatch(self, dispatch_id: str) -> DispatchRecord | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)
            ).fetchone()
        return _dispatch_record(row) if row else None

    def delete_dispatch(self, dispatch_id: str) -> None:
        with self._write() as conn:
            for table in _DISPATCH_TABLES:
                conn.execute(f"DELETE FROM {table} WHERE dispatch_id = ?", (dispatch_id,))

    def owned_dispatches(self) -> list[DispatchRecord]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM dispatches WHERE owner IS NOT NULL ORDER BY rowid"
            ).fetchall()
        return [_dispatch_record(row) for row in rows]

    def set_dispatch_spec(self, dispatch_id: str, content: bytes, media_type: str) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO dispatch_specs (dispatch_id, content, media_type) VALUES (?, ?, ?)"
                " ON CONFLICT(dispatch_id) DO UPDATE SET content = excluded.content,"
                " media_type = excluded.media_type",
                (dispatch_id, content, media_type),
            )

    def get_dispatch_spec(self, dispatch_id: str) -> tuple[bytes, str] | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT content, media_type FROM dispatch_specs WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        return (bytes(row["content"]), row["media_type"]) if row else None

    # -- ingest tokens -------------------------------------------------------------

    def set_ingest_token(self, run_id: str, dispatch_id: str | None, token_sha256: str) -> None:
        with self._write() as conn:
            _require_run(conn, run_id)
            if dispatch_id is not None:
                conn.execute("DELETE FROM ingest_tokens WHERE dispatch_id = ?", (dispatch_id,))
            conn.execute(
                "INSERT INTO ingest_tokens (token_sha256, run_id, dispatch_id) VALUES (?, ?, ?)",
                (token_sha256, run_id, dispatch_id),
            )

    def ingest_tokens(self, run_id: str) -> dict[str, str | None]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT token_sha256, dispatch_id FROM ingest_tokens WHERE run_id = ?", (run_id,)
            ).fetchall()
        return {row["token_sha256"]: row["dispatch_id"] for row in rows}

    # -- deliveries & verdicts (§5 idempotency) -----------------------------------

    def record_delivery(
        self,
        run_id: str,
        *,
        dispatch_id: str,
        shard: str,
        content_hash: str,
        format: str,
        verdicts: Sequence[Verdict],
    ) -> str:
        with self._write() as conn:
            _require_run(conn, run_id)
            existing = conn.execute(
                "SELECT delivery_id, content_hash FROM deliveries"
                " WHERE run_id = ? AND dispatch_id = ? AND shard = ?",
                (run_id, dispatch_id, shard),
            ).fetchone()
            outcome = "new"
            if existing is not None:
                if existing["content_hash"] == content_hash:
                    return "duplicate"  # the same bundle again: a no-op
                # Last-writer-wins per shard: a correction may flip FAIL→PASS.
                outcome = "replaced"
                conn.execute(
                    "DELETE FROM verdicts WHERE delivery_id = ?", (existing["delivery_id"],)
                )
                conn.execute(
                    "DELETE FROM deliveries WHERE delivery_id = ?", (existing["delivery_id"],)
                )
            delivery_id = new_ulid()
            conn.execute(
                "INSERT INTO deliveries (delivery_id, run_id, dispatch_id, shard, content_hash,"
                " format, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (delivery_id, run_id, dispatch_id, shard, content_hash, format, utc_now()),
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
            conn.execute(
                "DELETE FROM live_verdicts WHERE dispatch_id = ? AND shard = ?",
                (dispatch_id, shard),
            )
        return outcome

    def delivered_shards(self, run_id: str, dispatch_id: str) -> set[str]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT shard FROM deliveries WHERE run_id = ? AND dispatch_id = ?",
                (run_id, dispatch_id),
            ).fetchall()
        return {row["shard"] for row in rows}

    def verdicts_for(
        self, run_id: str, dispatch_id: str | None = None, *, shard: str | None = None
    ) -> list[Verdict]:
        return self._verdict_rows("verdicts", run_id, dispatch_id, shard)

    def record_live_verdict(
        self, run_id: str, dispatch_id: str, shard: str, verdict: LiveStatus
    ) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO live_verdicts (run_id, dispatch_id, shard, item_id, status,"
                " duration_ms, message, attempt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(dispatch_id, shard, item_id) DO UPDATE SET status = excluded.status,"
                " duration_ms = excluded.duration_ms, message = excluded.message,"
                " attempt = excluded.attempt",
                (
                    run_id,
                    dispatch_id,
                    shard,
                    verdict.item_id,
                    verdict.status,
                    verdict.duration_ms,
                    verdict.message,
                    verdict.attempt,
                ),
            )

    def live_verdicts(self, run_id: str, dispatch_id: str) -> list[LiveStatus]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM live_verdicts WHERE run_id = ? AND dispatch_id = ? ORDER BY rowid",
                (run_id, dispatch_id),
            ).fetchall()
        return [
            LiveStatus(
                item_id=row["item_id"],
                status=row["status"],
                duration_ms=row["duration_ms"],
                message=row["message"],
                attempt=row["attempt"],
                shard=row["shard"],
            )
            for row in rows
        ]

    def _verdict_rows(
        self, table: str, run_id: str, dispatch_id: str | None, shard: str | None
    ) -> list[Verdict]:
        query = f"SELECT * FROM {table} WHERE run_id = ?"
        params: tuple[Any, ...] = (run_id,)
        if dispatch_id is not None:
            query += " AND dispatch_id = ?"
            params += (dispatch_id,)
        if shard is not None:
            query += " AND shard = ?"
            params += (shard,)
        with self._read() as conn:
            rows = conn.execute(query + " ORDER BY rowid", params).fetchall()
        return [_verdict(row) for row in rows]

    def duration_aggregates(
        self,
        *,
        labels: Mapping[str, str] | None = None,
        last_n: int = 5,
        statuses: Sequence[str] | None = None,
    ) -> dict[str, float]:
        """Mean duration per item id over the last N dispatches that
        delivered, of runs matching the label selector. A run's *current*
        state is irrelevant — a re-dispatch must still see its own history.
        Items nothing was measured for answer with their seeded duration."""

        def carries(stored: str) -> bool:
            have = json.loads(stored)
            return not labels or all(have.get(k) == v for k, v in labels.items())

        with self._read() as conn:
            seeds: dict[str, list[float]] = {}
            for row in conn.execute("SELECT item_id, labels, duration_ms FROM duration_seeds"):
                if carries(row["labels"]):
                    seeds.setdefault(row["item_id"], []).append(float(row["duration_ms"]))
            measured: dict[str, float] = {}
            run_ids = [
                row["id"]
                for row in conn.execute("SELECT id, labels FROM runs").fetchall()
                if carries(row["labels"])
            ]
            dispatch_ids: list[str] = []
            if run_ids:
                marks = ",".join("?" for _ in run_ids)
                dispatch_ids = [
                    row["dispatch_id"]
                    for row in conn.execute(
                        f"SELECT d.dispatch_id FROM dispatches d WHERE d.run_id IN ({marks})"
                        " AND EXISTS (SELECT 1 FROM deliveries dv"
                        " WHERE dv.dispatch_id = d.dispatch_id)"
                        " ORDER BY d.rowid DESC LIMIT ?",
                        (*run_ids, max(0, int(last_n))),
                    ).fetchall()
                ]
            if dispatch_ids:
                marks = ",".join("?" for _ in dispatch_ids)
                params: list[Any] = list(dispatch_ids)
                only = ""
                if statuses is not None:
                    wanted = [str(status) for status in statuses]
                    only = f" AND status IN ({','.join('?' for _ in wanted)})"
                    params += wanted
                rows = conn.execute(
                    f"SELECT item_id, AVG(duration_ms) AS avg_ms FROM verdicts"
                    f" WHERE dispatch_id IN ({marks}) AND duration_ms > 0{only} GROUP BY item_id",
                    params,
                ).fetchall()
                measured = {row["item_id"]: float(row["avg_ms"]) for row in rows}
        fallback = {
            item_id: sum(values) / len(values)
            for item_id, values in seeds.items()
            if item_id not in measured
        }
        return {**fallback, **measured}

    def seed_durations(
        self, durations: Mapping[str, float], *, labels: Mapping[str, str] | None = None
    ) -> int:
        scope = json.dumps(dict(labels or {}), ensure_ascii=False, sort_keys=True)
        now = utc_now()
        stored = 0
        with self._write() as conn:
            for item_id, duration in durations.items():
                if float(duration) <= 0:
                    continue  # 0 means "no duration known" — nothing to seed
                conn.execute(
                    "INSERT INTO duration_seeds (item_id, labels, duration_ms, imported_at)"
                    " VALUES (?, ?, ?, ?) ON CONFLICT(item_id, labels) DO UPDATE SET"
                    " duration_ms = excluded.duration_ms, imported_at = excluded.imported_at",
                    (str(item_id), scope, float(duration), now),
                )
                stored += 1
        return stored

    # -- artifacts ------------------------------------------------------------------

    def add_artifact_ref(
        self,
        run_id: str,
        dispatch_id: str | None,
        *,
        name: str,
        media_type: str,
        url_or_path: str,
        shard: str | None = None,
        group: str | None = None,
    ) -> None:
        with self._write() as conn:
            _require_run(conn, run_id)
            conn.execute(
                "INSERT INTO artifact_refs (run_id, dispatch_id, name, media_type, url_or_path,"
                " shard, shard_group) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, dispatch_id, name, media_type, url_or_path, shard, group),
            )

    def artifact_refs(self, run_id: str, dispatch_id: str | None = None) -> list[ArtifactRef]:
        query = "SELECT * FROM artifact_refs WHERE run_id = ?"
        params: tuple[Any, ...] = (run_id,)
        if dispatch_id is not None:
            query += " AND dispatch_id = ?"
            params += (dispatch_id,)
        with self._read() as conn:
            rows = conn.execute(query + " ORDER BY rowid", params).fetchall()
        return [
            ArtifactRef(
                run_id=row["run_id"],
                dispatch_id=row["dispatch_id"],
                name=row["name"],
                media_type=row["media_type"],
                url_or_path=row["url_or_path"],
                shard=row["shard"],
                group=row["shard_group"],
            )
            for row in rows
        ]

    # -- events ------------------------------------------------------------------------

    def append_event(
        self,
        run_id: str,
        *,
        kind: str,
        at: str,
        dispatch_id: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> RunEvent:
        payload = dict(data or {})
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO events (run_id, at, kind, dispatch_id, data) VALUES (?, ?, ?, ?, ?)",
                (run_id, at, kind, dispatch_id, json.dumps(payload, ensure_ascii=False)),
            )
            seq = cursor.lastrowid
        assert seq is not None
        return RunEvent(
            seq=seq, run_id=run_id, at=at, kind=kind, dispatch_id=dispatch_id, data=payload
        )

    def events(self, run_id: str, *, after: int = 0, limit: int = 500) -> list[RunEvent]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (run_id, int(after), max(0, int(limit))),
            ).fetchall()
        return _events(rows)

    def latest_events(self, run_id: str, *, limit: int = 200) -> list[RunEvent]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM (SELECT * FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT ?)"
                " ORDER BY seq",
                (run_id, max(0, int(limit))),
            ).fetchall()
        return _events(rows)


def _events(rows: list[Any]) -> list[RunEvent]:
    return [
        RunEvent(
            seq=row["seq"],
            run_id=row["run_id"],
            at=row["at"],
            kind=row["kind"],
            dispatch_id=row["dispatch_id"],
            data=json.loads(row["data"]),
        )
        for row in rows
    ]


# -- helpers -------------------------------------------------------------------------

_RUN_TABLES = (
    "item_names",
    "verdicts",
    "live_verdicts",
    "deliveries",
    "artifact_refs",
    "events",
    "ingest_tokens",
    "specs",
)
_DISPATCH_TABLES = (
    "verdicts",
    "live_verdicts",
    "deliveries",
    "artifact_refs",
    "dispatch_specs",
    "ingest_tokens",
    "dispatches",
)


def _require_run(conn: sqlite3.Connection, run_id: str) -> None:
    if conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
        raise StoreError(f"unknown run id {run_id!r}")


def _delete_run(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute(
        "DELETE FROM dispatch_specs WHERE dispatch_id IN"
        " (SELECT dispatch_id FROM dispatches WHERE run_id = ?)",
        (run_id,),
    )
    for table in _RUN_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM dispatches WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))


def _verdict(row: sqlite3.Row) -> Verdict:
    return Verdict(
        item_id=row["item_id"],
        status=row["status"],
        duration_ms=row["duration_ms"],
        message=row["message"],
        attempt=row["attempt"],
        shard=row["shard"],
    )


def _dispatch_record(row: sqlite3.Row) -> DispatchRecord:
    return DispatchRecord(
        dispatch_id=row["dispatch_id"],
        run_id=row["run_id"],
        mode=row["mode"],
        state=row["state"],
        created_at=row["created_at"],
        shards=tuple(json.loads(row["shards"])),
        shard_items={
            shard: tuple(items) for shard, items in json.loads(row["shard_items"]).items()
        },
        shard_groups=json.loads(row["shard_groups"]),
        spec_sha256=row["spec_sha256"],
        error=row["error"],
        plan=row["plan"],
        links=json.loads(row["links"]),
        parameters=json.loads(row["parameters"]),
        completion=row["completion"],
        completed_at=row["completed_at"],
        summary=json.loads(row["summary"]),
        owner=row["owner"],
        cancel_requested=bool(row["cancel_requested"]),
        error_code=row["error_code"],
        error_owner=row["error_owner"],
    )


def _run_record(
    row: sqlite3.Row, dispatches: Sequence[sqlite3.Row], deliveries: Sequence[sqlite3.Row]
) -> RunRecord:
    return RunRecord(
        id=row["id"],
        title=row["title"],
        created_at=row["created_at"],
        origin=row["origin"],
        labels=json.loads(row["labels"]),
        state=row["state"],
        completion=row["completion"],
        completed_at=row["completed_at"],
        planned_count=row["planned_count"],
        dispatches=tuple(_dispatch_record(d) for d in dispatches),
        deliveries=tuple(
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
        ),
    )


# -- migration from the 0.1.x schema ---------------------------------------------------


def _add_columns(conn: sqlite3.Connection, table: str, columns: Mapping[str, str]) -> None:
    present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, declaration in columns.items():
        if name not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _migrate_from_0_1(conn: sqlite3.Connection) -> None:
    """Bring a database written by runcomposer 0.1.x to the current schema,
    in place and inside the caller's transaction.

    What changes for the data, beyond new columns:

    - every delivery belongs to a dispatch: deliveries recorded against a run
      directly get an ``ingest`` dispatch of their own;
    - the idempotency key becomes (run, dispatch, shard): where 0.1's
      check-then-write left several deliveries for one shard, the newest wins;
    - the lifecycle moves onto the dispatch, and completion is **recomputed**
      under the current rules — a run 0.1 called ``PASS`` although planned
      items never reported is ``INCOMPLETE`` now, which is what it was;
    - a dispatch that was still executing is ``STALE``: the process that
      owned it is necessarily gone;
    - a run's ``created_at`` is normalized to UTC (0.1 stored a document's
      own spelling, offset and all);
    - streamed live rows are dropped (they were never results);
    - a plaintext ingest token inside a stored document is removed; its hash
      stays valid for the run.
    """
    _add_columns(conn, "runs", {"planned_count": "INTEGER NOT NULL DEFAULT 0"})
    _add_columns(
        conn,
        "dispatches",
        {
            "state": "TEXT NOT NULL DEFAULT 'AWAITING_RESULTS'",
            "shards": "TEXT NOT NULL DEFAULT '[]'",
            "shard_items": "TEXT NOT NULL DEFAULT '{}'",
            "error": "TEXT NOT NULL DEFAULT ''",
            "plan": "TEXT NOT NULL DEFAULT ''",
            "links": "TEXT NOT NULL DEFAULT '{}'",
            "parameters": "TEXT NOT NULL DEFAULT '{}'",
            "completion": "TEXT",
            "completed_at": "TEXT",
            "summary": "TEXT NOT NULL DEFAULT '{}'",
            "owner": "TEXT",
            "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        },
    )
    _add_columns(conn, "artifact_refs", {"shard": "TEXT"})
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ingest_tokens"
        " (token_sha256 TEXT NOT NULL, run_id TEXT NOT NULL, dispatch_id TEXT)"
    )

    # Streamed status rows shared the verdicts table under a sentinel id.
    conn.execute("DELETE FROM verdicts WHERE delivery_id LIKE 'live-%'")

    runs = conn.execute("SELECT * FROM runs ORDER BY created_at, id").fetchall()
    for run in runs:
        run_id = run["id"]
        spec_row = conn.execute(
            "SELECT document FROM specs WHERE run_id = ?", (run_id,)
        ).fetchone()
        spec = json.loads(spec_row["document"]) if spec_row else {}
        materialized = (spec.get("selection") or {}).get("materialized") or {}
        planned = list(materialized.get("item_ids") or [])
        try:
            created_at = normalize_time(run["created_at"])
        except ValueError:
            created_at = run["created_at"]  # not a time 0.2 can read: left as it is
        conn.execute(
            "UPDATE runs SET planned_count = ?, created_at = ? WHERE id = ?",
            (len(planned), created_at, run_id),
        )

        # The token: keep the hash, drop the plaintext.
        results = spec.get("results") if isinstance(spec.get("results"), dict) else None
        token_hash = run["ingest_token_sha256"]
        if results and isinstance(results.get("token"), str):
            token_hash = token_hash or sha256_hex(results["token"].encode("utf-8"))
            del results["token"]
            conn.execute(
                "UPDATE specs SET document = ? WHERE run_id = ?",
                (json.dumps(spec, ensure_ascii=False), run_id),
            )
        if token_hash:
            conn.execute(
                "INSERT INTO ingest_tokens (token_sha256, run_id, dispatch_id) VALUES (?, ?, NULL)",
                (token_hash, run_id),
            )

        # Deliveries attached to the run directly get a dispatch of their own.
        orphan = conn.execute(
            "SELECT MIN(created_at) AS first FROM deliveries"
            " WHERE run_id = ? AND dispatch_id IS NULL",
            (run_id,),
        ).fetchone()
        if orphan["first"] is not None:
            ingest_id = new_ulid()
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, run_id, mode, declared_shards, created_at)"
                " VALUES (?, ?, 'ingest', 1, ?)",
                (ingest_id, run_id, orphan["first"]),
            )
            for table in ("deliveries", "verdicts"):
                conn.execute(
                    f"UPDATE {table} SET dispatch_id = ? WHERE run_id = ? AND dispatch_id IS NULL",
                    (ingest_id, run_id),
                )

        # One delivery per (dispatch, shard): the newest wins.
        for group in conn.execute(
            "SELECT dispatch_id, shard FROM deliveries WHERE run_id = ?"
            " GROUP BY dispatch_id, shard HAVING COUNT(*) > 1",
            (run_id,),
        ).fetchall():
            losers = conn.execute(
                "SELECT delivery_id FROM deliveries WHERE run_id = ? AND dispatch_id = ?"
                " AND shard = ? ORDER BY created_at DESC, delivery_id DESC",
                (run_id, group["dispatch_id"], group["shard"]),
            ).fetchall()[1:]
            for loser in losers:
                conn.execute("DELETE FROM verdicts WHERE delivery_id = ?", (loser["delivery_id"],))
                conn.execute(
                    "DELETE FROM deliveries WHERE delivery_id = ?", (loser["delivery_id"],)
                )

        # The lifecycle moves onto the dispatch.
        dispatches = conn.execute(
            "SELECT * FROM dispatches WHERE run_id = ? ORDER BY created_at, dispatch_id",
            (run_id,),
        ).fetchall()
        headline: tuple[str, str | None, str | None] = ("COMPOSED", None, None)
        for index, dispatch in enumerate(dispatches):
            latest = index == len(dispatches) - 1
            delivered = [
                row["shard"]
                for row in conn.execute(
                    "SELECT shard FROM deliveries WHERE dispatch_id = ? ORDER BY rowid",
                    (dispatch["dispatch_id"],),
                ).fetchall()
            ]
            declared = dispatch["declared_shards"] or 1
            names = list(delivered)
            filler = 1
            while len(names) < declared:  # shards 0.1 counted but never named
                if str(filler) not in names:
                    names.append(str(filler))
                filler += 1
            verdicts = [
                _verdict(row)
                for row in conn.execute(
                    "SELECT * FROM verdicts WHERE dispatch_id = ? ORDER BY rowid",
                    (dispatch["dispatch_id"],),
                ).fetchall()
            ]
            finalized = latest and run["state"] == "COMPLETE"
            if len(delivered) >= declared or finalized:
                state = "COMPLETE"
            elif latest and run["state"] in ("AWAITING_RESULTS", "COMPOSED"):
                state = "AWAITING_RESULTS"
            else:
                state = "STALE"
            completion = completed_at = None
            scored = verdicts
            if state == "COMPLETE":
                scored = verdicts + missing_verdicts(planned, verdicts)
                completion = completion_of(scored)
                completed_at = (run["completed_at"] if latest else None) or dispatch["created_at"]
            conn.execute(
                "UPDATE dispatches SET state = ?, shards = ?, completion = ?, completed_at = ?,"
                " summary = ?, error = ? WHERE dispatch_id = ?",
                (
                    state,
                    json.dumps(names),
                    completion,
                    completed_at,
                    json.dumps(summarize(scored)),
                    "interrupted before the upgrade to schema 2" if state == "STALE" else "",
                    dispatch["dispatch_id"],
                ),
            )
            headline = (state, completion, completed_at)
        conn.execute(
            "UPDATE runs SET state = ?, completion = ?, completed_at = ? WHERE id = ?",
            (*headline, run_id),
        )

    # 0.1 keyed deliveries on (run, content_hash); the constraint is part of
    # the table definition, so the table is rebuilt.
    conn.execute("DROP INDEX IF EXISTS idx_deliveries_run")
    conn.execute("ALTER TABLE deliveries RENAME TO deliveries_v1")
    conn.execute(
        "CREATE TABLE deliveries ("
        " delivery_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),"
        " dispatch_id TEXT NOT NULL, shard TEXT NOT NULL, content_hash TEXT NOT NULL,"
        " format TEXT NOT NULL, created_at TEXT NOT NULL,"
        " UNIQUE (run_id, dispatch_id, shard))"
    )
    conn.execute(
        "INSERT INTO deliveries SELECT delivery_id, run_id, dispatch_id, shard, content_hash,"
        " format, created_at FROM deliveries_v1 ORDER BY rowid"
    )
    conn.execute("DROP TABLE deliveries_v1")
