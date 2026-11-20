"""Self-containment guards (DESIGN.md §1, §11): the two import rules, and
the vocabulary the core is allowed.

1. **The core is the bottom.** It imports nothing above itself — no app
   layer, no transport, no SDK facade, no plugin — and contains no
   framework-specific vocabulary or native-name normalization. This is the
   anti-leak litmus test: a "generic" core that quietly authors deployment-
   or framework-specific behaviour is how such a tool becomes un-releasable.
2. **A connector sees the SDK and nothing else.** Every reference plugin
   imports from the ``runcomposer`` package only ``runcomposer.sdk`` — the
   same surface a connector outside this repository has. That the reference
   set can be written against it is the proof that the surface is sufficient.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import runcomposer
import runcomposer.core

PACKAGE_DIR = Path(runcomposer.__file__).parent
CORE_DIR = Path(runcomposer.core.__file__).parent
PLUGINS_DIR = PACKAGE_DIR / "plugins"
DEMO_DIR = PACKAGE_DIR / "demo"

# Vocabulary that must never appear in core code (case-insensitive): the
# layers above the core, and framework/CI/persistence words. Storage engines
# and web frameworks live in plugins and the app layer; the core knows only
# DESIGN.md §2 vocabulary.
FORBIDDEN_TOKENS = [
    "runcomposer.plugins",
    "runcomposer.demo",
    "runcomposer.app",
    "runcomposer.http",
    "runcomposer.cli",
    "runcomposer.sdk",
    "robot",  # the reference framework lives in plugins only
    "longname",
    "output.xml",
    "junit",
    "jenkins",
    "pytest",
    "nodeid",
    "sqlite",
    "postgres",
    "fastapi",
    "uvicorn",
]

# Third-party imports the core is allowed (see pyproject dependencies).
ALLOWED_THIRD_PARTY = {"jsonschema", "yaml"}


def core_files() -> list[Path]:
    return sorted(CORE_DIR.glob("*.py"))


def plugin_files() -> list[Path]:
    return sorted(PLUGINS_DIR.glob("*.py"))


def imports_of(source: str, package: list[str]) -> list[tuple[str, int]]:
    """Every module a piece of source imports — at module level *and* inside
    functions and classes — as ``(absolute name, line)``. A relative import
    is resolved against ``package``, the dotted package the source lives in."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                found.append((".".join([*base, *filter(None, [node.module])]), node.lineno))
            else:
                found.append((node.module or "", node.lineno))
    return sorted(found, key=lambda entry: entry[1])


def imported_modules(path: Path) -> list[tuple[str, int]]:
    package = ["runcomposer", *path.relative_to(PACKAGE_DIR).parts[:-1]]
    return imports_of(path.read_text(encoding="utf-8"), package)


def outside_the_sdk(imports: list[tuple[str, int]], allowed_prefixes: tuple[str, ...]) -> list[str]:
    """The runcomposer modules among ``imports`` that are neither the SDK nor
    under one of the allowed prefixes."""
    offending = []
    for name, line in imports:
        if name != "runcomposer" and not name.startswith("runcomposer."):
            continue
        if name == "runcomposer.sdk" or name.startswith(allowed_prefixes):
            continue
        offending.append(f"line {line}: {name}")
    return offending


# -- rule 1: the core is the bottom ------------------------------------------------


def test_core_has_files():
    names = {path.name for path in core_files()}
    assert {
        "errors.py", "events.py", "filter.py", "hashing.py", "ids.py", "lifecycle.py",
        "marker.py", "model.py", "parameters.py", "planning.py", "ports.py", "registry.py",
        "selection.py", "spec.py", "taxonomy.py", "timeutil.py", "xmlsafe.py",
    } <= names


def test_no_forbidden_vocabulary_in_core():
    for path in core_files():
        text = path.read_text(encoding="utf-8").lower()
        for token in FORBIDDEN_TOKENS:
            assert token not in text, f"core/{path.name} contains forbidden token {token!r}"


def test_core_imports_only_stdlib_allowed_deps_and_itself():
    stdlib = set(sys.stdlib_module_names)
    for path in core_files():
        for name, line in imported_modules(path):
            top = name.split(".")[0]
            if top in stdlib or top in ALLOWED_THIRD_PARTY:
                continue
            assert name == "runcomposer.core" or name.startswith("runcomposer.core."), (
                f"core/{path.name}:{line} imports {name!r} — core may only import stdlib, "
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
            assert needle not in text, f"core/{path.name} contains {needle!r}"


def test_core_protocols_are_not_runtime_checkable():
    """A ``runtime_checkable`` protocol invites ``isinstance`` checks that
    only look at member *names* — a conformance claim nothing verifies.
    Conformance is a matter of behaviour, checked by tests."""
    for path in core_files():
        assert "runtime_checkable" not in path.read_text(encoding="utf-8"), path.name


def test_the_layers_above_do_not_import_plugins():
    """Plugins are loaded by name (entry points, ``module:``), never imported
    by the application layer or a transport — otherwise a "reference" plugin
    is quietly a dependency of the tool."""
    for layer in ("app", "http", "cli"):
        for path in sorted((PACKAGE_DIR / layer).rglob("*.py")):
            for name, line in imported_modules(path):
                assert not name.startswith("runcomposer.plugins"), (
                    f"{layer}/{path.name}:{line} imports {name!r}"
                )


# -- rule 2: a connector sees the SDK and nothing else ------------------------------


def test_there_are_plugins_to_check():
    names = {path.name for path in plugin_files()}
    assert {
        "sqlite_store.py", "manifest_source.py", "robot_source.py",
        "demo_runner.py", "robot_pool.py", "robot_listener.py", "jenkins.py",
        "command_runner.py", "observers.py", "verdicts_parser.py", "junit_parser.py",
        "robot_output_parser.py",
    } <= names


#: The one sibling import among the reference plugins, and why it is one:
#: the live-status listener is the worker-side half of ``robot-pool``. Robot
#: Framework loads a listener by import path inside the worker process, so it
#: is a module of its own — but it is that connector's code, not another
#: plugin's. Every other plugin module imports nothing of runcomposer but the
#: SDK (the demo corpus source lives in ``manifest_source`` for that reason).
SIBLING_IMPORTS = {"robot_pool.py": {"runcomposer.plugins.robot_listener"}}


def test_plugins_import_only_the_sdk_from_runcomposer():
    """Everything of runcomposer a plugin uses comes through
    ``runcomposer.sdk`` — including imports made inside a function, which is
    where a worker process would hide one — and plugins do not import each
    other, except for the one justified case above."""
    offending = {}
    for path in plugin_files():
        imports = [
            (name, line)
            for name, line in imported_modules(path)
            if name not in SIBLING_IMPORTS.get(path.name, set())
        ]
        if found := outside_the_sdk(imports, ()):
            offending[path.name] = found
    assert not offending, (
        "reference plugins must import only runcomposer.sdk from the runcomposer package "
        f"(sibling exceptions: SIBLING_IMPORTS): {offending}"
    )


def test_the_sibling_exception_is_still_needed():
    """An exception that no longer matches an import is stale — remove it."""
    for filename, allowed in SIBLING_IMPORTS.items():
        names = {name for name, _line in imported_modules(PLUGINS_DIR / filename)}
        assert allowed <= names, f"SIBLING_IMPORTS[{filename!r}] lists {allowed - names}, not imported"


def test_plugins_do_not_name_the_layers_they_may_not_import():
    """The AST check above sees ``import`` statements. A dynamic import by
    string would slip past it, so the inner layers are not *named* in plugin
    source at all."""
    for path in plugin_files():
        text = path.read_text(encoding="utf-8")
        for layer in ("runcomposer.core", "runcomposer.app", "runcomposer.http", "runcomposer.cli"):
            assert layer not in text, f"plugins/{path.name} names {layer!r}"


def test_the_import_scanner_sees_function_level_and_relative_imports():
    """The guard is only as good as its scanner: pin that it finds an import
    hidden in a function and resolves relative ones."""
    source = (
        "from runcomposer.sdk import Verdict\n"
        "from . import manifest_source\n"
        "from .robot_listener import LiveStatusListener\n"
        "from .. import sdk\n"
        "def worker():\n"
        "    from runcomposer.core.model import Item\n"
        "    import runcomposer.app.service, json\n"
        "    class Inner:\n"
        "        from ..core import ports\n"
    )
    found = imports_of(source, ["runcomposer", "plugins"])
    assert [name for name, _line in found] == [
        "runcomposer.sdk",
        "runcomposer.plugins",
        "runcomposer.plugins.robot_listener",
        "runcomposer",
        "runcomposer.core.model",
        "runcomposer.app.service",
        "json",
        "runcomposer.core",
    ]
    assert outside_the_sdk(found, ("runcomposer.plugins",)) == [
        "line 4: runcomposer",
        "line 6: runcomposer.core.model",
        "line 7: runcomposer.app.service",
        "line 9: runcomposer.core",
    ]


def test_the_demo_imports_only_the_sdk():
    """`runcomposer demo` drives the tool the way an embedder would — so it
    is written against the embedding surface, and proves that one too."""
    imports = imported_modules(DEMO_DIR / "demo.py")
    assert outside_the_sdk(imports, ()) == []
    assert {name for name, _ in imports if name.startswith("runcomposer")} == {"runcomposer.sdk"}


def test_the_sdk_is_built_from_the_core_at_import_time():
    """At module level ``runcomposer.sdk`` pulls in the core only; the
    embedding names are resolved on first use (tests/test_sdk.py shows the
    behaviour — this pins the structure)."""
    tree = ast.parse((PACKAGE_DIR / "sdk.py").read_text(encoding="utf-8"))
    checked = 0
    for node in tree.body:  # module-level statements only
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("runcomposer"):
            checked += 1
            assert (node.module or "").startswith("runcomposer.core."), (
                f"sdk.py:{node.lineno} imports {node.module!r} at module level"
            )
    assert checked > 10
