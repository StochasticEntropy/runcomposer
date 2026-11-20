"""The connector's one import rule, kept by a test: of runcomposer it uses
``runcomposer.sdk`` — the module and its toolkits, ``runcomposer.sdk.robot``
and ``runcomposer.sdk.jenkins`` — at run time, and ``runcomposer.testing``
(with ``runcomposer.testing.jenkins``) in tests: the surfaces that keep their
meaning within a minor line. Nothing else — everything below them may change
in any release."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: Per folder, the packages that may be imported — each with its submodules.
ALLOWED = {
    "src": ("runcomposer.sdk",),
    "tests": ("runcomposer.sdk", "runcomposer.testing"),
    "example": ("runcomposer.sdk",),
}


def is_allowed(name: str, allowed: tuple[str, ...]) -> bool:
    """``runcomposer.sdk`` allows ``runcomposer.sdk`` and
    ``runcomposer.sdk.robot`` — and not ``runcomposer.sdkx``."""
    return any(name == package or name.startswith(package + ".") for package in allowed)


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
                if not is_allowed(name, allowed):
                    offending.append(f"{path.relative_to(ROOT)}:{line}: {name}")
    every = sorted({package for allowed in ALLOWED.values() for package in allowed})
    assert not offending, f"import only {every} and their submodules: {offending}"


def test_the_toolkits_and_the_kit_s_stand_in_are_part_of_those_surfaces():
    """Imported here for real, so that the rule above is held to what the
    connector guide allows: a connector that composes the Robot or the
    Jenkins toolkit, or tests against the kit's Jenkins stand-in, passes."""
    import runcomposer.sdk.jenkins
    import runcomposer.sdk.robot
    import runcomposer.testing.jenkins

    assert runcomposer.sdk.jenkins.JenkinsClient and runcomposer.sdk.robot.supervise
    assert runcomposer.testing.jenkins.FakeJenkins
    tests = ALLOWED["tests"]
    assert is_allowed("runcomposer.sdk.robot", ALLOWED["src"])
    assert is_allowed("runcomposer.testing.jenkins", tests)
    for internal in ("runcomposer", "runcomposer.sdkx", "runcomposer.cli", "runcomposer.testing_helpers"):
        assert not is_allowed(internal, tests), internal
    assert not is_allowed("runcomposer.testing", ALLOWED["src"]), "the kit is for tests"


def test_nothing_reaches_for_runcomposer_internals_by_name():
    """A dynamic import by string would slip past the AST check."""
    for path in sorted(ROOT.rglob("*.py")):
        if path == Path(__file__).resolve():
            continue  # this file names them to look for them
        text = path.read_text(encoding="utf-8")
        for layer in ("runcomposer.core", "runcomposer.app", "runcomposer.plugins", "runcomposer.http"):
            assert layer not in text, f"{path.relative_to(ROOT)} names {layer}"
