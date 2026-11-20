"""The catalog: every configured TestSource as one (DESIGN.md §6.1, §8).

Two jobs the individual sources should not each solve:

- **Merging.** Several ``sources:`` entries form one id space. An id that
  more than one item carries — within a source or across sources — is
  reported in ``duplicate_ids``; ``resolve`` asks each source in turn.
- **Staying current.** A source is loaded once and kept. A source that offers
  ``fingerprint()`` is reloaded when that token changes (checked cheaply, at
  most once per ``check_interval_s``); one that does not is reloaded on
  request (``refresh(force=True)``) and before every dispatch, where the
  drift check needs the corpus as it is *now*. Editing the corpus therefore
  never requires a restart.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from typing import Any

from runcomposer.core.hashing import sha256_hex
from runcomposer.core.model import Item
from runcomposer.core.timeutil import utc_now

from .config import Config, PluginSpec

__all__ = ["Catalog"]

log = logging.getLogger("runcomposer.catalog")


class _Loaded:
    def __init__(self, spec: PluginSpec, source: Any, fingerprint: str | None = None):
        self.spec = spec
        self.source = source
        #: The corpus state this source was built from. On a reload it is the
        #: token probed *before* building: if the corpus changes while it is
        #: being read, the next check sees a difference and reloads again —
        #: a token taken afterwards would mark stale content as current.
        self.fingerprint = fingerprint if fingerprint is not None else _fingerprint(source)


def _fingerprint(source: Any) -> str | None:
    probe = getattr(source, "fingerprint", None)
    if probe is None:
        return None
    try:
        return str(probe())
    except Exception:  # a broken probe means "cannot tell" — reload on request
        log.warning("source fingerprint failed", exc_info=True)
        return None


class Catalog:
    """A TestSource over all configured sources."""

    __test__ = False

    def __init__(self, config: Config, *, check_interval_s: float = 1.0):
        self._config = config
        self._interval = check_interval_s
        self._lock = threading.RLock()
        self._loaded: list[_Loaded] | None = None
        self._checked_at = 0.0
        self._derived: dict[str, Any] | None = None
        self.loaded_at: str | None = None

    # -- loading -------------------------------------------------------------

    def _load(self, spec: PluginSpec, fingerprint: str | None = None) -> _Loaded:
        return _Loaded(spec, self._config.build(spec), fingerprint)

    def _sources(self) -> list[_Loaded]:
        with self._lock:
            if self._loaded is None:
                self._loaded = [self._load(spec) for spec in self._config.source_specs()]
                self._derived = None
                self.loaded_at = utc_now()
                self._checked_at = time.monotonic()
            elif time.monotonic() - self._checked_at >= self._interval:
                try:
                    self._reload_changed(rebuild_unfingerprinted=False)
                except Exception as exc:
                    # The automatic check met a corpus it cannot read — a file
                    # caught mid-save, typically. That must not fail whoever
                    # happened to ask: keep what is loaded, look again at the
                    # next check. An explicit refresh() still raises.
                    self._checked_at = time.monotonic()
                    log.warning("catalog reload failed — keeping the loaded catalog: %s", exc)
            return self._loaded

    def _reload_changed(self, *, rebuild_unfingerprinted: bool) -> bool:
        assert self._loaded is not None
        # Built aside and swapped in whole: a source that fails to load leaves
        # the catalog exactly as it was.
        replacement = list(self._loaded)
        changed = False
        for index, loaded in enumerate(self._loaded):
            current = None if loaded.fingerprint is None else _fingerprint(loaded.source)
            if loaded.fingerprint is None:
                stale = rebuild_unfingerprinted
            else:
                stale = current != loaded.fingerprint
            if stale:
                replacement[index] = self._load(loaded.spec, current)
                changed = True
        self._checked_at = time.monotonic()
        if changed:
            self._loaded = replacement
            self._derived = None
            self.loaded_at = utc_now()
            log.info("catalog reloaded")
        return changed

    def refresh(self, *, force: bool = False) -> bool:
        """Bring the catalog up to date now. With ``force`` every source that
        cannot say whether it changed is rebuilt. Returns whether anything
        was reloaded."""
        with self._lock:
            if self._loaded is None:
                self._sources()
                return True
            return self._reload_changed(rebuild_unfingerprinted=force)

    def fresh(self) -> Catalog:
        """The catalog as the corpus is right now — what a dispatch gets."""
        self.refresh(force=True)
        return self

    # -- derived views ----------------------------------------------------------

    def _view(self) -> dict[str, Any]:
        with self._lock:
            sources = self._sources()
            if self._derived is None:
                items: list[Item] = []
                duplicates: set[str] = set()
                for loaded in sources:
                    items.extend(loaded.source.items())
                    duplicates.update(getattr(loaded.source, "duplicate_ids", ()) or ())
                counts = Counter(item.id for item in items)
                duplicates.update(item_id for item_id, n in counts.items() if n > 1)
                if len(sources) == 1:
                    snapshot = sources[0].source.snapshot()
                else:
                    joined = "\n".join(
                        f"{loaded.spec.key}={loaded.source.snapshot()}" for loaded in sources
                    )
                    snapshot = "sha256:" + sha256_hex(joined.encode("utf-8"))
                providers = list(dict.fromkeys(entry.source.provider_id for entry in sources))
                self._derived = {
                    "items": items,
                    "ids": frozenset(counts),
                    "duplicates": sorted(duplicates),
                    "snapshot": snapshot,
                    "provider": "+".join(providers),
                    "tags": Counter(tag for item in items for tag in item.tags),
                }
            return self._derived

    # -- the TestSource contract ---------------------------------------------------

    @property
    def provider_id(self) -> str:
        return str(self._view()["provider"])

    @property
    def duplicate_ids(self) -> list[str]:
        return list(self._view()["duplicates"])

    def items(self) -> list[Item]:
        return list(self._view()["items"])

    def item_ids(self) -> frozenset[str]:
        ids: frozenset[str] = self._view()["ids"]
        return ids

    def snapshot(self) -> str:
        return str(self._view()["snapshot"])

    def resolve(self, native_name: str) -> str | None:
        for loaded in self._sources():
            item_id = loaded.source.resolve(native_name)
            if item_id is not None:
                return str(item_id)
        return None

    # -- what the surfaces show -------------------------------------------------------

    def tag_counts(self) -> dict[str, int]:
        return dict(self._view()["tags"])

    def stats(self) -> dict[str, Any]:
        view = self._view()
        return {
            "items": len(view["items"]),
            "tags": len(view["tags"]),
            "snapshot": view["snapshot"],
            "provider": view["provider"],
            "sources": [loaded.spec.key for loaded in self._sources()],
            "duplicate_ids": len(view["duplicates"]),
            "loaded_at": self.loaded_at,
        }
