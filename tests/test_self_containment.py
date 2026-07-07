"""Self-containment guard (DESIGN.md §11): the core imports no plugin and
contains no framework-specific vocabulary or native-name normalization.

This is the anti-leak litmus test carried over from the prototype's failure
mode: a "generic" core that quietly authors deployment- or framework-specific
behavior.
"""

import ast
import sys
from pathlib import Path

import runcomposer.core

CORE_DIR = Path(runcomposer.core.__file__).parent

# Framework/CI vocabulary that must never appear in core code (case-insensitive).
FORBIDDEN_TOKENS = [
    "runcomposer.plugins",
    "runcomposer.demo",
    "robot",  # the reference framework lives in plugins only
    "longname",
    "output.xml",
    "junit",
    "jenkins",
    "pytest",
    "nodeid",
]

# Third-party imports the core is allowed (see pyproject dependencies).
ALLOWED_THIRD_PARTY = {"jsonschema", "yaml"}


def core_files():
    return sorted(CORE_DIR.glob("*.py"))


def test_core_has_files():
    assert len(core_files()) >= 7


def test_no_forbidden_vocabulary_in_core():
    for path in core_files():
        text = path.read_text(encoding="utf-8").lower()
        for token in FORBIDDEN_TOKENS:
            assert token not in text, f"{path.name} contains forbidden token {token!r}"


def test_core_imports_only_stdlib_allowed_deps_and_itself():
    stdlib = set(sys.stdlib_module_names)
    for path in core_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:  # relative import within core — fine
                    continue
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in stdlib or top in ALLOWED_THIRD_PARTY:
                    continue
                assert name.startswith("runcomposer.core"), (
                    f"{path.name} imports {name!r} — core may only import stdlib, "
                    f"{sorted(ALLOWED_THIRD_PARTY)}, or runcomposer.core"
                )


def test_core_never_splits_or_normalizes_item_ids():
    """The core compares ids for equality only (DESIGN.md §2). Any .split(,
    partition, or lower/upper call on an id would be normalization creeping in.
    Tag matching (filter.py) is the one place case folding is a documented
    part of the grammar."""
    for path in core_files():
        text = path.read_text(encoding="utf-8")
        for needle in ("item_id.split", "item_id.partition", "id.lower(", "id.upper(", "id.casefold"):
            assert needle not in text, f"{path.name} contains {needle!r}"
