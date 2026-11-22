"""Self-containment guards (DESIGN.md §1, §11): the two import rules, and
the vocabulary the core is allowed.

1. **The core is the bottom.** It imports nothing above itself — no app
   layer, no transport, no SDK facade, no plugin — and contains no
   framework-specific vocabulary or native-name normalization. This is the
   anti-leak litmus test: a "generic" core that quietly authors deployment-
   or framework-specific behaviour is how such a tool becomes un-releasable.
2. **A connector sees the SDK and nothing else.** Every reference plugin
   imports from the ``runcomposer`` package only ``runcomposer.sdk`` and its
   toolkits (``runcomposer.sdk.robot``, ``runcomposer.sdk.jenkins``) — the
   same surface a connector outside this repository has. That the reference
   set can be written against it is the proof that the surface is sufficient.
3. **The toolkits are connector code too.** They are built from
   ``runcomposer.sdk`` alone: what they export could have been written
   outside this repository.

Every scanner below takes the directory it scans and walks it at any depth
(a module in a sub-package is checked like any other); the guards call them
on the installed package, and the self-tests at the end call them on a
planted tree with a violation in a nested sub-package.
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
SDK_DIR = PACKAGE_DIR / "sdk"
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


# -- scanners: each takes the directory it scans --------------------------------------


def python_files(directory: Path) -> list[Path]:
    """Every module under ``directory``, at any depth."""
    return sorted(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)


def core_files(core_dir: Path = CORE_DIR) -> list[Path]:
    return python_files(core_dir)


def plugin_files(plugins_dir: Path = PLUGINS_DIR) -> list[Path]:
    return python_files(plugins_dir)


def toolkit_files(sdk_dir: Path = SDK_DIR) -> list[Path]:
    """The SDK's toolkits: every module under ``sdk/`` but the facade itself."""
    return [path for path in python_files(sdk_dir) if path != sdk_dir / "__init__.py"]


def shown(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


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


def imported_modules(path: Path, package_dir: Path = PACKAGE_DIR) -> list[tuple[str, int]]:
    """The imports of the module at ``path``, which lives in the
    ``runcomposer`` package rooted at ``package_dir``."""
    package = ["runcomposer", *path.relative_to(package_dir).parts[:-1]]
    return imports_of(path.read_text(encoding="utf-8"), package)


def outside_the_sdk(imports: list[tuple[str, int]], allowed_prefixes: tuple[str, ...]) -> list[str]:
    """The runcomposer modules among ``imports`` that are neither the SDK nor
    under one of the allowed prefixes."""
    offending = []
    for name, line in imports:
        if name != "runcomposer" and not name.startswith("runcomposer."):
            continue
        if name == "runcomposer.sdk" or name.startswith("runcomposer.sdk."):
            continue  # the SDK and its toolkits
        if name.startswith(allowed_prefixes):
            continue
        offending.append(f"line {line}: {name}")
    return offending


def forbidden_vocabulary(core_dir: Path) -> list[str]:
    """``file:line: token`` for every forbidden token in core source."""
    found = []
    for path in core_files(core_dir):
        for number, line in enumerate(path.read_text(encoding="utf-8").lower().splitlines(), 1):
            found.extend(
                f"core/{shown(path, core_dir)}:{number}: {token!r}"
                for token in FORBIDDEN_TOKENS if token in line
            )
    return found


def core_import_violations(core_dir: Path, package_dir: Path) -> list[str]:
    stdlib = set(sys.stdlib_module_names)
    found = []
    for path in core_files(core_dir):
        for name, line in imported_modules(path, package_dir):
            top = name.split(".")[0]
            if top in stdlib or top in ALLOWED_THIRD_PARTY:
                continue
            if name != "runcomposer.core" and not name.startswith("runcomposer.core."):
                found.append(f"core/{shown(path, core_dir)}:{line}: imports {name!r}")
    return found


def id_normalization(core_dir: Path) -> list[str]:
    needles = ("item_id.split", "item_id.partition", "id.lower(", "id.upper(", "id.casefold")
    return [
        f"core/{shown(path, core_dir)}: {needle!r}"
        for path in core_files(core_dir)
        for needle in needles if needle in path.read_text(encoding="utf-8")
    ]


def runtime_checkable_protocols(core_dir: Path) -> list[str]:
    return [
        f"core/{shown(path, core_dir)}"
        for path in core_files(core_dir) if "runtime_checkable" in path.read_text(encoding="utf-8")
    ]


def plugin_imports_beyond_the_sdk(plugins_dir: Path, package_dir: Path) -> dict[str, list[str]]:
    offending = {}
    for path in plugin_files(plugins_dir):
        name_here = shown(path, plugins_dir)
        imports = [
            (name, line)
            for name, line in imported_modules(path, package_dir)
            if name not in SIBLING_IMPORTS.get(name_here, set())
        ]
        if found := outside_the_sdk(imports, ()):
            offending[name_here] = found
    return offending


def layers_named(files: list[Path], base: Path, label: str, layers: tuple[str, ...]) -> list[str]:
    return [
        f"{label}/{shown(path, base)}: names {layer!r}"
        for path in files
        for layer in layers if layer in path.read_text(encoding="utf-8")
    ]


def toolkit_violations(sdk_dir: Path, package_dir: Path) -> list[str]:
    found = []
    for path in toolkit_files(sdk_dir):
        names = {name for name, _line in imported_modules(path, package_dir) if name.startswith("runcomposer")}
        if not names <= {"runcomposer.sdk"}:
            found.append(f"sdk/{shown(path, sdk_dir)} imports {sorted(names - {'runcomposer.sdk'})}")
    found += layers_named(
        toolkit_files(sdk_dir), sdk_dir, "sdk",
        ("runcomposer.core", "runcomposer.app", "runcomposer.http", "runcomposer.plugins"),
    )
    return found


def plugin_imports_from_layers_above(package_dir: Path) -> list[str]:
    found = []
    for layer in ("app", "http", "cli", "core"):
        for path in python_files(package_dir / layer):
            for name, line in imported_modules(path, package_dir):
                if name.startswith(("runcomposer.plugins", "runcomposer.testing")):
                    found.append(f"{layer}/{shown(path, package_dir / layer)}:{line} imports {name!r}")
    return found


# -- rule 1: the core is the bottom ------------------------------------------------


def test_core_has_files():
    names = {path.name for path in core_files()}
    assert {
        "errors.py", "events.py", "filter.py", "hashing.py", "ids.py", "lifecycle.py",
        "marker.py", "model.py", "parameters.py", "planning.py", "ports.py", "registry.py",
        "selection.py", "spec.py", "taxonomy.py", "timeutil.py", "xmlsafe.py",
    } <= names


def test_no_forbidden_vocabulary_in_core():
    found = forbidden_vocabulary(CORE_DIR)
    assert not found, "framework or upper-layer vocabulary in the core:\n" + "\n".join(found)


def test_core_imports_only_stdlib_allowed_deps_and_itself():
    found = core_import_violations(CORE_DIR, PACKAGE_DIR)
    assert not found, (
        f"core may only import stdlib, {sorted(ALLOWED_THIRD_PARTY)}, or runcomposer.core:\n" + "\n".join(found)
    )


def test_core_never_splits_or_normalizes_item_ids():
    """The core compares ids for equality only (DESIGN.md §2). Any .split(,
    partition, or lower/upper call on an id would be normalization creeping in.
    Tag matching (filter.py) is the one place case folding is a documented
    part of the grammar."""
    found = id_normalization(CORE_DIR)
    assert not found, "item ids split or case-folded in the core:\n" + "\n".join(found)


def test_core_protocols_are_not_runtime_checkable():
    """A ``runtime_checkable`` protocol invites ``isinstance`` checks that
    only look at member *names* — a conformance claim nothing verifies.
    Conformance is a matter of behaviour, checked by tests."""
    found = runtime_checkable_protocols(CORE_DIR)
    assert not found, "runtime_checkable protocols in:\n" + "\n".join(found)


def test_the_layers_above_do_not_import_plugins():
    """Plugins are loaded by name (entry points, ``module:``), never imported
    by the application layer or a transport — otherwise a "reference" plugin
    is quietly a dependency of the tool."""
    found = plugin_imports_from_layers_above(PACKAGE_DIR)
    assert not found, "plugins are loaded by name, and the conformance kit is for tests:\n" + "\n".join(found)


# -- rule 2: a connector sees the SDK and nothing else ------------------------------


def test_there_are_plugins_to_check():
    names = {path.name for path in plugin_files()}
    assert {
        "sqlite_store.py", "manifest_source.py", "robot_source.py",
        "demo_runner.py", "robot_pool.py", "jenkins.py",
        "command_runner.py", "observers.py", "verdicts_parser.py", "junit_parser.py",
        "robot_output_parser.py",
    } <= names


#: Sibling imports among the reference plugins that are allowed: none. What
#: two plugins share — and what the worker-side half of a runner needs —
#: lives in the SDK's toolkits, where a connector can use it too. Keys are
#: paths relative to ``plugins/``.
SIBLING_IMPORTS: dict[str, set[str]] = {}


def test_plugins_import_only_the_sdk_from_runcomposer():
    """Everything of runcomposer a plugin uses comes through
    ``runcomposer.sdk`` or one of its toolkits — including imports made
    inside a function, which is where a worker process would hide one — and
    plugins do not import each other."""
    offending = plugin_imports_beyond_the_sdk(PLUGINS_DIR, PACKAGE_DIR)
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
    layers = ("runcomposer.core", "runcomposer.app", "runcomposer.http", "runcomposer.cli")
    found = layers_named(plugin_files(), PLUGINS_DIR, "plugins", layers)
    assert not found, "\n".join(found)


def test_the_import_scanner_sees_function_level_and_relative_imports():
    """The guard is only as good as its scanner: pin that it finds an import
    hidden in a function and resolves relative ones."""
    source = (
        "from runcomposer.sdk import Verdict\n"
        "from runcomposer.sdk.robot import supervise\n"
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
        "runcomposer.sdk.robot",
        "runcomposer.plugins",
        "runcomposer.plugins.robot_listener",
        "runcomposer",
        "runcomposer.core.model",
        "runcomposer.app.service",
        "json",
        "runcomposer.core",
    ]
    assert outside_the_sdk(found, ("runcomposer.plugins",)) == [
        "line 5: runcomposer",
        "line 7: runcomposer.core.model",
        "line 8: runcomposer.app.service",
        "line 10: runcomposer.core",
    ]


def test_the_demo_imports_only_the_sdk():
    """`runcomposer demo` drives the tool the way an embedder would — so it
    is written against the embedding surface, and proves that one too. Every
    module of the demo package is held to it."""
    demo_modules = python_files(DEMO_DIR)
    assert DEMO_DIR / "demo.py" in demo_modules
    for path in demo_modules:
        imports = imported_modules(path)
        assert outside_the_sdk(imports, ()) == [], shown(path, DEMO_DIR)
        assert {name for name, _ in imports if name.startswith("runcomposer")} <= {"runcomposer.sdk"}
    assert {
        name for name, _ in imported_modules(DEMO_DIR / "demo.py") if name.startswith("runcomposer")
    } == {"runcomposer.sdk"}


def test_the_sdk_is_built_from_the_core_at_import_time():
    """At module level ``runcomposer.sdk`` pulls in the core only; the
    embedding names are resolved on first use (tests/test_sdk.py shows the
    behaviour — this pins the structure)."""
    tree = ast.parse((SDK_DIR / "__init__.py").read_text(encoding="utf-8"))
    checked = 0
    for node in tree.body:  # module-level statements only
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("runcomposer"):
            checked += 1
            assert (node.module or "").startswith("runcomposer.core."), (
                f"sdk.py:{node.lineno} imports {node.module!r} at module level"
            )
    assert checked > 10


# -- rule 3: the toolkits are built from the SDK alone --------------------------------


def test_the_sdk_has_exactly_its_two_toolkits():
    assert [shown(path, SDK_DIR) for path in toolkit_files()] == ["jenkins.py", "robot.py"]


def test_the_toolkits_import_only_the_sdk_from_runcomposer():
    """A toolkit is what a reference runner is made of, offered to every
    connector — so it is held to the connector's rule: of runcomposer it
    imports ``runcomposer.sdk`` and nothing below or beside it."""
    found = toolkit_violations(SDK_DIR, PACKAGE_DIR)
    assert not found, "\n".join(found)


def test_the_robot_toolkit_imports_robot_framework_only_inside_functions():
    """Module level stays free of the framework: importing the toolkit — and
    the runner built on it — works where Robot Framework is not installed."""
    tree = ast.parse((SDK_DIR / "robot.py").read_text(encoding="utf-8"))
    at_module_level = [
        node for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and any(
            name == "robot" or name.startswith("robot.")
            for name in (
                [alias.name for alias in node.names] if isinstance(node, ast.Import)
                else [node.module or ""]
            )
        )
    ]
    assert at_module_level == []


def test_the_reference_runners_are_built_on_the_toolkits():
    """The proof that a toolkit suffices: the runner it came out of uses it
    and carries none of the machinery itself."""
    pool = {name for name, _line in imported_modules(PLUGINS_DIR / "robot_pool.py")}
    jenkins = {name for name, _line in imported_modules(PLUGINS_DIR / "jenkins.py")}
    assert "runcomposer.sdk.robot" in pool and "runcomposer.sdk.jenkins" in jenkins
    # No process handling and no Robot import of its own in the pool runner …
    assert not {"multiprocessing", "robot", "robot.api"} & pool
    # … and no HTTP of its own in the Jenkins runner.
    assert not {"urllib.request", "http.cookiejar", "ssl", "base64"} & jenkins
    # The callback URLs come from the SDK's helper, not from a format string.
    assert "/api/v1" not in (PLUGINS_DIR / "jenkins.py").read_text(encoding="utf-8")
    # The command runner executes its shards under the SDK's supervisor, and
    # keeps no process supervision of its own: no worker processes, no
    # signals, no sessions or process groups.
    command_file = PLUGINS_DIR / "command_runner.py"
    command = {name for name, _line in imported_modules(command_file)}
    assert not {"multiprocessing", "signal", "concurrent.futures", "threading"} & command
    tree = ast.parse(command_file.read_text(encoding="utf-8"))
    imported_from_sdk = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "runcomposer.sdk"
        for alias in node.names
    }
    called = {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "supervise_workers" in imported_from_sdk and "supervise_workers" in called
    assert not {"killpg", "setsid", "setpgrp", "fork"} & called
    assert "start_new_session" not in command_file.read_text(encoding="utf-8")


# -- self-tests: a violation in a nested sub-package is seen ----------------------------


def planted_package(tmp_path: Path) -> Path:
    """A ``runcomposer`` package with one violation of every rule, each in a
    module two levels below the directory its scanner is pointed at."""
    package = tmp_path / "runcomposer"
    framework = "jen" + "kins"  # assembled: this file stays free of the planted word's context
    files = {
        "core/__init__.py": "",
        "core/model.py": "import json\n",
        "core/deep/er/leak.py": (
            "from __future__ import annotations\n"
            "import runcomposer.app.service\n"
            f"WORD = '{framework}'\n"
            "def f(item_id):\n"
            "    return item_id.split('.')\n"
            "from typing import runtime_checkable\n"
        ),
        "plugins/__init__.py": "",
        "plugins/ok.py": "from runcomposer.sdk import Verdict\n",
        "plugins/vendor/inner/sneaky.py": (
            "from runcomposer.sdk import Verdict\n"
            "def worker():\n"
            "    from runcomposer.core.model import Item\n"
            "TARGET = 'runcomposer.app'\n"
        ),
        "sdk/__init__.py": "",
        "sdk/robot.py": "from runcomposer.sdk import Verdict\n",
        "sdk/kits/more/tool.py": "from ....app import service\n",
        "app/__init__.py": "",
        "app/sub/mod/uses.py": "from runcomposer.plugins import demo_runner\n",
    }
    for name, text in files.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return package


def test_the_scanners_see_a_violation_in_a_nested_sub_package(tmp_path):
    package = planted_package(tmp_path)
    core, plugins, sdk = package / "core", package / "plugins", package / "sdk"
    assert "core/deep/er/leak.py:3: 'jenkins'" in forbidden_vocabulary(core)
    assert core_import_violations(core, package) == ["core/deep/er/leak.py:2: imports 'runcomposer.app.service'"]
    assert id_normalization(core) == ["core/deep/er/leak.py: 'item_id.split'"]
    assert runtime_checkable_protocols(core) == ["core/deep/er/leak.py"]
    assert plugin_imports_beyond_the_sdk(plugins, package) == {
        "vendor/inner/sneaky.py": ["line 3: runcomposer.core.model"]
    }
    assert layers_named(plugin_files(plugins), plugins, "plugins", ("runcomposer.app",)) == [
        "plugins/vendor/inner/sneaky.py: names 'runcomposer.app'"
    ]
    assert [shown(path, sdk) for path in toolkit_files(sdk)] == ["kits/more/tool.py", "robot.py"]
    assert toolkit_violations(sdk, package) == ["sdk/kits/more/tool.py imports ['runcomposer.app']"]
    assert plugin_imports_from_layers_above(package) == [
        "app/sub/mod/uses.py:1 imports 'runcomposer.plugins'"
    ]


def test_the_scanners_pass_a_clean_nested_tree(tmp_path):
    package = tmp_path / "runcomposer"
    for name, text in {
        "core/nested/clean.py": "from runcomposer.core.model import Item\nimport json\n",
        "core/model.py": "",
        "plugins/nested/clean.py": "from runcomposer.sdk import Verdict\nfrom runcomposer.sdk.robot import supervise\n",
        "sdk/robot.py": "from runcomposer.sdk import Verdict\n",
        "app/nested/clean.py": "from runcomposer.core.model import Item\n",
    }.items():
        (package / name).parent.mkdir(parents=True, exist_ok=True)
        (package / name).write_text(text, encoding="utf-8")
    assert forbidden_vocabulary(package / "core") == []
    assert core_import_violations(package / "core", package) == []
    assert plugin_imports_beyond_the_sdk(package / "plugins", package) == {}
    assert toolkit_violations(package / "sdk", package) == []
    assert plugin_imports_from_layers_above(package) == []
