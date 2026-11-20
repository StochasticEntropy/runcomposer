"""The example world is the only world (DESIGN.md §12).

`test_self_containment.py` lists tokens the *core* must not contain, which
only works for vocabulary this project already knows about. It cannot catch
vocabulary from outside the example world — a deny-list would have to name
it. So these guards are built from *structure*: wherever the repository
spells something tag-shaped, the tag must exist in the bundled demo corpus.
Anything else fails without ever being named here.

Two shapes are checked:

1. **Tag literals in the UI's test files** — unit tests and browser
   end-to-end tests alike (quoted strings, comments included): every bare
   word, `prefix:` and `regex:` pattern must match the demo corpus.
2. **Filter expressions anywhere in the tracked tree** — prose, comments,
   docs: the operands of an uppercase `AND` / `OR` / `NOT` are tags by
   construction, so each capitalised operand must be a demo tag (or a
   one-letter metavariable such as `A`).
"""

from __future__ import annotations

import json
import re
import subprocess
from importlib import resources
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Suffixes scanned as text. Media and the minified UI bundle are not prose.
TEXT_SUFFIXES = {
    ".py", ".md", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".json", ".yaml", ".yml", ".toml",
    ".html", ".css", ".robot", ".resource", ".sh", ".txt", ".cfg", ".ini", "",
}
SKIPPED_DIRS = ("src/runcomposer/ui_dist/", "ui/node_modules/")
SKIPPED_FILES = {"ui/package-lock.json"}


def demo_tags() -> set[str]:
    corpus = json.loads(
        (resources.files("runcomposer.demo") / "corpus.json").read_text(encoding="utf-8")
    )
    return {tag for item in corpus["items"] for tag in item["tags"]}


def demo_vocabulary() -> set[str]:
    """Every word the example world is made of: tag components, taxonomy
    labels, item names and hierarchy segments of the bundled corpus —
    case-folded. A tag invented for a test must be assembled from these."""
    corpus = json.loads(
        (resources.files("runcomposer.demo") / "corpus.json").read_text(encoding="utf-8")
    )
    taxonomy = (resources.files("runcomposer.demo") / "taxonomy.yaml").read_text(encoding="utf-8")
    words: set[str] = set()
    for item in corpus["items"]:
        for text in (*item["tags"], item.get("name", ""), *item.get("hierarchy", ())):
            words.update(re.findall(r"[A-Za-z]+", text))
    words.update(re.findall(r"label:\s*([A-Za-z]+)", taxonomy))
    return {word.casefold() for word in words}


def tracked_text_files() -> list[Path]:
    """Every tracked text file; falls back to walking the tree outside a git
    checkout (an sdist), where "tracked" has no meaning."""
    try:
        listed = subprocess.run(
            ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        listed = [
            path.relative_to(REPO).as_posix()
            for path in REPO.rglob("*")
            if path.is_file() and not any(part.startswith(".") for part in path.parts)
        ]
    files = []
    for name in listed:
        if name in SKIPPED_FILES or name.startswith(SKIPPED_DIRS):
            continue
        path = REPO / name
        if path.suffix.lower() in TEXT_SUFFIXES and path.is_file():
            files.append(path)
    return files


# -- 1. tag literals in the UI's test files ------------------------------------


def quoted_strings(text: str) -> list[str]:
    found = re.findall(r'"([^"\n]{1,80})"|\'([^\'\n]{1,80})\'|`([^`\n]{1,80})`', text)
    return [next(part for part in parts if part) for parts in found if any(parts)]


def pattern_is_of_the_demo_world(value: str, tags: set[str]) -> bool | None:
    """True/False for a tag-shaped literal, None when the literal is not
    tag-shaped at all (prose, identifiers, paths)."""
    folded = {tag.casefold() for tag in tags}
    if value.startswith("prefix:"):
        body = value[len("prefix:"):]
        return bool(body) and any(tag.startswith(body) for tag in tags)
    if value.startswith("regex:"):
        try:
            compiled = re.compile(value[len("regex:"):])
        except re.error:
            return None  # a deliberately broken expression is a grammar test
        return any(compiled.search(tag) for tag in tags)
    if not re.fullmatch(r"[A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*", value):
        return None  # not shaped like a tag of this world or any other
    if len(value) == 1 or value.casefold() in folded:
        return True  # a one-letter metavariable, or a tag of the corpus
    # An invented tag is still of this world when every hyphen-separated part
    # is (a stem of) a word the bundled corpus uses — `Checkout-Cart` is, a
    # word out of somebody's real suite is not.
    vocabulary = demo_vocabulary()

    def of_this_world(part: str) -> bool:
        part = part.casefold()
        if part.isdigit() or part in vocabulary:
            return True
        return len(part) >= 3 and any(word.startswith(part) for word in vocabulary)

    return all(of_this_world(part) for part in value.split("-"))


def test_pattern_classifier_checks_bare_prefix_and_regex_literals():
    """Bare tags, `prefix:` and `regex:` literals are all classified — a
    pattern is example data as much as a tag is."""
    tags = demo_tags()
    assert pattern_is_of_the_demo_world("Payments", tags) is True
    assert pattern_is_of_the_demo_world("prefix:Checkout-", tags) is True
    assert pattern_is_of_the_demo_world("regex:^Cart(V2)?$", tags) is True
    assert pattern_is_of_the_demo_world("Checkout-Cart", tags) is True  # invented from its words
    assert pattern_is_of_the_demo_world("Warehouse", tags) is False
    assert pattern_is_of_the_demo_world("Checkout-Warehouse", tags) is False
    assert pattern_is_of_the_demo_world("prefix:Warehouse-", tags) is False
    assert pattern_is_of_the_demo_world("regex:^Warehouse", tags) is False
    assert pattern_is_of_the_demo_world("some prose here", tags) is None


#: Names of keys a browser test presses. Not tags, and nobody's test data.
_KEYBOARD_KEYS = {"Tab"}


def ui_test_files() -> list[Path]:
    """The UI's unit tests (anywhere under ``ui/src``) and its browser
    end-to-end tests (``ui/e2e``)."""
    ui = REPO / "ui"
    unit = [path for pattern in ("*.test.ts", "*.test.tsx") for path in (ui / "src").rglob(pattern)]
    return sorted([*unit, *(ui / "e2e").glob("*.ts")])


def ui_own_vocabulary() -> set[str]:
    """What the UI itself spells, and may therefore be spelled back by its
    tests: operator ids, field ids, node kinds — whatever its non-test source
    has in quotes — and every text of its locale files (a test names a button
    by its label). Derived, so this guard needs no hand-kept list."""
    ui_src = REPO / "ui" / "src"
    own: set[str] = {"AND", "OR", "NOT"}
    for pattern in ("*.ts", "*.tsx"):
        for source in ui_src.rglob(pattern):
            if ".test." not in source.name:
                own.update(quoted_strings(source.read_text(encoding="utf-8")))

    def texts(node: object) -> None:
        if isinstance(node, dict):
            for child in node.values():
                texts(child)
        elif isinstance(node, str):
            own.add(node)

    for locale in (ui_src / "locales").glob("*.json"):
        texts(json.loads(locale.read_text(encoding="utf-8")))
        # The keys too: a state or status the server sends (`INCOMPLETE`) is a key.
        own.update(re.findall(r'"([A-Za-z_-]+)":', locale.read_text(encoding="utf-8")))
    # A runner declares the labels of its per-run parameters and the UI shows
    # them verbatim, so a test of the form names them: what the bundled
    # plugins spell is the product's vocabulary as well.
    for plugin in (REPO / "src" / "runcomposer" / "plugins").glob("*.py"):
        own.update(quoted_strings(plugin.read_text(encoding="utf-8")))
    return own | _KEYBOARD_KEYS


def test_the_ui_has_tests_to_check():
    """The guard below is vacuous if a change of layout hides the files from
    it — so the files it reads are counted."""
    names = {path.name for path in ui_test_files()}
    if not (REPO / "ui" / "src").is_dir():  # the UI is optional in a source checkout
        return
    assert {"filter.test.ts", "editor.test.ts", "compose.spec.ts", "run.spec.ts"} <= names


def test_ui_test_data_uses_only_the_demo_corpus_vocabulary():
    """The UI's tests pin filter behaviour, so they carry tag names. Those
    names must come from the shipped example world — a real corpus's tags in
    this repository are a self-containment failure."""
    test_files = ui_test_files()
    if not test_files:  # the UI is optional in a source checkout
        return
    tags = demo_tags()
    own = ui_own_vocabulary()
    suspects: dict[str, set[str]] = {}
    for test_file in test_files:
        for value in quoted_strings(test_file.read_text(encoding="utf-8")):
            value = value.strip()
            if value in own:
                continue
            if pattern_is_of_the_demo_world(value, tags) is False:
                suspects.setdefault(test_file.name, set()).add(value)
    assert not suspects, (
        f"UI test data uses tag names that are not in the bundled demo corpus: "
        f"{ {name: sorted(values) for name, values in suspects.items()} } — example data must "
        "use the shipped example world (DESIGN.md §12), never a real corpus"
    )


# -- 2. filter expressions anywhere, comments included --------------------------

_OPERAND = r"(?<![\w.-])[A-Z][A-Za-z0-9_]*(?:-[A-Za-z0-9]+)*(?![\w.-])"
_EXPRESSION = re.compile(
    rf"(?:({_OPERAND})\)?\s+(?:AND|OR)\s+(?:NOT\s+)?\(?({_OPERAND}))"
    rf"|(?:\bNOT\s+\(?({_OPERAND}))"
)
_OPERATORS = {"AND", "OR", "NOT"}
#: Uppercase words that follow NOT in query-language and status spellings
#: (`IS NOT NULL`, `IF NOT EXISTS`, `NOT RUN`). Language keywords, not tags.
_KEYWORDS = {"NULL", "EXISTS", "RUN", "IN", "LIKE"}
#: Verbatim legal text is written in capitals and is nobody's example data.
_NOT_EXAMPLE_DATA = {"LICENSE"}


def expression_operands(text: str) -> set[str]:
    operands: set[str] = set()
    position = 0
    while True:
        match = _EXPRESSION.search(text, position)
        if match is None:
            return operands
        operands.update(group for group in match.groups() if group)
        # Overlapping scan: `A AND B OR C` must yield B as a left operand too.
        position = match.start() + 1


def test_expression_scanner_reads_operands_out_of_prose_and_comments():
    assert expression_operands("// `Payments AND (Cart OR Auth)` picks two") >= {
        "Payments", "Cart", "Auth"
    }
    assert expression_operands("NOT (A OR B) == (NOT A) AND (NOT B)") >= {"A", "B"}
    assert expression_operands("this and that, or nothing") == set()
    assert expression_operands("An OR of negations is prose") == set()
    assert expression_operands("`Auth AND (Checkout-Express OR Checkout-Gift)`") == {
        "Auth", "Checkout-Express", "Checkout-Gift"
    }


def test_filter_expressions_everywhere_speak_the_example_world():
    """A filter expression in a comment or a document is example data like any
    other. Its capitalised operands are tags, so they must be tags of the
    bundled corpus — which is how a real suite's vocabulary in a code comment
    fails here without this file ever listing it."""
    tags = demo_tags()
    suspects: dict[str, set[str]] = {}
    for path in tracked_text_files():
        name = path.relative_to(REPO).as_posix()
        if name in _NOT_EXAMPLE_DATA:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for operand in expression_operands(text):
            if operand in _OPERATORS or operand in _KEYWORDS:
                continue
            if pattern_is_of_the_demo_world(operand, tags) is False:
                suspects.setdefault(name, set()).add(operand)
    assert not suspects, (
        "filter expressions outside the example world (DESIGN.md §12) — every operand of "
        f"AND/OR/NOT must be a tag of the bundled demo corpus: "
        f"{ {name: sorted(values) for name, values in suspects.items()} }"
    )


def test_a_foreign_expression_in_a_comment_would_be_caught():
    """A filter expression outside the example world, sitting in a code
    comment, is caught. Demonstrated with an invented one."""
    tags = demo_tags()
    # Assembled, so that this file does not itself contain the expression it
    # shows the guard catching.
    both, either = "AND", "OR"
    comment = f"// `Warehouse {both} (Forklift {either} Pallet)` is picking two tags"
    foreign = {
        operand
        for operand in expression_operands(comment)
        if pattern_is_of_the_demo_world(operand, tags) is False
    }
    assert foreign == {"Warehouse", "Forklift", "Pallet"}
