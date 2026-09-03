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

# Framework/CI/persistence vocabulary that must never appear in core code
# (case-insensitive). Storage engines and web frameworks live in plugins and
# the app layer; the core knows only DESIGN.md §2 vocabulary.
FORBIDDEN_TOKENS = [
    "runcomposer.plugins",
    "runcomposer.demo",
    "runcomposer.service",
    "runcomposer.config",
    "runcomposer.api",
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


# -- the example world is the only world (DESIGN.md §12) ----------------------
#
# The guard above lists tokens to forbid, which only works for vocabulary this
# project already knows about. It cannot catch an adopter's domain leaking in
# from a real corpus — naming those terms here would be the leak. So this half
# inverts the test: example data may only use tags the bundled demo corpus
# defines. A tag from somebody's real suite fails without ever being named.

import json


def demo_tags():
    from importlib import resources

    corpus = json.loads(
        (resources.files("runcomposer.demo") / "corpus.json").read_text(encoding="utf-8")
    )
    return {tag for item in corpus["items"] for tag in item["tags"]}


def quoted_strings(text):
    import re

    return re.findall(r'"([^"\n]{1,60})"|\'([^\'\n]{1,60})\'', text)


def test_ui_test_data_uses_only_the_demo_corpus_vocabulary():
    """The UI's adapter tests pin filter behaviour, so they carry tag names.
    Those names must come from the shipped example world — a real corpus's
    tags in this repository are a self-containment failure (CLAUDE.md)."""
    ui_test = Path(__file__).parent.parent / "ui" / "src" / "filterAdapter.test.js"
    if not ui_test.is_file():  # the UI is optional in a source checkout
        return
    # The adapter's own vocabulary — operator ids, glues, field ids — is
    # whatever it spells in quotes. Deriving it from the module keeps this
    # guard from needing a hand-maintained list that goes stale.
    adapter = (ui_test.parent / "filterAdapter.js").read_text(encoding="utf-8")
    own = {(d or s_).strip() for d, s_ in quoted_strings(adapter)}
    allowed = demo_tags() | own | {"AND", "OR"}
    text = ui_test.read_text(encoding="utf-8")
    suspects = []
    for double, single in quoted_strings(text):
        value = (double or single).strip()
        # Only tag-shaped literals are candidates: a bare word or one of the
        # grammar's prefixed patterns. Prose, identifiers and paths are not.
        bare = value.split(":", 1)[1] if value.startswith(("prefix:", "regex:")) else value
        if not bare or " " in bare or "/" in bare or "." in bare or ":" in value:
            continue
        if bare in allowed or bare.lower() in {t.lower() for t in allowed}:
            continue
        if bare.islower() and bare.isalpha():  # option keys like "and"/"text"
            continue
        suspects.append(value)
    assert not suspects, (
        f"{ui_test.name} uses tag names that are not in the bundled demo corpus: "
        f"{sorted(set(suspects))} — example data must use the shipped example world "
        "(DESIGN.md §12), never a real corpus"
    )
