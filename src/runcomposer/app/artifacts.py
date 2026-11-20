"""Artifacts (DESIGN.md §6.4): reading references back as something a person
can follow, and mapping a request under ``/artifacts/`` to a real file.

``url_or_path`` is one string with two meanings, handled differently on
purpose:

- an absolute ``http``/``https`` URL is a **remote** artifact. It passes
  through verbatim as the href; runcomposer never fetches, proxies or
  validates it. No other scheme is treated as a URL — ``file:``, ``data:``
  and ``javascript:`` would be a link-injection vector.
- anything else is a **local path**, servable only when it really lies inside
  ``core.artifact_dir`` (symlinks resolved). A path outside stays visible —
  a runner may legitimately write elsewhere — with no href.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from runcomposer.core.errors import NotFound

__all__ = ["ARTIFACT_ROUTE_PREFIX", "Artifacts", "UnknownArtifact"]

ARTIFACT_ROUTE_PREFIX = "/artifacts/"


class UnknownArtifact(NotFound):
    code = "unknown-artifact"


def is_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


class Artifacts:
    def __init__(self, store: Any, root: Path):
        self._store = store
        self._root = Path(root)

    def root(self) -> Path | None:
        """The artifact directory, fully resolved; ``None`` while it does not
        exist — nothing can be inside a directory that is not there."""
        try:
            return self._root.resolve(strict=True)
        except OSError:
            return None

    def _relative(self, candidate: str, root: Path | None) -> str | None:
        if root is None:
            return None
        try:
            resolved = Path(candidate).resolve()
        except OSError:
            return None
        if resolved == root or root not in resolved.parents:
            return None
        return resolved.relative_to(root).as_posix()

    def list(self, run_id: str, dispatch_id: str | None = None) -> list[dict[str, Any]]:
        root = self.root()
        resolved = []
        for ref in self._store.artifact_refs(run_id, dispatch_id):
            kind, href = "external-path", None
            if is_http_url(ref.url_or_path):
                kind, href = "url", ref.url_or_path
            else:
                relative = self._relative(ref.url_or_path, root)
                if relative is not None:
                    kind, href = "local", ARTIFACT_ROUTE_PREFIX + quote(relative)
            resolved.append(
                {
                    "name": ref.name,
                    "media_type": ref.media_type,
                    "dispatch_id": ref.dispatch_id,
                    "shard": ref.shard,
                    "url_or_path": ref.url_or_path,
                    "kind": kind,
                    "href": href,
                }
            )
        return resolved

    def resolve_file(self, artifact_path: str) -> Path:
        """Containment, checked after full resolution — so ``..`` segments,
        absolute paths and symlinks out of the tree are one rule. A refusal
        is indistinguishable from a miss: probing learns nothing."""
        root = self.root()
        if root is None or not artifact_path:
            raise UnknownArtifact(f"unknown artifact {artifact_path!r}")
        candidate = (root / artifact_path).resolve()
        if candidate == root or root not in candidate.parents:
            raise UnknownArtifact(f"unknown artifact {artifact_path!r}")
        if not candidate.is_file():
            raise UnknownArtifact(
                f"unknown artifact {artifact_path!r} — if it was recorded, the file is gone "
                "(artifacts are removed with their run: `core.retention` / `runcomposer gc`)"
            )
        return candidate
