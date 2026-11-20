"""The ``http-checks`` TestSource: a YAML file of HTTP checks is the catalog.

```yaml
checks:
  - id: shop.cart.add          # the item id — any stable string you choose
    tags: [Cart, Smoke]
    path: /cart/add?sku=BOOK-1
    method: POST               # default GET
    expect_status: 200         # default 200
    expect_text: added         # optional: the body must contain it
```

What a source owes runcomposer (``runcomposer.sdk.TestSource``): the items,
a snapshot hash of what a selection depends on (ids and tags), and
``resolve`` from a result's native name to an item id. Here a check's id *is*
its native name — the runner reports by id — so ``resolve`` is identity.
``fingerprint`` lets runcomposer reload the catalog when the file changes.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from runcomposer.sdk import ConfigError, Item, SourceBase, canonical_json, sha256_hex

__all__ = ["HttpChecksSource"]

_METHODS = ("GET", "POST", "PUT", "DELETE", "HEAD")


class HttpChecksSource(SourceBase):
    provider_id = "http-checks"
    #: Options that are file paths, resolved against the config file's directory.
    PATH_OPTIONS = ("path",)

    def __init__(self, path: str):
        self.path = Path(path)
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"http-checks: cannot read {self.path}: {exc}") from None
        checks = data.get("checks") if isinstance(data, dict) else None
        if not isinstance(checks, list) or not checks:
            raise ConfigError(f"http-checks: {self.path} must hold a non-empty 'checks' list")
        self._items = [self._item(entry, index) for index, entry in enumerate(checks)]
        ids = [item.id for item in self._items]
        if len(set(ids)) != len(ids):
            raise ConfigError(f"http-checks: {self.path}: check ids must be unique")
        self._ids = set(ids)
        self._snapshot = "sha256:" + sha256_hex(
            canonical_json([{"id": item.id, "tags": list(item.tags)} for item in self._items])
        )

    def _item(self, entry: Any, index: int) -> Item:
        where = f"http-checks: {self.path}: checks[{index}]"
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            raise ConfigError(f"{where} needs a string 'id'")
        if not isinstance(entry.get("path"), str) or not entry["path"].startswith("/"):
            raise ConfigError(f"{where} needs a 'path' starting with '/'")
        method = str(entry.get("method", "GET")).upper()
        if method not in _METHODS:
            raise ConfigError(f"{where}: method must be one of {_METHODS}")
        tags = entry.get("tags") or []
        if not isinstance(tags, list) or not all(isinstance(tag, str) and tag for tag in tags):
            raise ConfigError(f"{where}: tags must be a list of strings")
        return Item(
            id=entry["id"],
            tags=tuple(tags),
            name=str(entry.get("name") or entry["id"]),
            # The request travels with the item: the runner reads it from the
            # catalog it is handed (ctx.source), not from the spec document.
            meta={
                "method": method,
                "path": entry["path"],
                "expect_status": int(entry.get("expect_status", 200)),
                "expect_text": str(entry.get("expect_text") or ""),
            },
        )

    def items(self) -> list[Item]:
        return list(self._items)

    def snapshot(self) -> str:
        return self._snapshot

    def resolve(self, native_name: str) -> str | None:
        return native_name if native_name in self._ids else None

    def fingerprint(self) -> str:
        try:
            stat = os.stat(self.path)
        except OSError:
            return "missing"
        return f"{stat.st_size}:{stat.st_mtime_ns}"
