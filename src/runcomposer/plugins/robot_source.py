"""`robotframework` TestSource (DESIGN.md §6.1): walks .robot files via
robot.api, mints ``id = longname``, and owns any native-name normalization
quirks in ``resolve``.

Robot Framework is a dependency of this PLUGIN, installed via the
``runcomposer[robot]`` extra — never of the core.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from runcomposer.core.model import Item

__all__ = ["RobotFrameworkSource"]


def _require_robot():
    try:
        from robot.api import TestSuiteBuilder
    except ImportError as exc:
        raise ImportError(
            "the robotframework source needs Robot Framework — install the extra: "
            "pip install 'runcomposer[robot]'"
        ) from exc
    return TestSuiteBuilder


class RobotFrameworkSource:
    provider_id = "robotframework"

    def __init__(self, root: str):
        builder_cls = _require_robot()
        self._root = Path(root)
        if not self._root.exists():
            raise ValueError(f"robotframework source root not found: {root}")
        suite = builder_cls().build(str(self._root))
        self._items: list[Item] = []
        self._collect(suite)
        self._by_longname = {item.id: item for item in self._items}

    def _collect(self, suite) -> None:
        for test in suite.tests:
            self._items.append(
                Item(
                    id=test.longname,  # the native id space (§2, §6.1)
                    name=test.name,
                    tags=tuple(str(tag) for tag in test.tags),
                    hierarchy=tuple(test.longname.split(".")[:-1]),
                    meta={"source": str(getattr(suite, "source", "") or "")},
                )
            )
        for child in suite.suites:
            self._collect(child)

    def items(self) -> list[Item]:
        return list(self._items)

    def snapshot(self) -> str:
        canonical = json.dumps(
            [{"id": item.id, "tags": list(item.tags)} for item in self._items],
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def resolve(self, native_name: str) -> str | None:
        """output.xml longnames match builder longnames exactly; whitespace
        trimming is the one normalization this source owns."""
        candidate = native_name.strip()
        return candidate if candidate in self._by_longname else None
