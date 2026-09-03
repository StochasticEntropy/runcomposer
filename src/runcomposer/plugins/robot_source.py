"""`robotframework` TestSource (DESIGN.md §6.1): walks .robot files via
robot.api, mints ``id = longname``, and owns any native-name normalization
quirks in ``resolve``.

Robot Framework is a dependency of this PLUGIN, installed via the
``runcomposer[robot]`` extra — never of the core.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from runcomposer.core.model import Item

__all__ = ["RobotFrameworkSource"]


def _require_robot():
    try:
        from robot.api import TestSuiteBuilder
    except ImportError as exc:
        raise ImportError(
            "the robotframework source needs Robot Framework — install the extra "
            "from a checkout: pip install '.[robot]'"
        ) from exc
    return TestSuiteBuilder


class RobotFrameworkSource:
    provider_id = "robotframework"

    @staticmethod
    def resolve_config_paths(options, resolve):
        """§8 opt-in: ``root``/``roots`` are test checkouts, relative to the
        config file's directory — so the same ``--config`` sees the same
        corpus from any working directory."""
        if "root" in options:
            options["root"] = resolve(options["root"])
        if isinstance(options.get("roots"), (list, tuple)):
            options["roots"] = [resolve(entry) for entry in options["roots"]]
        return options

    def __init__(self, root: str | None = None, roots: list[str] | None = None):
        """One suite root, or several.

        A repository often holds more than one suite tree — components,
        products, or a split between fast and slow suites — and they belong in
        one catalog, because a tag filter is asked of the corpus, not of a
        directory. Parsing each root separately (rather than pointing Robot at
        their common parent) is what keeps the ids stable: Robot names the
        root suite after the directory it was given, so a common parent would
        prepend a segment to every longname and the ids in this catalog would
        no longer be the ids in the ``output.xml`` that comes back.
        """
        builder_cls = _require_robot()
        if roots is not None and not isinstance(roots, (list, tuple)):
            raise ValueError("robotframework source 'roots' must be a list of paths")
        given = ([root] if root is not None else []) + list(roots or ())
        if not given:
            raise ValueError("the robotframework source needs 'root' or 'roots'")
        self._roots = [Path(entry) for entry in given]
        missing = [str(path) for path in self._roots if not path.exists()]
        if missing:
            raise ValueError(f"robotframework source root not found: {', '.join(missing)}")
        self._items: list[Item] = []
        for path in self._roots:
            self._collect(builder_cls().build(str(path)))
        self._by_longname = {item.id: item for item in self._items}
        # Two tests under one longname are one id for two tests: a selection
        # cannot name one without the other, and neither can the results that
        # come back. That is a defect in the suites, not in this catalog, so it
        # is reported (``runcomposer catalog``) rather than raised — a corpus
        # is not unusable because two of its tests are named alike.
        counts = Counter(item.id for item in self._items)
        self.duplicate_ids = sorted(item_id for item_id, n in counts.items() if n > 1)

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
