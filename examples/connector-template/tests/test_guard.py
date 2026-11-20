"""The connector's one import rule, kept by a test: of runcomposer it uses
``runcomposer.sdk`` (at run time) and ``runcomposer.testing`` (in tests) —
the surfaces that keep their meaning within a minor line — and nothing else.
Everything below them may change in any release."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED = {
    "src": {"runcomposer.sdk"},
    "tests": {"runcomposer.sdk", "runcomposer.testing"},
    "example": {"runcomposer.sdk"},
}


def runcomposer_imports(path: Path) -> list[tuple[str, int]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names = [node.module or ""]
        found += [(name, node.lineno) for name in names if name.split(".")[0] == "runcomposer"]
    return found


def test_only_the_stable_surfaces_are_imported():
    offending = []
    for folder, allowed in ALLOWED.items():
        for path in sorted((ROOT / folder).rglob("*.py")):
            for name, line in runcomposer_imports(path):
                if name not in allowed:
                    offending.append(f"{path.relative_to(ROOT)}:{line}: {name}")
    assert not offending, f"import only {sorted(set().union(*ALLOWED.values()))}: {offending}"


def test_nothing_reaches_for_runcomposer_internals_by_name():
    """A dynamic import by string would slip past the AST check."""
    for path in sorted(ROOT.rglob("*.py")):
        if path == Path(__file__).resolve():
            continue  # this file names them to look for them
        text = path.read_text(encoding="utf-8")
        for layer in ("runcomposer.core", "runcomposer.app", "runcomposer.plugins", "runcomposer.http"):
            assert layer not in text, f"{path.relative_to(ROOT)} names {layer}"
