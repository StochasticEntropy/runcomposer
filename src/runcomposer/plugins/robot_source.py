"""`robotframework` TestSource (DESIGN.md §6.1): walks .robot files via
robot.api, mints ``id = longname``, and owns any native-name normalization
quirks in ``resolve``.

Robot Framework is a dependency of this PLUGIN, installed via the
``runcomposer[robot]`` extra — never of the core.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any

from runcomposer.sdk import ConfigError, Item, SourceBase, canonical_json, sha256_hex

__all__ = ["RobotFrameworkSource"]

#: What a suite tree is made of — the files whose change can change the catalog.
_SUITE_SUFFIXES = (".robot", ".resource", ".rst", ".tsv", ".txt", ".py", ".yaml", ".json")


def _require_robot() -> Any:
    try:
        from robot.api import TestSuiteBuilder
    except ImportError as exc:
        raise ConfigError(
            "the robotframework source needs Robot Framework — install the extra: "
            "pip install 'runcomposer[robot]'"
        ) from exc
    return TestSuiteBuilder


class RobotFrameworkSource(SourceBase):
    provider_id = "robotframework"
    PATH_OPTIONS = ("root", "roots")

    def __init__(self, root: str | None = None, roots: list[str] | None = None):
        """One suite root, or several.

        A repository often holds more than one suite tree, and they belong in
        one catalog. Each root is parsed on its own (rather than pointing
        Robot at a common parent) because Robot names the root suite after
        the directory it is given: a common parent would prepend a segment to
        every longname, and the ids here would no longer be the ids in the
        ``output.xml`` that comes back.
        """
        builder_cls = _require_robot()
        if roots is not None and not isinstance(roots, (list, tuple)):
            raise ConfigError("robotframework source 'roots' must be a list of paths")
        given = ([root] if root is not None else []) + list(roots or ())
        if not given:
            raise ConfigError("the robotframework source needs 'root' or 'roots'")
        self.roots = [Path(entry) for entry in given]
        missing = [str(path) for path in self.roots if not path.exists()]
        if missing:
            raise ConfigError(f"robotframework source root not found: {', '.join(missing)}")
        self._stamp = self.fingerprint()
        self._items: list[Item] = []
        for path in self.roots:
            self._collect(builder_cls().build(str(path)), str(path))
        self._by_longname = {item.id: item for item in self._items}
        # Two tests under one longname are one id for two tests: a selection
        # cannot name one without the other. A defect in the suites, so it is
        # reported (`runcomposer catalog`, `doctor`) rather than raised.
        counts = Counter(item.id for item in self._items)
        self.duplicate_ids = sorted(item_id for item_id, n in counts.items() if n > 1)

    def fingerprint(self) -> str:
        """Cheap staleness token: name, size and modification time of every
        suite file under the roots — no parsing."""
        entries: list[tuple[str, int, int]] = []
        for root in self.roots:
            if root.is_file():
                stat = root.stat()
                entries.append((str(root), stat.st_size, stat.st_mtime_ns))
                continue
            for directory, _dirs, files in os.walk(root):
                for name in files:
                    if name.endswith(_SUITE_SUFFIXES):
                        path = os.path.join(directory, name)
                        try:
                            stat = os.stat(path)
                        except OSError:
                            continue
                        entries.append((path, stat.st_size, stat.st_mtime_ns))
        return sha256_hex(canonical_json(sorted(entries)))

    def _collect(self, suite: Any, root: str) -> None:
        for test in suite.tests:
            self._items.append(
                Item(
                    id=test.longname,  # the native id space (§2, §6.1)
                    name=test.name,
                    tags=tuple(str(tag) for tag in test.tags),
                    hierarchy=tuple(test.longname.split(".")[:-1]),
                    meta={"source": str(getattr(suite, "source", "") or ""), "root": root},
                )
            )
        for child in suite.suites:
            self._collect(child, root)

    def items(self) -> list[Item]:
        return list(self._items)

    def snapshot(self) -> str:
        return "sha256:" + sha256_hex(
            canonical_json([{"id": item.id, "tags": list(item.tags)} for item in self._items])
        )

    def resolve(self, native_name: str) -> str | None:
        """output.xml longnames match builder longnames exactly; whitespace
        trimming is the one normalization this source owns."""
        candidate = native_name.strip()
        return candidate if candidate in self._by_longname else None
