"""The rules of the example-world guard, shared by ``tests/test_example_world.py``
and ``ci/check_commit_messages.py`` (DESIGN.md §12).

Every rule is an *allow* rule: it describes what the example world and this
project's own infrastructure look like, and anything else is reported. No
list of outside names exists here and none may be added — something from
outside fails because it is not of this world, not because it was foreseen.

The rules, and the structure each one relies on:

``host``
    Anything host-shaped, with or without a scheme: lower-case dot-separated
    labels whose last label is a top-level domain — every two-letter label
    (the country-code shape, assigned or not), the classic generic ones,
    common newer ones, and the names private networks use instead of a real
    domain (``local``, ``internal``, ``corp``, ``lan`` …), see ``HOST_TLDS``
    — plus every host behind a scheme or an ``@`` and every IPv4 address.
    It must be under the example domain (``*.shop.example``), a reserved
    documentation name (``example.com``/``.org``/``.net``, ``*.example``,
    ``*.test``, ``*.invalid``, ``*.localhost``, ``localhost``), a loopback,
    unspecified or documentation address (127/8, 0.0.0.0, 192.0.2/24,
    198.51.100/24, 203.0.113/24), or one of ``PROJECT_HOSTS`` (each entry
    says why). A GitHub URL must point at this project's own account. A
    single-label host behind a scheme (``http://runner:8100``) must be a
    word of the vocabulary or the code. These are *not* hosts, by structure:

    * a name whose last label is a file suffix (``config.yaml``,
      ``output.xml``, ``conftest.py``, ``teaser.vtt``) — ``FILE_SUFFIXES``;
    * a dotted name whose first label is a Python module this repository
      imports or ships, or a build backend its ``pyproject.toml`` names
      (``runcomposer.sdk.robot``, ``os.path``, ``json.dumps``) — derived
      from the code, never listed;
    * a dotted message id of the UI's locale files (``error.code.network``);
    * a dotted reference ending in a label that is also an everyday
      identifier (``WEAK_TLDS``: ``run.id``, ``ctx.live``,
      ``preview.group``) when every label is an identifier of the code or a
      word of the vocabulary;
    * anything with an upper-case letter in a label (``Shop.Checkout.Cards``
      item ids, ``Service.dispatch``) — host names are written lower-case;
    * version numbers (``1.2.3``) — no alphabetic top-level label;
    * template expressions (``${{ matrix.os }}``, ``{run.id}``);
    * in Python and C-family source, the code itself: only string literals
      and comments are read, so ``self.store`` in code is never a host.

    IPv6 literals are not read.

``path``
    A home directory or machine-specific absolute path: ``/Users/<name>``,
    ``/home/<name>``, ``/root/…``, a mounted volume (``/Volumes``, ``/mnt``,
    ``/media``, ``/net``), and an absolute path under any other filesystem
    root (``FILESYSTEM_ROOTS``: ``/tmp``, ``/var``, ``/opt``, ``/etc``,
    ``/srv``, ``/data``, ``/app``, ``/private`` …) unless it is under the
    reviewed ``GENERIC_PATHS``; a Windows drive path deeper than one entry
    or under ``Users`` (``C:\\Users\\<name>``); a UNC path whose server is not
    a word of the vocabulary or the code. A placeholder segment (``<name>``,
    ``{user}``, ``$HOME``, ``${VAR}``, ``…``, ``...``) makes a path
    generic. URL paths and API routes (``/api/runs``) start at no
    filesystem root and are not read; a bare root (``/tmp``) names nothing.

``ticket-key``
    Every token shaped like ``SHOP-1200`` (capitals, a hyphen, digits) has
    the example world's prefix (``SHOP``) or a standard's (``ISO-8601``,
    ``UTF-8``, ``RFC-…``).

``tag``
    Every tag literal, in every shape test data spells one in — values under
    a tag-named key of YAML/JSON (``tags``, ``tag_filter``, ``force_tags``
    …, and ``filter`` when its value is shaped like a filter; structured
    filters included), Python values given under a tag-named key, keyword
    or variable (lists, tuples, sets, ``frozenset(...)``, filter dicts),
    Robot ``[Tags]``/``Force Tags``/``Test Tags``/``Default Tags``/``Keyword
    Tags`` (continuation lines too, in suites and in suites embedded in
    strings), the operands of an ``AND``/``OR``/``NOT`` expression,
    ``prefix:``/``regex:`` patterns anywhere, the filter argument of a
    ``runcomposer compile|spec|run|plan`` command line and ``--tag`` values,
    a ``tags`` column of a CSV file, every capitalised quoted literal in
    the UI's test files that the product does not spell itself, and every
    string constant of a Python test shaped like this world's tags
    (``Checkout``, ``Payments-Cards``) that the package does not spell
    itself — in a parametrize list, a plain list, a dict key, a call
    argument — is of the
    example world's tag vocabulary: the demo corpus, the tags of the shipped
    examples, the reviewed invented tags of the words file, and the derived
    forms (``DERIVED_TAGS``: ``RanOn-{partition}``, ``Sprint-<n>``,
    ``SHOP-1<nnn>``; a vocabulary tag with ``-<n>`` appended). Comparison is
    case-insensitive, as the filter grammar's is. A tag written in lower
    case (``smoke``, an API router's ``runs``) is made of words of the
    vocabulary or the code; a pattern that selects nothing on purpose is
    made of pieces of the vocabulary (``prefix:heckout``). See
    ``tag_is_allowed`` for placeholders (``<tag>``, ``X``, ``[FILTER]``).

``prose-token``
    Every capitalised word in prose — Markdown outside code, comments,
    docstrings, WebVTT cues, HTML text and human-facing attributes, UI
    locale strings, the names of UI tests (``it``/``test``/``describe``)
    and of Robot tests and keywords (a Python test's snake_case name has no
    capital to check) — that is not the first word of a
    sentence, and every CamelCase, ALL-CAPS or hyphenated-capitalised word
    wherever it stands, is a word of the reviewed vocabulary
    (``tests/example_world_words.txt``), an identifier of the code
    (``CODE_DIRS``: ``src/``, ``ui/src/``, ``ui/e2e/``, ``tests/``,
    ``examples/``, ``ci/``), or a word of the example world (the demo
    corpus). Inline code and URLs are not prose. Matching is
    case-insensitive. There is no system dictionary: the committed words
    file *is* the vocabulary, and a new word fails until somebody adds it
    there — that is the intended friction.

``process-word`` (commit messages only)
    How a change came about is not part of the project's history: the words
    of ``PROCESS_WORDS`` do not appear in a commit message (the comment
    there says, per word, which uses are flagged and which are the
    project's own — the run agent of ``examples/remote-agent``).

``lowercase-token``
    Every lower-case word of prose, every hyphenated lower-case identifier
    wherever a host could be written (a job, cluster or queue name in a YAML
    value, a string literal, a URL's path), every word of a JSON text value
    and of an SQL statement, and the words of every class name, is a word of
    the reviewed vocabulary, the reviewed lower-case list
    (``tests/example_world_lowercase.txt``), the example world or the code
    (a class name's words: the vocabulary and the list only). Foreign
    names written in lower case are names all the same.

``finding-id`` (the tree's prose and commit messages)
    The same rule for the labels a list of work items gives its entries: no
    sentence, docstring or comment begins with one (``H4: the refusal …``,
    ``(M2) …``) — see ``FINDING_ID``. The sentence stays; the label goes. A
    hyphenated label (capitals, a digit, a hyphen, digits) is a
    ``ticket-key`` wherever it stands.

Built bundles and lock files (``src/runcomposer/ui_dist/**``,
``package-lock.json`` and other lock files) are generated: only the
``host`` and ``path`` rules read them — minified identifiers and hashes
cannot be weighed as prose or tags, so there only hosts behind a scheme or
an ``@`` count — and their hosts are checked against ``PROJECT_HOSTS``
plus ``DEPENDENCY_HOSTS`` (the hosts package metadata names), with a
GitHub URL of any account allowed there.

Which files: every file of the tree that decodes as UTF-8 and has no
``BINARY_SUFFIXES`` suffix — tracked and not-yet-tracked files of a git
checkout, or, without one, a walk honouring the tree's ``.gitignore``
files (``list_files``).
"""

from __future__ import annotations

import ast
import functools
import io
import json
import re
import subprocess
import sys
import tokenize
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORDS_FILE_NAME = "tests/example_world_words.txt"
LOWERCASE_FILE_NAME = "tests/example_world_lowercase.txt"

# ---------------------------------------------------------------------------
# Violations
# ---------------------------------------------------------------------------

#: What to do about a violation, per rule — part of every failure message.
ADVICE = {
    "host": "use a host under shop.example (or another reserved name: *.example, *.test, "
    "*.invalid); a host of this project's own infrastructure goes on PROJECT_HOSTS in "
    "tests/example_world_rules.py with a comment saying why",
    "path": "write the path relative, under a placeholder (/home/<user>/…, $HOME/…), or use a "
    "generic one; a new generic path goes on GENERIC_PATHS in tests/example_world_rules.py",
    "ticket-key": "use a ticket of the example world (SHOP-1200 … SHOP-1299)",
    "tag": "use a tag of the example world (the demo corpus, DESIGN.md §12); a tag invented "
    f"for a test goes under 'Invented tags' in {WORDS_FILE_NAME} — additions need review",
    "prose-token": f"use a word of the example world, or add the word to {WORDS_FILE_NAME} "
    "under the heading it belongs to — additions need review: no names of people, "
    "companies, products or systems from outside this project",
    "process-word": "describe the change in the project's own terms, not how it came about",
    "finding-id": "drop the label of the work item and keep the sentence — the project's text "
    "does not refer to an outside list of items",
    "lowercase-token": f"use a word of the example world, or add the word to {LOWERCASE_FILE_NAME} "
    "— additions need review: an English word or a technical term, never a name from outside "
    "this project (a lower-case job, host, team or system name included)",
}


@dataclass(frozen=True, order=True)
class Violation:
    path: str
    line: int
    rule: str
    token: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.token!r}"


def report(violations: Iterable[Violation]) -> str:
    """A failure message: every violation as ``file:line: [rule] 'token'``,
    followed by what to do, per rule."""
    violations = sorted(set(violations))
    rules = sorted({violation.rule for violation in violations})
    lines = [str(violation) for violation in violations]
    lines += ["", *(f"[{rule}] {ADVICE[rule]}" for rule in rules)]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The files of a tree
# ---------------------------------------------------------------------------

#: Suffixes of files that are not text. Everything else is read when it
#: decodes as UTF-8.
BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tif", ".tiff",
    ".mp4", ".webm", ".mov", ".mp3", ".wav", ".ogg",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".tar", ".whl", ".jar",
    ".pyc", ".so", ".dylib", ".dll", ".exe", ".db", ".sqlite", ".sqlite3", ".pdf",
})
#: Directories a walk outside a git checkout skips: caches, virtualenvs,
#: installed packages and build output — nothing a checkout tracks.
WALK_SKIPPED_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".nox", "dist", "build", "test-results", "playwright-report",
})
#: Generated files: read for hosts and paths only (see the module docstring).
BUNDLE_PREFIXES = ("src/runcomposer/ui_dist/",)
LOCK_FILE_NAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock", "Pipfile.lock",
})


def is_generated(name: str) -> bool:
    return name.startswith(BUNDLE_PREFIXES) or name.rsplit("/", 1)[-1] in LOCK_FILE_NAMES


@functools.cache
def is_git_checkout(root: Path) -> bool:
    """Whether ``root`` is the top of a git checkout."""
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return False
    return Path(top).resolve() == root.resolve()


@functools.cache
def list_files(root: Path) -> tuple[str, ...]:
    """Every file of the tree at ``root`` that a checkout would hold, as a
    POSIX path relative to ``root``: tracked and not-yet-tracked (but not
    ignored) files when ``root`` is the top of a git checkout; otherwise —
    an sdist, a container without ``.git``, a temporary tree — a walk that
    skips ``WALK_SKIPPED_DIRS``, ``*.egg-info`` and what the tree's
    ``.gitignore`` files exclude (``ignored_by``)."""
    root = root.resolve()
    if is_git_checkout(root):
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.split("\0")
        return tuple(sorted({name for name in listed if name and (root / name).is_file()}))
    names = []
    ignores = [
        (path.parent.relative_to(root).as_posix(), path.read_text(encoding="utf-8", errors="replace"))
        for path in root.rglob(".gitignore")
        if not any(part in WALK_SKIPPED_DIRS for part in path.relative_to(root).parts[:-1])
    ]
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in WALK_SKIPPED_DIRS or part.endswith(".egg-info") for part in relative.parts[:-1]):
            continue
        if path.is_file() and not ignored_by(ignores, relative.as_posix()):
            names.append(relative.as_posix())
    return tuple(names)


def ignored_by(ignores: list[tuple[str, str]], name: str) -> bool:
    """Whether ``.gitignore`` files — ``(directory, text)`` pairs — exclude
    the file ``name``. The subset of the format this repository uses: one
    pattern per line, ``#`` comments, ``!`` re-includes, a trailing ``/``
    for directories, a pattern with an inner ``/`` anchored at its file's
    directory, ``*`` wildcards; the last matching line wins."""
    import fnmatch

    ignored = False
    for directory, text in ignores:
        prefix = "" if directory == "." else directory + "/"
        if not name.startswith(prefix):
            continue
        relative = name[len(prefix):]
        parts = relative.split("/")
        for raw in text.splitlines():
            pattern = raw.strip()
            if not pattern or pattern.startswith("#"):
                continue
            negated = pattern.startswith("!")
            pattern = pattern.lstrip("!")
            directory_only = pattern.endswith("/")
            pattern = pattern.strip("/")
            if "/" in pattern:  # anchored: the path itself, or a directory above it
                candidates = ["/".join(parts[:end]) for end in range(1, len(parts) + (0 if directory_only else 1))]
            else:  # any directory on the way, or (for a file pattern) the name
                candidates = parts[:-1] if directory_only else parts
            if any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates):
                ignored = not negated
    return ignored


@functools.cache
def read_text(root: Path, name: str) -> str | None:
    """The file's text, or None when it is not text (a binary suffix, or
    bytes that are not UTF-8)."""
    if Path(name).suffix.lower() in BINARY_SUFFIXES:
        return None
    try:
        return (root / name).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def text_files(root: Path) -> list[tuple[str, str]]:
    return [(name, text) for name in list_files(root) if (text := read_text(root, name)) is not None]


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# ---------------------------------------------------------------------------
# Views of a file: which parts are code, which are strings, which are prose
# ---------------------------------------------------------------------------

PY_SUFFIXES = {".py", ".pyi"}
JS_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".groovy", ".java", ".kt", ".go", ".rs"}
HASH_COMMENT_SUFFIXES = {
    ".sh", ".bash", ".zsh", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".conf", ".robot",
    ".resource", ".dockerignore", ".gitignore", ".env", ".properties", ".mk", ".r", ".ps1",
}
HASH_COMMENT_NAMES = {"Dockerfile", "Makefile", "Containerfile", ".gitignore", ".dockerignore", "Procfile"}


def mask(text: str, spans: Iterable[tuple[int, int]]) -> str:
    """``text`` with everything outside ``spans`` blanked — newlines kept,
    so offsets and line numbers stay what they were."""
    out, position = [], 0
    for start, end in sorted(spans):
        if end <= position:
            continue
        start = max(start, position)
        out.append(_BLANK.sub(" ", text[position:start]))
        out.append(text[start:end])
        position = end
    out.append(_BLANK.sub(" ", text[position:]))
    return "".join(out)


_BLANK = re.compile(r"[^\n]")


def _offsets(text: str) -> list[int]:
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


@dataclass
class PythonParts:
    strings: list[tuple[int, int]] = field(default_factory=list)
    comments: list[tuple[int, int]] = field(default_factory=list)
    docstrings: list[tuple[int, int]] = field(default_factory=list)
    names: set[str] = field(default_factory=set)
    imports: set[str] = field(default_factory=set)  # top-level names of absolute imports
    tags: list[tuple[str, int]] = field(default_factory=list)  # see tags_in_python
    classes: list[tuple[str, int]] = field(default_factory=list)  # class names, with their line
    constants: list[tuple[str, int]] = field(default_factory=list)  # every string constant, with its line


@functools.cache
def python_parts(text: str) -> PythonParts | None:
    """String literals, comments, docstrings, identifiers, imports and tag
    values of Python source, from one parse and one walk (None when it does
    not parse: a deliberately broken fixture)."""
    parts = PythonParts()
    starts = _offsets(text)
    try:
        tree = ast.parse(text)
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (SyntaxError, tokenize.TokenError, IndentationError, ValueError):
        return None
    docstring_at = set()
    for node in ast.walk(tree):
        # Names from the tree as well as from the tokens: an identifier used
        # only inside an f-string is a token of its own only from Python 3.12
        # on, and the vocabulary must not depend on the interpreter.
        if isinstance(node, ast.Name):
            parts.names.add(node.id)
        elif isinstance(node, ast.Attribute):
            parts.names.add(node.attr)
        elif isinstance(node, ast.ClassDef):
            parts.classes.append((node.name, node.lineno))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            parts.constants.append((node.value, node.lineno))
        if isinstance(node, ast.Expr):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                docstring_at.add((node.lineno, node.col_offset))
        elif isinstance(node, ast.Import):
            parts.imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and not node.level:
                parts.imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and _TAG_KEY.search(key.value):
                    _tag_strings(value, parts.tags)
        elif isinstance(node, ast.keyword):
            if node.arg and _TAG_KEY.search(node.arg):
                _tag_strings(node.value, parts.tags)
        elif isinstance(node, ast.Assign):
            if any(_tag_named(target) for target in node.targets):
                _tag_strings(node.value, parts.tags)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if node.value is not None and _tag_named(node.target):
                _tag_strings(node.value, parts.tags)
        elif (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("append", "add", "extend", "insert", "update")
            and _tag_named(node.func.value)
        ):
            for argument in node.args:
                _tag_strings(argument, parts.tags)
    fstring_types = {getattr(tokenize, name) for name in ("FSTRING_MIDDLE",) if hasattr(tokenize, name)}
    for token in tokens:
        start = starts[token.start[0] - 1] + token.start[1]
        end = starts[token.end[0] - 1] + token.end[1]
        if token.type == tokenize.STRING or token.type in fstring_types:
            target = parts.docstrings if token.start in docstring_at else parts.strings
            target.append((start, end))
        elif token.type == tokenize.COMMENT:
            parts.comments.append((start, end))
        elif token.type == tokenize.NAME:
            parts.names.add(token.string)
    return parts


_JS_LEXEME = re.compile(
    r"(?P<comment>//[^\n]*|/\*.*?\*/)"
    r"|(?P<string>'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`)",
    re.S,
)
#: A string that is the name of a test: the first argument of it/test/describe.
_JS_TEST_NAME_CALL = re.compile(r"\b(?:it|test|describe)(?:\.\w+)?\(\s*$")


@functools.cache
def js_parts(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]], list[tuple[int, int]]]:
    """(strings, comments, test names) of C-family source — a lexer that
    knows comments and the three kinds of string literal, which is all the
    rules need."""
    strings, comments, test_names = [], [], []
    for match in _JS_LEXEME.finditer(text):
        span = match.span()
        if match.group("comment"):
            comments.append(span)
        else:
            strings.append(span)
            if _JS_TEST_NAME_CALL.search(text, max(0, span[0] - 40), span[0]):
                test_names.append(span)
    return strings, comments, test_names


def kind_of(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    suffix = Path(base).suffix.lower()
    if suffix in PY_SUFFIXES:
        return "python"
    if suffix in JS_SUFFIXES or base == "Jenkinsfile":
        return "js"
    if suffix == ".md":
        return "markdown"
    if suffix in (".html", ".htm", ".svg", ".xml"):
        return "html"
    if suffix == ".vtt":
        return "vtt"
    if suffix == ".json":
        return "json"
    if suffix == ".css":
        return "css"
    if suffix == ".sql":
        return "sql"
    if suffix == ".csv":
        return "csv"
    if suffix in HASH_COMMENT_SUFFIXES or base in HASH_COMMENT_NAMES:
        return "hash"
    return "text"


@functools.cache
def string_view(name: str, text: str) -> str:
    """The parts of a file in which a host can be written: in Python and
    C-family source its string literals and comments (code is identifiers
    and attribute chains, never hosts); everything elsewhere."""
    kind = kind_of(name)
    if kind == "python":
        parts = python_parts(text)
        if parts is not None:
            return mask(text, [*parts.strings, *parts.comments, *parts.docstrings])
    elif kind == "js":
        strings, comments, _ = js_parts(text)
        return mask(text, [*strings, *comments])
    return text


_MD_FENCE = re.compile(r"^\s*(```|~~~)")
_MD_INLINE_CODE = re.compile(r"``[^`\n]+``|`[^`\n]*`")
_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s)>\]\"'`]*")
_HTML_PROSE_ATTRIBUTE = re.compile(r"\b(?:content|alt|title|aria-label|placeholder)=\"([^\"]*)\"")
_HTML_TAG = re.compile(r"<[^>]*>", re.S)
_HTML_RAW = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)


def _hash_comments(text: str, marker: str = "#") -> list[tuple[int, int]]:
    spans = []
    for match in re.finditer(rf"(?:^|(?<=\s)){re.escape(marker)}[^\n]*", text, re.M):
        spans.append(match.span())
    return spans


@functools.cache
def prose_view(name: str, text: str) -> str:
    """The prose of a file — text written for a human reader in sentences —
    with everything else blanked: Markdown outside code (fences and inline
    code spans) and URLs; comments and docstrings of Python; comments and
    test names of C-family source; comments of shell, YAML, TOML, Robot and
    the like (plus Robot's test and keyword names and documentation);
    WebVTT cue text; the text and the human-facing attributes of HTML; the
    string values of JSON that contain a space (UI messages, item names).
    A generated file has no prose."""
    if is_generated(name):
        return ""
    kind = kind_of(name)
    spans: list[tuple[int, int]] = []
    if kind == "python":
        parts = python_parts(text)
        if parts is None:
            return ""
        spans = [*parts.comments, *parts.docstrings]
    elif kind == "js":
        _strings, comments, test_names = js_parts(text)
        spans = [*comments, *test_names]
    elif kind == "css":
        spans = [m.span() for m in re.finditer(r"/\*.*?\*/", text, re.S)]
    elif kind == "sql":
        spans = _hash_comments(text, "--")
    elif kind == "hash":
        spans = _hash_comments(text)
        if name.endswith((".robot", ".resource")):
            spans += _robot_prose(text)
    elif kind == "markdown":
        offset, fenced = 0, False
        for line in text.splitlines(keepends=True):
            if _MD_FENCE.match(line):
                fenced = not fenced
            elif not fenced:
                spans.append((offset, offset + len(line)))
            offset += len(line)
    elif kind == "html":
        hidden = [m.span() for m in _HTML_RAW.finditer(text)]
        for tag in _HTML_TAG.finditer(text):
            if tag.group().startswith("<!--"):
                continue
            kept = [(tag.start() + m.start(1), tag.start() + m.end(1)) for m in _HTML_PROSE_ATTRIBUTE.finditer(tag.group())]
            hidden += _subtract(tag.span(), kept)
        hidden += [m.span() for m in _URL.finditer(text)]
        return mask(text, _complement(text, hidden))
    elif kind == "vtt":
        offset = 0
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            if stripped and stripped != "WEBVTT" and "-->" not in stripped and not stripped.isdigit():
                spans.append((offset, offset + len(line)))
            offset += len(line)
    elif kind == "json":
        for match in re.finditer(r'"((?:\\.|[^"\\\n])*)"\s*(?![\s:])', text):
            if " " in match.group(1):
                spans.append(match.span(1))
    elif kind == "text":
        if name == WORDS_FILE_NAME or name.endswith("/" + WORDS_FILE_NAME):
            spans = _hash_comments(text)
        else:
            spans = [(0, len(text))]
    prose = mask(text, spans)
    # Inline code in a comment or docstring (`name`, ``name``) is code.
    hidden = [*(m.span() for m in _MD_INLINE_CODE.finditer(prose)), *(m.span() for m in _URL.finditer(prose))]
    return mask(prose, _complement(prose, hidden))


def _complement(text: str, hidden: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    spans, position = [], 0
    for start, end in sorted(hidden):
        if start > position:
            spans.append((position, start))
        position = max(position, end)
    spans.append((position, len(text)))
    return spans


def _subtract(span: tuple[int, int], kept: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out, position = [], span[0]
    for start, end in kept:
        out.append((position, start))
        position = end
    out.append((position, span[1]))
    return out


_ROBOT_SECTION = re.compile(r"^\*{3}\s*([A-Za-z ]+?)\s*\*{3}", re.M)


def _robot_prose(text: str) -> list[tuple[int, int]]:
    """Test and keyword names (a line starting in column 0 of those
    sections) and documentation (``[Documentation]``, ``Documentation``,
    and their ``...`` continuations)."""
    spans: list[tuple[int, int]] = []
    section, offset, in_doc = "", 0, False
    for line in text.splitlines(keepends=True):
        header = _ROBOT_SECTION.match(line)
        stripped = line.strip()
        if header:
            section, in_doc = header.group(1).lower(), False
        elif section in ("test cases", "tasks", "keywords") and line[:1] not in (" ", "\t", "#", "\n", ""):
            spans.append((offset, offset + len(line)))
            in_doc = False
        elif re.match(r"\s*(\[Documentation\]|Documentation\b)", line):
            spans.append((offset, offset + len(line)))
            in_doc = True
        elif in_doc and stripped.startswith("..."):
            spans.append((offset, offset + len(line)))
        else:
            in_doc = False
        offset += len(line)
    return spans


# ---------------------------------------------------------------------------
# What the code defines: module names and identifiers
# ---------------------------------------------------------------------------

CODE_DIRS = ("src/", "ui/src/", "ui/e2e/", "tests/", "examples/", "ci/")


@dataclass(frozen=True)
class CodeFacts:
    modules: frozenset[str]  # importable top-level names: stdlib, imported, shipped
    identifiers: frozenset[str]  # every identifier of the code, case-folded
    message_ids: frozenset[str]  # dotted keys of the UI's locale files (``error.code.network``)


@functools.cache
def code_facts(root: Path) -> CodeFacts:
    modules: set[str] = set(sys.stdlib_module_names) | set(sys.builtin_module_names)
    identifiers: set[str] = set()
    message_ids: set[str] = set()
    for name, text in text_files(root):
        if not (name.startswith(CODE_DIRS) or name.endswith("pyproject.toml")) or is_generated(name):
            continue
        kind = kind_of(name)
        stem = Path(name).stem
        if kind == "python":
            modules.add(stem)
            identifiers.add(stem.casefold())
            parts = python_parts(text)
            if parts is None:
                continue
            identifiers.update(word.casefold() for word in parts.names)
            modules.update(parts.imports)
        elif kind == "js":
            strings, comments, _ = js_parts(text)
            code = mask(text, _complement(text, [*strings, *comments]))
            identifiers.update(word.casefold() for word in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", code))
            identifiers.add(stem.split(".")[0].casefold())
        elif name.endswith("pyproject.toml"):
            modules.update(re.findall(r'^build-backend\s*=\s*"([A-Za-z_]\w*)', text, re.M))
        elif name.startswith("ui/src/locales/") and name.endswith(".json"):
            message_ids.update(_flat_keys(json.loads(text)))
    for package in (root / "src").glob("*"):
        modules.add(package.stem)
    return CodeFacts(frozenset(modules), frozenset(identifiers), frozenset(message_ids))


def _flat_keys(node: object, prefix: str = "") -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _flat_keys(value, f"{prefix}{key}.")
    elif prefix:
        yield prefix[:-1]


# ---------------------------------------------------------------------------
# Rule: hosts
# ---------------------------------------------------------------------------

#: Top-level labels that make a dotted name a host. Every two-letter label
#: counts as well (the country-code shape, assigned or not), see ``is_tld``.
HOST_TLDS = frozenset({
    # generic
    "com", "net", "org", "edu", "gov", "mil", "int", "info", "biz", "name", "pro", "mobi", "aero",
    "coop", "museum", "travel", "jobs", "asia", "tel",
    # common newer generic ones
    "app", "dev", "cloud", "tech", "online", "site", "website", "xyz", "top", "shop", "store",
    "page", "link", "blog", "systems", "services", "solutions", "software", "network", "digital",
    "company", "agency", "global", "world", "zone", "works", "tools", "email", "host", "space",
    "live", "club", "codes", "engineering", "group", "team", "ltd", "inc", "llc", "gmbh",
    "consulting", "studio", "design", "media", "news", "center", "today", "build", "run",
    # names private networks use instead of a registered domain
    "local", "internal", "corp", "lan", "intranet", "home", "localdomain", "private", "priv",
    "intra", "office", "wan", "vpn",
    # reserved for documentation and testing (allowed by host_is_allowed)
    "example", "test", "invalid", "localhost",
})
#: Top-level labels that are everyday identifiers as well (``run.id``,
#: ``ctx.live``, ``preview.group``, ``shop.checkout.page``): a name ending
#: in one is a host unless every label of it is an identifier of the code,
#: a word of the vocabulary or a message id of the UI — a dotted reference.
WEAK_TLDS = frozenset({
    "app", "dev", "cloud", "online", "site", "top", "shop", "store", "page", "link", "blog",
    "network", "digital", "global", "world", "zone", "works", "tools", "email", "host", "space",
    "live", "club", "codes", "group", "team", "center", "today", "build", "run", "home", "name",
    "info", "int", "pro", "media", "news", "design", "studio", "private", "domain", "office",
    # two-letter labels that are words of code and English
    "id", "is", "at", "as", "op", "ok", "os", "on", "in", "to", "do", "if", "or", "no", "it", "me",
    "my", "up", "by", "be", "go", "so", "of", "an", "am", "ui", "db", "fs", "ms", "px",
})
#: Suffixes of file names: a dotted name ending in one is a file, not a host.
FILE_SUFFIXES = frozenset({
    "py", "pyi", "pyc", "md", "rst", "txt", "json", "yaml", "yml", "toml", "cfg", "ini", "xml",
    "html", "htm", "css", "js", "mjs", "cjs", "jsx", "ts", "tsx", "map", "sh", "bash", "zsh",
    "ps1", "bat", "cmd", "sql", "csv", "tsv", "vtt", "srt", "mp4", "webm", "mov", "png", "jpg",
    "jpeg", "gif", "webp", "svg", "ico", "pdf", "zip", "gz", "tgz", "tar", "whl", "lock", "log",
    "db", "sqlite", "robot", "resource", "groovy", "jar", "war", "so", "dll", "exe", "env", "out",
    "err", "bak", "tmp", "pid", "sock", "pem", "crt", "key", "keep", "jsonl", "ndjson", "typed",
    "rs", "go", "rb", "pl", "cs", "java", "kt", "cpp", "hpp", "mk", "spec", "conf",
    "properties", "proto", "wasm", "woff", "woff2", "ttf", "otf",
})
#: Hosts of this project's own infrastructure and of the public standards
#: and services its pages, files and workflows point at. Nobody's example
#: data. Each entry is used in the tree; an entry nothing uses is removed
#: (``test_every_listed_host_is_used``).
PROJECT_HOSTS = {
    "github.com": "the project's repository (any path must be under PROJECT_OWNER)",
    "users.noreply.github.com": "the author e-mail domain of pyproject.toml (GitHub's no-reply addresses)",
    "stochasticentropy.github.io": "the project's homepage (GitHub Pages)",
    "img.shields.io": "the README's badges",
    "pypi.org": "the package index the project is published to",
    "json-schema.org": "the meta-schema the run spec schema declares",
    "www.w3.org": "XML/SVG namespaces",
    "fonts.googleapis.com": "the homepage's web font stylesheet",
    "fonts.gstatic.com": "the homepage's web font files",
    "host.docker.internal": "Docker's name for the host machine, used by the CI examples",
    "cdn.jsdelivr.net": "the CDN the web framework's optional API-docs pages load their assets from",
}
#: The only GitHub account this repository links to: its own.
PROJECT_OWNER = "StochasticEntropy"
#: Hosts that package metadata names (registry, funding links, error-page
#: links compiled into a bundle) — allowed in generated files only.
DEPENDENCY_HOSTS = {
    "registry.npmjs.org": "the public npm registry every locked package resolves from",
    "opencollective.com": "funding links in package metadata",
    "tidelift.com": "funding links in package metadata",
    "eslint.org": "funding link in the linter's package metadata",
    "react.dev": "the error-decoder link the UI library compiles into the bundle",
}
#: The file that defines ``DEPENDENCY_HOSTS`` names them — the one place
#: outside a generated file that may.
RULES_MODULE_NAME = "tests/example_world_rules.py"

_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(
    rf"(?<![\w.@/-])((?:{_HOST_LABEL}\.)+[a-z][a-z0-9-]*[a-z0-9])"  # the host
    r"(?::\d{1,5})?"  # optional port
    r"(?![\w-])(?!\.[A-Za-z0-9])"
)
_URL_SCHEME_HOST = re.compile(
    r"\b[a-z][a-z0-9+.-]*://(?:[^\s/@\"'`<>]*@)?([^\s/:?#\"'`<>)\]\\]+)(/[A-Za-z0-9_.~-]+)?"
)
_MAIL = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![\w-])")
_IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.]|\.\d)")
_TEMPLATE_EXPRESSION = re.compile(r"\$\{\{.*?\}\}|\{\{.*?\}\}|\$\{[^}\n]*\}|\{[A-Za-z_][\w.]*\}")


def is_tld(label: str) -> bool:
    return label in HOST_TLDS or (len(label) == 2 and label.isalpha())


def ip_is_allowed(ip: str) -> bool:
    octets = [int(part) for part in ip.split(".")]
    if any(octet > 255 for octet in octets):
        return True  # not an address (a version, a timestamp fragment)
    return (
        octets[0] == 127
        or octets == [0, 0, 0, 0]
        or octets[:3] in ([192, 0, 2], [198, 51, 100], [203, 0, 113])
    )


def host_is_allowed(host: str, path: str = "", generated: bool = False) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith((".example", ".invalid", ".test", ".localhost")):
        return True
    if host in ("example.com", "example.org", "example.net") or host.endswith(
        (".example.com", ".example.org", ".example.net")
    ):
        return True
    if host == "github.com" and path and not generated:
        return path.strip("/").split("/")[0].lower() == PROJECT_OWNER.lower()
    return host in PROJECT_HOSTS or (generated and host in DEPENDENCY_HOSTS)


def _is_reference(labels: list[str], vocab: Vocabulary, facts: CodeFacts) -> bool:
    return all(
        label.casefold() in facts.identifiers or label.casefold() in vocab.words
        for label in labels
    )


def host_candidates(
    name: str, text: str, facts: CodeFacts, vocab: Vocabulary
) -> Iterator[tuple[int, str, str]]:
    """(offset, host, first path segment) of everything host-shaped in the
    parts of ``text`` where a host can be written (``string_view``), with
    template expressions (``${{ matrix.os }}``, ``{run.id}``) left out. In a
    generated file only hosts behind a scheme or an ``@`` are read: bare
    dotted names there are minified code."""
    view = _TEMPLATE_EXPRESSION.sub(lambda m: _BLANK.sub(" ", m.group()), string_view(name, text))
    seen: set[int] = set()
    for match in _URL_SCHEME_HOST.finditer(view):
        host = match.group(1)
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or not re.search(r"[A-Za-z]", host):
            continue  # a placeholder (<host>, $HOST) or an address handled below
        seen.add(match.start(1))
        if "." not in host and (
            len(host) == 1 or host.casefold() in facts.identifiers or host.casefold() in vocab.words
        ):
            continue  # a single word of this project (http://runner:8100), not a machine
        yield match.start(1), host.lower(), match.group(2) or ""
    for match in _MAIL.finditer(view):
        seen.add(match.start(1))
        yield match.start(1), match.group(1).lower(), ""
    if not is_generated(name):
        for match in _HOST.finditer(view):
            host, start = match.group(1), match.start(1)
            if start in seen:
                continue
            labels = host.split(".")
            if labels[-1] in FILE_SUFFIXES or not is_tld(labels[-1]) or labels[0] in facts.modules:
                continue
            if host in facts.message_ids:
                continue
            if labels[-1] in WEAK_TLDS and len(labels) <= 3 and _is_reference(labels, vocab, facts):
                continue
            yield start, host, ""
    for match in _IPV4.finditer(view):
        yield match.start(1), match.group(1), ""


def check_hosts(name: str, text: str, facts: CodeFacts, vocab: Vocabulary) -> list[Violation]:
    generated = is_generated(name) or name == RULES_MODULE_NAME
    violations = []
    for offset, host, path in host_candidates(name, text, facts, vocab):
        if _IPV4.fullmatch(host):
            if ip_is_allowed(host):
                continue
        elif host_is_allowed(host, path, generated):
            continue
        token = host + (path if host == "github.com" else "")
        violations.append(Violation(name, line_of(text, offset), "host", token))
    return violations


# ---------------------------------------------------------------------------
# Rule: absolute paths
# ---------------------------------------------------------------------------

#: First segments of an absolute path that make it a filesystem path (and
#: not a URL path or an API route).
FILESYSTEM_ROOTS = (
    "Users", "home", "root", "tmp", "var", "opt", "etc", "usr", "srv", "mnt", "media", "Volumes",
    "private", "data", "app", "workspace", "workspaces", "builds", "net", "nfs", "scratch",
    "export", "proj", "projects", "work", "Library", "System", "Applications", "cygdrive", "c", "d",
)
#: Generic absolute paths: not anybody's machine. Each entry is one reviewed
#: path — an entry ending in ``/`` covers what lies below it, any other is
#: that path (and what lies below it) only; never a whole filesystem root
#: such as ``/opt/``, under which anybody's layout would pass. Each says
#: where the tree uses it; an entry nothing uses is removed
#: (``test_every_generic_path_is_used``).
GENERIC_PATHS = {
    "/tmp/fresh/": "a throwaway virtualenv of a CI step",
    "/tmp/minimal/": "the CI step's minimal install",
    "/tmp/rt/": "the CI step's round trip",
    "/tmp/tpl/": "the CI step's connector template",
    "/tmp/venv/": "a CI step's virtualenv",
    "/tmp/verify/": "the release check's virtualenv",
    "/tmp/work": "a CI step's work directory",
    "/tmp/ids.txt": "the consumer tests' ids file",
    "/tmp/out/": "the consumer tests' output directory",
    "/tmp/x": "a planted path in the guard's own self-test",
    "/var/lib/apt/": "the package lists a Dockerfile removes after installing",
    "/opt/runcomposer/runcomposer_exec.py": "where the CI Jenkins image installs runcomposer-exec",
    "/opt/shop/keywords": "a keyword path of the example shop in a config test",
    "/usr/bin/": "system executables (the env shebang)",
    "/usr/share/": "the Jenkins image's reference files (ci/jenkins)",
    "/srv/shop": "the example shop's service directory in deployment tests and docs",
    "/srv/envs/broken/config.yaml": "a broken deployment's config in the CLI docs",
    "/srv/robot-pool/": "the robot-pool example's directory in the CLI docs",
    "/etc/passwd": "the classic target a path-traversal test aims at",
    "/etc/machine-id": "the system's machine id an owner identity is keyed by (app.liveness)",
    "/var/lib/dbus/machine-id": "the older place of the system's machine id (app.liveness)",
}
_PLACEHOLDER = re.compile(r"[<{$…]|\.\.\.|%[A-Z]")
_POSIX_PATH = re.compile(
    rf"(?<![\w.~$}}\]):/@-])(/(?:{'|'.join(FILESYSTEM_ROOTS)})(?![\w-])(?:/[^\s\"'`()\[\],;|]*)?)"
)
# Windows paths as prose writes them (C:\Users\<name>) and as a string
# literal escapes them (C:\\Users\\<name>); UNC paths likewise, raw
# (\\<server>\<share>) or escaped (\\\\<server>\\<share>).
_WINDOWS_PATH = re.compile(r"(?<![\w\\])([A-Za-z]:(?:\\{1,2}[^\s\"'`|\\();,]+)+)")
_UNC_PATH = re.compile(
    r"(?<![\w\\:])(\\\\([A-Za-z][\w.-]*)\\(?!\\)[\w$.-]+)"
    r"|(?<![\w\\:])(\\\\\\\\([A-Za-z][\w.-]*)\\\\(?!\\)[\w$.-]+)"
)


def path_is_allowed(path: str) -> bool:
    if _PLACEHOLDER.search(path):
        return True
    for prefix in GENERIC_PATHS:
        if path == prefix.rstrip("/") or path.startswith(prefix if prefix.endswith("/") else (prefix, prefix + "/")):
            return True
    return False


def windows_path_is_allowed(path: str) -> bool:
    """A drive path is generic when it names one entry under the drive root
    (``C:\\x``, a test of path handling) and is not a home directory; a
    deeper path is somebody's machine layout."""
    segments = [segment for segment in re.split(r"\\+", path)[1:] if segment]
    if _PLACEHOLDER.search(path):
        return True
    return len(segments) <= 1 and segments[:1] != ["Users"]


def check_paths(name: str, text: str, vocab: Vocabulary, facts: CodeFacts) -> list[Violation]:
    violations = []
    for match in _POSIX_PATH.finditer(text):
        path = match.group(1).rstrip(".:")
        if path.rstrip("/") in ("/" + root for root in FILESYSTEM_ROOTS):
            continue  # a bare root names no machine
        if not path_is_allowed(path):
            violations.append(Violation(name, line_of(text, match.start(1)), "path", path))
    for match in _WINDOWS_PATH.finditer(text):
        if not windows_path_is_allowed(match.group(1)):
            violations.append(Violation(name, line_of(text, match.start(1)), "path", match.group(1)))
    for match in _UNC_PATH.finditer(text):
        path, server = (match.group(1), match.group(2)) if match.group(1) else (match.group(3), match.group(4))
        # A server that is a word of this project (\\server\share) is the
        # textbook placeholder; any other names a machine.
        if server.casefold() not in vocab.words and server.casefold() not in facts.identifiers:
            violations.append(Violation(name, line_of(text, match.start()), "path", path))
    return violations


# ---------------------------------------------------------------------------
# Rule: ticket keys
# ---------------------------------------------------------------------------

_TICKET_KEY = re.compile(r"(?<![\w-])([A-Z][A-Z0-9]{1,9})-[0-9]+(?![\w])")
#: The example world's ticket prefix.
TICKET_PREFIXES = {"SHOP"}
#: Standards whose names are spelled like a ticket key.
STANDARD_PREFIXES = {"ISO", "UTF", "SHA", "RFC", "PEP", "CVE", "CWE", "GHSA", "ECMA", "IEEE", "X"}


def check_ticket_keys(name: str, text: str) -> list[Violation]:
    return [
        Violation(name, line_of(text, match.start()), "ticket-key", match.group(0))
        for match in _TICKET_KEY.finditer(text)
        if match.group(1) not in TICKET_PREFIXES | STANDARD_PREFIXES
    ]


# ---------------------------------------------------------------------------
# The vocabulary: the words file and the example world
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Vocabulary:
    words: frozenset[str]  # case-folded: reviewed words + example-world words
    tags: frozenset[str]  # case-folded tag vocabulary
    sections: dict[str, frozenset[str]]  # words file: heading -> words (for the self-tests)
    lowercase: frozenset[str] = frozenset()  # the reviewed lower-case words (LOWERCASE_FILE_NAME)


def parse_words_file(text: str) -> dict[str, frozenset[str]]:
    """``# heading`` lines open a section; every other non-blank line is one
    token (a trailing ``# …`` comment is allowed)."""
    sections: dict[str, set[str]] = {}
    heading = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            heading = line[3:].strip()
            sections.setdefault(heading, set())
        elif line and not line.startswith("#"):
            token = line.split("#", 1)[0].strip()
            if token:
                sections.setdefault(heading, set()).add(token)
    return {heading: frozenset(words) for heading, words in sections.items()}


#: The heading of the words file under which invented tags are listed.
INVENTED_TAGS_HEADING = "Invented tags"


def corpus_tags(root: Path) -> set[str]:
    corpus = json.loads((root / "src/runcomposer/demo/corpus.json").read_text(encoding="utf-8"))
    return {tag for item in corpus["items"] for tag in item["tags"]}


def example_tags(root: Path) -> set[str]:
    """Tags the shipped examples spell: their manifests' and suites' tags
    (plain tags — not patterns or templates)."""
    found: set[str] = set()
    for name, text in text_files(root):
        if name.startswith("examples/") and not is_generated(name):
            found.update(
                tag for tag, _offset in data_tags(name, text)
                if _TAG_SHAPED.fullmatch(tag) and len(tag) > 1
            )
    return found


@functools.cache
def vocabulary(root: Path) -> Vocabulary:
    corpus = json.loads((root / "src/runcomposer/demo/corpus.json").read_text(encoding="utf-8"))
    taxonomy = (root / "src/runcomposer/demo/taxonomy.yaml").read_text(encoding="utf-8")
    world: set[str] = set()
    for item in corpus["items"]:
        for text in (*item["tags"], item.get("name", ""), *item.get("hierarchy", ())):
            world.update(re.findall(r"[A-Za-z]+", text))
    world.update(re.findall(r"label:\s*([A-Za-z]+)", taxonomy))
    words_path = root / WORDS_FILE_NAME
    sections = parse_words_file(words_path.read_text(encoding="utf-8")) if words_path.is_file() else {}
    reviewed = {word for words in sections.values() for word in words}
    tags = corpus_tags(root) | set(sections.get(INVENTED_TAGS_HEADING, ()))
    base = Vocabulary(
        words=frozenset(word.casefold() for word in world | reviewed | tags),
        tags=frozenset(tag.casefold() for tag in tags),
        sections=sections,
    )
    # A tag of the shipped examples joins the vocabulary when it is made of
    # words the vocabulary or the code already has: an example cannot bring
    # a word of its own in by being an example (`Account-Profile` can; a
    # word nobody reviewed cannot, and fails where the example spells it).
    facts = code_facts(root)
    tags |= {tag for tag in example_tags(root) if _words_known(tag, base, facts)}
    lowercase_path = root / LOWERCASE_FILE_NAME
    lowercase = (
        {
            line.split("#", 1)[0].strip().casefold()
            for line in lowercase_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        if lowercase_path.is_file()
        else set()
    )
    return Vocabulary(
        words=frozenset(word.casefold() for word in world | reviewed | tags),
        tags=frozenset(tag.casefold() for tag in tags),
        sections=sections,
        lowercase=frozenset(lowercase),
    )


# ---------------------------------------------------------------------------
# Rule: tags
# ---------------------------------------------------------------------------

#: Derived tag forms: a tag matching one of these is of the vocabulary.
DERIVED_TAGS = (
    # the executed-tag template (a runner setting) and the tags it produces
    re.compile(r"RanOn-(?:\{[a-z_]+\}|[A-Za-z0-9-]+)"),
    re.compile(r"Sprint-[0-9]+", re.I),
    re.compile(r"SHOP-1[0-9]{3}", re.I),
)
_TAG_SHAPED = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)*")
_TAG_KEY = re.compile(r"(?:^|_)(?:tags?|tag_filter|force_tags|test_tags|default_tags)(?:_|$)", re.I)
#: A key whose value is a filter when the value is shaped like one.
_FILTER_KEY = re.compile(r"^(?:filter|selection_filter)$", re.I)
_FILTER_NODE_KEYS = {"op", "items", "not"}
_PLACEHOLDER_TAG = re.compile(r"[<{…]|\.\.\.|^\^?[A-Z]\$?$")


def _fragment_of_vocabulary(word: str, vocab: Vocabulary, facts: CodeFacts | None) -> bool:
    """A word of a pattern that selects nothing on purpose (an anchoring or
    a case test): a word of the vocabulary or the code (``regex`` itself),
    or a piece (three letters or more) of one of its tags — ``heckout``,
    ``print``."""
    word = word.casefold()
    return (
        word in vocab.words
        or (facts is not None and word in facts.identifiers)
        or (len(word) >= 3 and any(word in known for known in vocab.tags))
    )


def _words_known(text: str, vocab: Vocabulary, facts: CodeFacts | None) -> bool:
    words = [part for part in re.split(r"[-_\s]+|(?<=[a-z])(?=[A-Z])", text) if part and not part.isdigit()]
    return bool(words) and all(
        len(word) < 2 or word.casefold() in vocab.words or (facts is not None and word.casefold() in facts.identifiers)
        for word in words
    )


def tag_is_allowed(
    tag: str, vocab: Vocabulary, facts: CodeFacts | None = None, *, loose: bool = False
) -> bool | None:
    """True/False for a tag-shaped literal — a plain tag, or a ``prefix:`` /
    ``regex:`` pattern; None when the literal is no tag (prose, a path, an
    expression with spaces) or a placeholder (``<tag>``, ``X``, ``FILTER``,
    ``RanOn-{name}``-style templates are checked against ``DERIVED_TAGS``).

    A capitalised tag must be in the tag vocabulary, a derived form, or a
    vocabulary tag with a number appended (``Checkout-20``, a generated
    corpus). A tag written in lower case (``smoke``, an API router's
    ``runs``) is made of words of the vocabulary or identifiers of the code.
    ``loose`` is for literals that are only *possibly* tags (any quoted
    string of a UI test): there a capitalised literal also passes as a
    prefix of a tag (typed into a search box) or a phrase of known words (a
    label)."""
    tag = tag.strip()
    if tag.startswith("prefix:"):
        body = tag[len("prefix:"):]
        if not body or _PLACEHOLDER_TAG.search(body):
            return None
        folded = body.casefold()
        if any(known.startswith(folded) for known in vocab.tags):
            return True
        words = re.findall(r"[A-Za-z]{2,}", body)
        return bool(words) and all(_fragment_of_vocabulary(word, vocab, facts) for word in words)
    if tag.startswith("regex:"):
        body = tag[len("regex:"):]
        if "<" in body or "…" in body or re.search(r"\{[A-Za-z_]", body) or re.fullmatch(r"\^?[A-Z]\$?", body):
            return None  # a placeholder: regex:<expression>, regex:^X
        try:
            compiled = re.compile(body, re.I)
        except re.error:
            return None  # a deliberately broken expression is a grammar test
        if any(compiled.search(known) for known in vocab.tags):
            return True
        # A pattern that matches nothing (the empty selection, an anchoring
        # test) is still of this world when its words are.
        words = [w for run in re.findall(r"[A-Za-z]{2,}", body) for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", run)]
        return all(_fragment_of_vocabulary(word, vocab, facts) for word in words if len(word) > 1)
    if any(pattern.fullmatch(tag) for pattern in DERIVED_TAGS):
        return True
    if _PLACEHOLDER_TAG.search(tag) or not _TAG_SHAPED.fullmatch(tag) or len(tag) == 1:
        return None
    folded = tag.casefold()
    if folded in vocab.tags or re.sub(r"-[0-9]+$", "", folded) in vocab.tags:
        return True
    if not any(ch.isupper() for ch in tag):
        return _words_known(tag, vocab, facts)
    if loose:
        if len(folded) >= 3 and any(known.startswith(folded) for known in vocab.tags):
            return True
        return _words_known(tag, vocab, facts)
    return False


def _filter_strings(node: object) -> Iterator[str]:
    """The leaf patterns of a structured filter (``{op, items}``, ``{not}``)
    or of a list of tags."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for child in node:
            yield from _filter_strings(child)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key != "op":
                yield from _filter_strings(value)


def _is_tag_value(value: object) -> bool:
    """A value that can be tags or a filter: a string, a list, or a filter
    node — not a mapping of something else (a locale's ``filter`` section
    of messages, a ``tags`` section of settings)."""
    if isinstance(value, dict):
        return bool(value) and set(value) <= _FILTER_NODE_KEYS
    return isinstance(value, (str, list))


def tags_in_data(data: object) -> list[str]:
    """Values under a tag-named key (``tags``, ``tag_filter``, ``force_tags``
    …, and ``filter`` when its value is a filter) at any depth of parsed
    JSON/YAML."""
    found: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            named = isinstance(key, str) and (_TAG_KEY.search(key) or _FILTER_KEY.match(key))
            if named and _is_tag_value(value):
                found.extend(_filter_strings(value))
            else:
                found.extend(tags_in_data(value))
    elif isinstance(data, list):
        for value in data:
            found.extend(tags_in_data(value))
    return found


def tags_in_python(source: str) -> list[tuple[str, int]]:
    """String constants given under a tag-named name — dict entries, keyword
    arguments, assignments (``TAGS = [...]``, ``tags: list = ...``) — through
    lists, tuples, sets, ``frozenset(...)``-style calls and filter dicts
    (except a filter node's ``op``), as ``(value, line)``."""
    parts = python_parts(source)
    return list(parts.tags) if parts is not None else []


def _tag_strings(node: ast.AST, found: list[tuple[str, int]]) -> None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        found.append((node.value, node.lineno))
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for element in node.elts:
            _tag_strings(element, found)
    elif isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=True):
            if not (isinstance(key, ast.Constant) and key.value == "op"):
                _tag_strings(value, found)
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
        "frozenset", "set", "list", "tuple", "sorted",
    ):
        for argument in node.args:
            _tag_strings(argument, found)


def _tag_named(target: ast.AST) -> bool:
    name = target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else ""
    return bool(name) and bool(_TAG_KEY.search(name))


_ROBOT_TAG_SETTING = re.compile(
    r"^[ \t]*(?:\[Tags\]|Force Tags|Test Tags|Default Tags|Keyword Tags)((?:[ \t]{2,}|\t)[^\n]*)$", re.M
)
_ROBOT_CONTINUATION = re.compile(r"\n[ \t]*\.\.\.((?:[ \t]{2,}|\t)[^\n]*)")


def robot_tags(text: str) -> list[tuple[str, int]]:
    """Tags of Robot ``[Tags]`` and tag settings, continuation lines
    included, wherever such a line appears (a suite, or a suite embedded in
    a test as a string)."""
    found: list[tuple[str, int]] = []
    for match in _ROBOT_TAG_SETTING.finditer(text):
        values = [(match.group(1), match.start(1))]
        position = match.end()
        while (more := _ROBOT_CONTINUATION.match(text, position)) is not None:
            values.append((more.group(1), more.start(1)))
            position = more.end()
        for value, offset in values:
            for cell in re.split(r"\t|[ \t]{2,}|\\n|\\t", value):
                cell = cell.strip().strip("'\"")
                if cell and not cell.startswith(("#", "${", "@{", "&{", "%{")):
                    found.append((cell, offset))
    return found


def data_tags(name: str, text: str) -> list[tuple[str, int]]:
    """(tag, offset) of every tag the file's data spells: Robot tag lines;
    JSON/YAML values under tag-named keys; Python values under tag-named
    names; a ``tags`` column of a CSV file."""
    import yaml

    found = robot_tags(text)
    kind = kind_of(name)
    try:
        if name.endswith(".json"):
            found += [(tag, 0) for tag in tags_in_data(json.loads(text))]
        elif name.endswith((".yaml", ".yml")):
            for document in yaml.safe_load_all(text):
                found += [(tag, 0) for tag in tags_in_data(document)]
        elif kind == "python":
            starts = _offsets(text)
            found += [(tag, starts[line - 1]) for tag, line in tags_in_python(text)]
        elif kind == "csv":
            lines = text.splitlines()
            header = [cell.strip().lower() for cell in lines[0].split(",")] if lines else []
            for column, title in enumerate(header):
                if _TAG_KEY.search(title):
                    starts = _offsets(text)
                    for number, row in enumerate(lines[1:], start=1):
                        cells = row.split(",")
                        if column < len(cells):
                            found += [(tag, starts[number]) for tag in re.split(r"[;| ]+", cells[column]) if tag]
    except (ValueError, yaml.YAMLError, SyntaxError):
        pass  # not data (a template, a deliberately broken fixture)
    return found


def _locate(text: str, tag: str, offset: int) -> int:
    """The offset of ``tag`` in ``text``, preferring the given hint (data
    parsed by a library has no offsets of its own)."""
    if offset:
        return offset
    match = re.search(rf"(?<![\w-]){re.escape(tag)}(?![\w-])", text)
    return match.start() if match else 0


# Filter expressions in any text: the operands of AND / OR / NOT.
_OPERAND = r"(?<![\w.-])[A-Z][A-Za-z0-9_]*(?:-[A-Za-z0-9]+)*(?![\w.-])"
_EXPRESSION = re.compile(
    rf"(?:({_OPERAND})\)?\s+(?:AND|OR)\s+(?:NOT\s+)?\(?({_OPERAND}))"
    rf"|(?:\bNOT\s+\(?({_OPERAND}))"
)
_OPERATORS = {"AND", "OR", "NOT"}
#: Uppercase words that follow NOT in query-language and status spellings
#: (`IS NOT NULL`, `IF NOT EXISTS`, `NOT RUN`). Language keywords, not tags.
_EXPRESSION_KEYWORDS = {"NULL", "EXISTS", "RUN", "IN", "LIKE"}
#: Verbatim legal text is written in capitals and is nobody's example data.
_NOT_EXAMPLE_DATA = {"LICENSE"}
_PATTERN_LITERAL = re.compile(r"(?<![\w-])((?:prefix|regex):[^\s'\"`,)\]}\\&|]+)")
_CLI_FILTER = re.compile(
    r"(?m)(?:^|(?<=[`$])|(?<=\$ ))\s*runc" r"omposer[ \t]+(?:compile|spec|run|plan)[ \t]+"
    r"(?:--?[\w-]+(?:[ =](?!-)[^\s`]+)?[ \t]+)*"
    r"(?:'([^'\n]+)'|\"([^\"\n]+)\"|([^\s'\"`-][^\s`]*))"
)
_CLI_TAG_OPTION = re.compile(r"--tags?[ =](?:'([^'\n]+)'|\"([^\"\n]+)\"|([^\s'\"-]\S*))")


def expression_operands(text: str) -> list[tuple[str, int]]:
    operands: list[tuple[str, int]] = []
    position = 0
    while (match := _EXPRESSION.search(text, position)) is not None:
        operands.extend((group, match.start(index)) for index, group in enumerate(match.groups(), 1) if group)
        # Overlapping scan: `A AND B OR C` must yield B as a left operand too.
        position = match.start() + 1
    return operands


def _filter_argument_tags(value: str) -> list[str]:
    """The tags of a filter written on a command line: a structured filter
    (YAML flow), a pattern, or a bare tag; an expression's operands are
    found by the expression scanner."""
    import yaml

    value = value.strip()
    if re.fullmatch(r"\[[A-Z_ .]+\]|[A-Z_]{2,}", value):
        return []  # a usage line's metavariable: [FILTER], FILTER
    if value.startswith(("{", "[")):
        try:
            return list(_filter_strings(yaml.safe_load(value)))
        except yaml.YAMLError:
            return []
    if " " in value:
        return []
    return [value]


def text_tags(name: str, text: str) -> list[tuple[str, int]]:
    """(tag, offset) of every tag written in running text: expression
    operands, ``prefix:``/``regex:`` patterns, the filter argument of a
    ``runcomposer compile|spec|run|plan`` command, and ``--tag`` values."""
    found = [
        (operand, offset) for operand, offset in expression_operands(text)
        if operand not in _OPERATORS and operand not in _EXPRESSION_KEYWORDS
    ]
    found += [
        (re.sub(r"</\w+>.*$", "", m.group(1)).rstrip(".:;"), m.start(1)) for m in _PATTERN_LITERAL.finditer(text)
    ]
    for pattern in (_CLI_FILTER, _CLI_TAG_OPTION):
        for match in pattern.finditer(text):
            value = next(group for group in match.groups() if group)
            start = next(match.start(i) for i, group in enumerate(match.groups(), 1) if group)
            found += [(tag, start) for tag in _filter_argument_tags(value)]
    return found


def ui_test_files(root: Path) -> list[str]:
    """The UI's unit tests (``*.test.ts[x]`` anywhere under ``ui/src``) and
    every file of its browser end-to-end tests (``ui/e2e``, at any depth)."""
    return [
        name for name in list_files(root)
        if (name.startswith("ui/src/") and re.search(r"\.test\.tsx?$", name))
        or (name.startswith("ui/e2e/") and name.endswith((".ts", ".tsx")))
    ]


def quoted_strings(text: str) -> list[tuple[str, int]]:
    return [
        (match.group(match.lastindex or 0), match.start(match.lastindex or 0))
        for match in re.finditer(r'"([^"\n]{1,80})"|\'([^\'\n]{1,80})\'|`([^`\n]{1,80})`', text)
    ]


#: The UI's message catalogs: their values are text shown to a person (read
#: as prose), never test data — a ``filter`` key there is a group of messages.
MESSAGE_CATALOG = re.compile(r"^ui/src/locales/[^/]+\.json$")
#: Names of keys a browser test presses. Not tags, and nobody's test data.
KEYBOARD_KEYS = {"Tab", "Enter", "Escape", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Space", "Backspace", "Home", "End"}


@functools.cache
def ui_own_vocabulary(root: Path) -> frozenset[str]:
    """What the product itself spells, and its UI tests may therefore spell
    back: whatever the UI's non-test source has in quotes, every text and
    key of its locale files, and what the bundled plugins have in quotes (a
    runner's parameter labels are shown verbatim). Derived, never listed."""
    own: set[str] = {"AND", "OR", "NOT"} | KEYBOARD_KEYS
    for name, text in text_files(root):
        if name.startswith("ui/src/") and kind_of(name) == "js" and ".test." not in name:
            own.update(value for value, _ in quoted_strings(text))
        elif name.startswith("ui/src/locales/") and name.endswith(".json"):
            own.update(re.findall(r'"((?:\\.|[^"\\\n])*)"', text))
        elif name.startswith("src/runcomposer/plugins/") and name.endswith(".py"):
            own.update(value for value, _ in quoted_strings(text))
    return frozenset(own)


#: A tag shaped as this world's tags are: capitalised words, hyphenated.
_TITLE_TAG = re.compile(r"[A-Z][a-z]+(?:-[A-Z][a-z]+)*")
#: The guard's own self-tests plant what it must catch.
_GUARD_SELF_TESTS = frozenset({"tests/test_example_world.py", "tests/test_self_containment.py"})


@functools.cache
def python_own_vocabulary(root: Path) -> frozenset[str]:
    """What the package itself spells in a string constant — a status, a
    state, an error code, a kind — which its tests may spell back."""
    own: set[str] = set()
    for name, text in text_files(root):
        if name.startswith("src/") and kind_of(name) == "python":
            parts = python_parts(text)
            if parts is not None:
                own.update(value for value, _line in parts.constants)
    return frozenset(own)


def check_tags(name: str, text: str, vocab: Vocabulary, facts: CodeFacts, root: Path) -> list[Violation]:
    if is_generated(name) or name in _NOT_EXAMPLE_DATA:
        return []
    candidates = []
    if not MESSAGE_CATALOG.match(name):
        candidates += [(tag, _locate(text, tag, offset), False) for tag, offset in data_tags(name, text)]
    candidates += [(tag, offset, False) for tag, offset in text_tags(name, text)]
    if name in ui_test_files(root):
        own = ui_own_vocabulary(root)
        candidates += [
            (value.strip(), offset, True)
            for value, offset in quoted_strings(text)
            if value.strip() not in own and value.strip()[:1].isupper()
        ]
    elif name.startswith("tests/") and kind_of(name) == "python" and name not in _GUARD_SELF_TESTS | {RULES_MODULE_NAME}:
        # Every string constant of a Python test shaped like a tag of this
        # world — capitalised words, hyphenated (``Checkout``,
        # ``Payments-Cards``), in a parametrize list, a plain list, a dict
        # key or a call argument — is read as the UI tests' literals are: a
        # tag of the vocabulary, or words of it, unless the product itself
        # spells the literal. (ALL-CAPS names are configuration keys and
        # parameter names, checked as prose tokens where they are prose.)
        parts = python_parts(text)
        own = python_own_vocabulary(root)
        if parts is not None:
            starts = _offsets(text)
            candidates += [
                (value, starts[line - 1], True)
                for value, line in parts.constants
                if _TITLE_TAG.fullmatch(value) and value not in own
            ]
    violations = []
    for tag, offset, loose in candidates:
        if tag_is_allowed(tag, vocab, facts, loose=loose) is False:
            violations.append(Violation(name, line_of(text, offset), "tag", tag.strip()))
    return violations


# ---------------------------------------------------------------------------
# Rule: prose tokens
# ---------------------------------------------------------------------------

#: A word: letters of any script (German captions have umlauts), digits
#: after the first letter, an English possessive, hyphenated parts.
_WORD = re.compile(r"(?<![\w$@/\\.-])([^\W\d_][^\W_]*(?:['’][^\W\d_]+)?(?:-[^\W_]+)*)(?![\w/\\@]|\.\w)")
_SENTENCE_END = re.compile(r"[.!?:;…·•]['\"’”)\]*_`]*$|^$")
_OPENING = re.compile(r"[\s\[(*_\"'“‘`<>#⚠✓✗→←↳▶]+$")
_LINE_START_MARKERS = re.compile(r"^\s*(?:[#>|*+-]+\s*|\d+[.)]\s+|//+\s*|/?\*+\s*|--\s*|\.\.\.\s*|\"\"\"|''')*")


def _needs_review(token: str) -> bool:
    """Whether a token says more than an ordinary word could: CamelCase
    (an upper-case letter after the first), ALL-CAPS, or hyphenated parts
    of which one is capitalised."""
    letters = token.replace("-", "")
    return any(ch.isupper() for ch in letters[1:])


def _known(token: str, vocab: Vocabulary, facts: CodeFacts) -> bool:
    folded = token.casefold()
    return folded in vocab.words or folded in facts.identifiers


def prose_tokens(name: str, text: str) -> Iterator[tuple[str, int, bool]]:
    """(token, offset, sentence-initial) for every word with an upper-case
    letter in the prose of a file."""
    view = prose_view(name, text)
    offset = 0
    previous = ""  # the text of the previous prose line
    for line in view.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            previous = ""
            offset += len(line)
            continue
        marker = _LINE_START_MARKERS.match(line)
        body_start = marker.end() if marker else 0
        structural = bool(marker and marker.group().strip())
        for match in _WORD.finditer(line):
            token = match.group(1)
            if not any(ch.isupper() for ch in token):
                continue
            # Opening punctuation and symbols do not end what came before:
            # `[See above](…)`, `**Your source …**`, `⚠ These …`.
            before = _OPENING.sub("", line[body_start:match.start()])
            if match.start() <= body_start or not before:
                initial = structural or bool(_SENTENCE_END.search(previous.rstrip()))
            else:
                initial = bool(_SENTENCE_END.search(before)) or before.endswith(("|", "·", "•"))
            yield token, offset + match.start(), initial
        previous = stripped
        offset += len(line)


def check_prose(name: str, text: str, vocab: Vocabulary, facts: CodeFacts) -> list[Violation]:
    violations = []
    for token, offset, initial in prose_tokens(name, text):
        token = re.sub(r"['’]s$", "", token)
        if len(token.replace("-", "")) < 2:
            continue
        if initial and not _needs_review(token):
            continue
        if _known(token, vocab, facts):
            continue
        if "-" in token:
            parts = [part for part in token.split("-") if part]
            if all(
                len(part) < 2 or part.isdigit() or _known(part, vocab, facts)
                or (part.islower() and not _needs_review(part))
                for part in parts
            ):
                continue
        violations.append(Violation(name, line_of(text, offset), "prose-token", token))
    return violations


# ---------------------------------------------------------------------------
# Rule: lower-case tokens
# ---------------------------------------------------------------------------

#: A lower-case word of prose, hyphenated parts included.
_LOWER_WORD = re.compile(r"(?<![\w$@/\\.-])([a-z\u00df-\u00ff][a-z0-9\u00df-\u00ff]*(?:['’][a-z]+)?(?:-[a-z0-9\u00df-\u00ff]+)*)(?![\w/\\@]|\.\w)")
#: A hyphenated lower-case identifier anywhere a host could be written — a
#: job name, a cluster, a queue (``shop-nightly-eu``).
_HYPHENATED = re.compile(r"(?<![\w$@/\\.:%-])([a-z][a-z0-9]*(?:-[a-z0-9]+)+)(?![\w/\\@:-]|\.\w)")
_CAMEL_PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+")


def _lower_known(word: str, vocab: Vocabulary, facts: CodeFacts) -> bool:
    folded = re.sub(r"['’].*$", "", word).casefold()
    return (
        len(folded) < 2 or folded.isdigit() or folded in vocab.words or folded in vocab.lowercase
        or folded in facts.identifiers
    )


def _parts(token: str) -> list[str]:
    return [part for part in token.split("-") if part]


def check_lowercase(name: str, text: str, vocab: Vocabulary, facts: CodeFacts) -> list[Violation]:
    """Lower-case words the vocabulary does not know: every word of prose
    (``prose_view``), every hyphenated lower-case identifier in the string
    view (a job or cluster name in a YAML value or a string literal), every
    word of a JSON string value and of SQL, and the words of a class name.
    Known is a word of the reviewed vocabulary, the reviewed lower-case
    list (``LOWERCASE_FILE_NAME``), the example world or the code."""
    if is_generated(name) or name in (RULES_MODULE_NAME, LOWERCASE_FILE_NAME):
        return []
    found: list[Violation] = []
    seen: set[tuple[int, str]] = set()

    def flag(offset: int, token: str) -> None:
        key = (line_of(text, offset), token)
        if key not in seen:
            seen.add(key)
            found.append(Violation(name, key[0], "lowercase-token", token))

    view = prose_view(name, text)
    for match in _LOWER_WORD.finditer(view):
        token = match.group(1)
        if any(not _lower_known(part, vocab, facts) for part in _parts(token)):
            flag(match.start(1), token)
    strings = string_view(name, text)
    # A URL's path names things too: a job in a CI address.
    for url in _URL.finditer(strings):
        path = url.group().split("://", 1)[-1].partition("/")[2]
        for segment in re.finditer(r"[^/?#&=]+", path):
            token = segment.group()
            if _HYPHENATED.fullmatch(token) and any(
                not _lower_known(part, vocab, facts) for part in _parts(token) if not part.isdigit()
            ):
                flag(url.start(), token)
    strings = mask(strings, _complement(strings, [m.span() for m in _URL.finditer(strings)]))
    for match in _HYPHENATED.finditer(strings):
        token = match.group(1)
        if any(not _lower_known(part, vocab, facts) for part in _parts(token) if not part.isdigit()):
            flag(match.start(1), token)
    kind = kind_of(name)
    if kind == "json":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        for value in _json_strings(data):
            # Text — a value with a space — or one plain word; an id, a hash
            # or an encoded value is neither.
            if " " in value.strip() or re.fullmatch(r"[A-Za-z][a-z]+", value):
                for word in re.findall(r"(?<![\w-])[A-Za-z][a-z]+(?![\w-])", _URL.sub(" ", value)):
                    if not _lower_known(word, vocab, facts):
                        flag(text.find(word), word)
    elif kind == "sql":
        # The statements' words — keywords, tables, columns — outside string
        # literals (values: hashes, encoded documents) and comments (prose).
        code = mask(text, _complement(text, [m.span() for m in re.finditer(r"'(?:''|[^'])*'|--[^\n]*", text)]))
        for match in re.finditer(r"(?<![\w])[A-Za-z][a-z]*(?=[_\W]|$)", code):
            if not _lower_known(match.group(), vocab, facts):
                flag(match.start(), match.group())
    if kind == "python" and name.startswith(CODE_DIRS):
        parts = python_parts(text)
        if parts is not None:
            starts = _offsets(text)
            for class_name, line in parts.classes:
                words = [w for w in _CAMEL_PARTS.findall(class_name.strip("_")) if not w.isdigit()]
                if any(
                    len(word) > 1 and word.casefold() not in vocab.words and word.casefold() not in vocab.lowercase
                    for word in words
                ):
                    flag(starts[line - 1], class_name)
    return found


def _json_strings(data: object) -> Iterator[str]:
    """The string values of parsed JSON, keys left out."""
    if isinstance(data, str):
        yield data
    elif isinstance(data, list):
        for value in data:
            yield from _json_strings(value)
    elif isinstance(data, dict):
        for value in data.values():
            yield from _json_strings(value)


# ---------------------------------------------------------------------------
# Rule: process words (commit messages)
# ---------------------------------------------------------------------------

#: How a change came about is not part of the project's history. Decided
#: per word:
#:
#: * review, reviewer, judge, finding, gap list, round <n>, predecessor,
#:   coordinator — flagged in every form; none of them names anything the
#:   project has (there is no review feature, no rounds of anything);
#: * agent — the project *has* agents: the run agent of
#:   ``examples/remote-agent`` ("bring your own run agent"), a CI system's
#:   build agents, an HTTP user agent. So a bare "agent" is allowed, and
#:   only the process uses are flagged: sub-agents, another/other/coding/
#:   parallel agents, and an agent's worktree, instructions, session,
#:   transcript, brief or prompt.
PROCESS_WORDS = re.compile(
    r"\b(reviews?|reviewed|reviewers?|reviewing|judges?|judged|judging|findings?|gap[- ]lists?|"
    r"rounds?\s+\d+|predecessors?|coordinators?|coordinating\s+\w+)\b"
    r"|\b(sub-?agents?|(?:another|other|coding|ai|parallel|several|multiple)\s+agents?"
    r"|agents?(?:'s)?\s+(?:worktrees?|instructions?|instruction\s+files?|sessions?|transcripts?|briefs?|prompts?))\b",
    re.I,
)


#: A work item's label at the start of a prose line (after a comment marker,
#: quotes, a list dash): one to three capitals and one or two digits —
#: optionally ``-<n>`` and a letter — followed by a colon, or in
#: parentheses, and then words (``H4: …``, ``(M2) …``). The hyphenated form
#: anywhere in prose is already a ``ticket-key``.
FINDING_ID = re.compile(
    r"(?m)^[ \t#/*>\"'`\-]*(?:\(([A-Z]{1,3}\d{1,2}(?:-\d{1,2})?[a-z]?)\)|([A-Z]{1,3}\d{1,2}(?:-\d{1,2})?[a-z]?):)"
    r"[ \t]+[A-Za-z(`]"
)


def check_finding_ids(name: str, text: str) -> list[Violation]:
    """Work-item labels in ``text`` — the prose view of a file, or a commit
    message."""
    return [
        Violation(name, line_of(text, match.start()), "finding-id",
                  next(group for group in match.groups() if group))
        for match in FINDING_ID.finditer(text)
    ]


def check_process_words(name: str, text: str) -> list[Violation]:
    return [
        Violation(name, line_of(text, match.start()), "process-word", match.group(0))
        for match in PROCESS_WORDS.finditer(text)
    ]


# ---------------------------------------------------------------------------
# Running the rules
# ---------------------------------------------------------------------------

RULES = ("host", "path", "ticket-key", "tag", "prose-token", "finding-id", "lowercase-token")


def scan_file(name: str, text: str, root: Path, vocab: Vocabulary, facts: CodeFacts) -> dict[str, list[Violation]]:
    found: dict[str, list[Violation]] = {
        "host": check_hosts(name, text, facts, vocab),
        "path": check_paths(name, text, vocab, facts),
        "ticket-key": [],
        "tag": [],
        "prose-token": [],
        "finding-id": [],
        "lowercase-token": [],
    }
    if not is_generated(name):
        found["ticket-key"] = check_ticket_keys(name, text)
        found["tag"] = check_tags(name, text, vocab, facts, root)
        found["prose-token"] = check_prose(name, text, vocab, facts)
        if name != RULES_MODULE_NAME:  # this module spells the labels it describes
            found["finding-id"] = check_finding_ids(name, prose_view(name, text))
        found["lowercase-token"] = check_lowercase(name, text, vocab, facts)
    return found


@functools.cache
def scan_tree(root: Path, vocabulary_root: Path | None = None) -> dict[str, tuple[Violation, ...]]:
    """Every violation of every rule over the tree at ``root``. The vocabulary
    and the code facts come from ``vocabulary_root`` (default: ``root``) —
    a planted tree in a self-test is checked against this repository's words."""
    words_root = vocabulary_root or root
    vocab, facts = vocabulary(words_root), code_facts(words_root)
    results: dict[str, list[Violation]] = {rule: [] for rule in RULES}
    for name, text in text_files(root):
        for rule, violations in scan_file(name, text, root, vocab, facts).items():
            results[rule].extend(violations)
    return {rule: tuple(sorted(set(violations))) for rule, violations in results.items()}


def scan_message(label: str, message: str, root: Path = REPO) -> list[Violation]:
    """The rules a commit message is held to: hosts, paths, ticket keys,
    tags written in its text, prose tokens, process words and work-item
    labels."""
    vocab, facts = vocabulary(root), code_facts(root)
    name = f"{label}.md"  # a commit message is Markdown-ish prose
    violations = [
        *check_hosts(name, message, facts, vocab),
        *check_paths(name, message, vocab, facts),
        *check_ticket_keys(name, message),
        *(
            Violation(name, line_of(message, offset), "tag", tag)
            for tag, offset in text_tags(name, message)
            if tag_is_allowed(tag, vocab, facts) is False
        ),
        *check_prose(name, message, vocab, facts),
        *check_process_words(name, message),
        *check_finding_ids(name, message),
    ]
    return [Violation(label, violation.line, violation.rule, violation.token) for violation in violations]
