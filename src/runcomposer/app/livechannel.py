"""Live status and log lines out of worker processes (DESIGN.md §6.2), for
any RunStore.

A worker cannot be handed a store: stores are not picklable in general, and a
third-party one may not be reachable from a child process at all. So the
channel carries no store. It appends JSON lines to a file in the dispatch's
work directory — one file per writing process, so writers never interleave —
and the dispatching process tails those files and records what it reads.

Stdlib only, and imported on its own by workers.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

__all__ = ["FileLiveChannel", "LiveTail"]


class FileLiveChannel:
    """Picklable: its whole state is one directory name."""

    def __init__(self, directory: str):
        self.directory = directory

    def live(
        self, shard: str, item_id: str, status: str, *, duration_ms: int = 0, message: str = ""
    ) -> None:
        line = json.dumps(
            {
                "shard": shard,
                "item_id": item_id,
                "status": status,
                "duration_ms": int(duration_ms),
                "message": message,
            },
            ensure_ascii=False,
        )
        self._append(line)

    def log(self, message: str, level: str = "info") -> None:
        """A line for the run's event stream, from a worker process."""
        self._append(json.dumps({"log": str(message), "level": str(level)}, ensure_ascii=False))

    def _append(self, line: str) -> None:
        path = os.path.join(self.directory, f"live-{os.getpid()}.jsonl")
        # Opened per line: the line is on disk when this returns, and a worker
        # that is killed mid-run has lost nothing it reported.
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class LiveTail:
    """Reads what :class:`FileLiveChannel` writers appended since last time.
    Only complete lines are returned; a line still being written waits for
    the next read."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._offsets: dict[str, int] = {}

    def read(self) -> Iterator[dict[str, Any]]:
        try:
            files = sorted(self.directory.glob("live-*.jsonl"))
        except OSError:
            return
        for file in files:
            offset = self._offsets.get(file.name, 0)
            try:
                with open(file, "rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
            except OSError:
                continue
            complete = chunk.rfind(b"\n") + 1
            if not complete:
                continue
            self._offsets[file.name] = offset + complete
            for raw in chunk[:complete].splitlines():
                try:
                    entry = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(entry, dict):
                    yield entry
