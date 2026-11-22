"""The example world is the only world (DESIGN.md §12).

`test_self_containment.py` lists tokens the *core* must not contain, which
only works for vocabulary this project already knows about. It cannot catch
vocabulary from outside the example world — a deny-list would have to name
it. So these guards are built from *structure* and from allow-lists: wherever
the repository spells something host-shaped, path-shaped, ticket-shaped,
tag-shaped or name-shaped, it must be of the example world, of this
project's own infrastructure, or of a reviewed vocabulary. Anything else
fails without ever being named here.

The rules, and the structure each one relies on, are documented in
``tests/example_world_rules.py`` (shared with ``ci/check_commit_messages.py``,
which holds commit messages to the same rules). This module

* runs every rule over every text file of the repository — tracked and
  not-yet-tracked files of a git checkout, or a directory walk where there
  is no checkout (an sdist, a container) — and fails with ``file:line``,
  the offending token, the rule and what to do;
* proves every rule with a planted violation in a temporary tree, and with
  an allowed look-alike that must pass.

The planted violations are assembled at run time from innocuous synthetic
pieces (``"Zzz-" + "Unknown"``), so this file never spells the thing it shows
being caught and needs no exemption of its own.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from . import example_world_rules as rules
from .example_world_rules import REPO, Violation

# -- the real tree -----------------------------------------------------------------------


def violations_of(rule: str) -> tuple[Violation, ...]:
    return rules.scan_tree(REPO)[rule]


def fail_with(rule: str, what: str) -> None:
    found = violations_of(rule)
    assert not found, f"{what} — {len(found)} violation(s):\n{rules.report(found)}"


def test_every_host_is_of_the_example_world():
    fail_with("host", "hosts outside the example world and this project's infrastructure")


def test_every_absolute_path_is_generic():
    fail_with("path", "home-directory or machine-specific absolute paths")


def test_every_ticket_key_is_of_the_example_world():
    fail_with("ticket-key", "ticket-key-shaped tokens outside the example world")


def test_every_tag_is_of_the_example_world():
    fail_with("tag", "tags outside the example world's tag vocabulary")


def test_every_lower_case_word_is_reviewed():
    fail_with("lowercase-token", "lower-case words outside the reviewed vocabulary")


def test_every_reviewed_lower_case_word_is_still_used():
    """The lower-case list is the vocabulary, not a dictionary: a word
    nothing spells any more is removed."""
    import re

    spelled: set[str] = set()
    for name, text in rules.text_files(REPO):
        if name not in (rules.LOWERCASE_FILE_NAME, rules.RULES_MODULE_NAME) and not rules.is_generated(name):
            for word in re.findall(r"[^\W_]+", text):
                spelled.add(word.casefold())
                spelled.update(part.casefold() for part in rules._CAMEL_PARTS.findall(word))
    listed = rules.vocabulary(REPO).lowercase
    assert not sorted(listed - spelled), f"listed in {rules.LOWERCASE_FILE_NAME} but spelled nowhere"


def test_no_prose_carries_the_label_of_a_work_item():
    fail_with("finding-id", "sentences, docstrings or comments labelled like entries of a work list")


#: The prose vocabulary includes identifiers of the UI's code, and the
#: allow-lists below are checked against every file that uses them: both need
#: the whole tree. An sdist ships without the UI, the workflows and the media
#: captions, so there these tests skip and say why.
needs_full_tree = pytest.mark.skipif(
    not ((REPO / "ui" / "src").is_dir() and (REPO / ".github").is_dir()),
    reason="a partial tree (an sdist): the UI's identifiers and the files the allow-lists serve are not here",
)


@needs_full_tree
def test_every_prose_word_is_in_the_reviewed_vocabulary():
    fail_with(
        "prose-token",
        f"capitalised words in prose that are neither code identifiers nor in {rules.WORDS_FILE_NAME}",
    )


# -- the lists the rules allow are themselves kept honest ----------------------------------


@needs_full_tree
def test_every_project_host_is_used():
    """An allowed host nothing names any more is stale: remove it."""
    text = "\n".join(t for name, t in rules.text_files(REPO) if name != rules.RULES_MODULE_NAME)
    unused = [host for host in rules.PROJECT_HOSTS if host not in text]
    assert not unused, f"PROJECT_HOSTS entries nothing uses: {unused}"


@needs_full_tree
def test_every_generic_path_is_used():
    text = "\n".join(t for name, t in rules.text_files(REPO) if name != rules.RULES_MODULE_NAME)
    unused = [path for path in rules.GENERIC_PATHS if path not in text]
    assert not unused, f"GENERIC_PATHS entries nothing uses: {unused}"


@needs_full_tree
def test_every_reviewed_word_is_still_used():
    """The words file is the vocabulary, not a dictionary: a word nothing
    spells any more is removed, so the list stays a reviewed record of what
    the repository actually says."""
    import re

    spelled: set[str] = set()
    for name, text in rules.text_files(REPO):
        if name != rules.WORDS_FILE_NAME and not rules.is_generated(name):
            for word in re.findall(r"[^\W_]+(?:-[^\W_]+)*", text):
                spelled.add(word.casefold())
                spelled.update(part.casefold() for part in word.split("-"))
    vocab = rules.vocabulary(REPO)
    unused = sorted(
        word for words in vocab.sections.values() for word in words
        if word.casefold() not in spelled
    )
    assert not unused, f"{rules.WORDS_FILE_NAME} lists words nothing spells any more: {unused}"


def test_the_words_file_is_sectioned_and_free_of_duplicates():
    vocab = rules.vocabulary(REPO)
    assert rules.INVENTED_TAGS_HEADING in vocab.sections
    seen: dict[str, str] = {}
    duplicates = []
    for heading, words in vocab.sections.items():
        assert heading, f"{rules.WORDS_FILE_NAME}: words before the first '## heading'"
        for word in words:
            if word.casefold() in seen:
                duplicates.append((word, seen[word.casefold()], heading))
            seen[word.casefold()] = heading
    assert not duplicates, f"listed twice: {duplicates}"


def test_the_scan_reads_every_text_shape_of_the_tree():
    """The guard is vacuous if a change of layout hides files from it — so
    the shapes it must read are counted: every suffix the tree has, files
    without a suffix, the UI's tests at any depth."""
    names = rules.list_files(REPO)
    read = {name for name, _text in rules.text_files(REPO)}
    for name in names:  # every file that is text is read, whatever its suffix
        if Path(name).suffix.lower() not in rules.BINARY_SUFFIXES:
            try:
                (REPO / name).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            assert name in read, name
    assert not any(Path(name).suffix.lower() in rules.BINARY_SUFFIXES for name in read)
    assert "Dockerfile" in read and "LICENSE" in read
    for suffix in (".py", ".md", ".yaml", ".json", ".robot", ".csv", ".sql", ".sh", ".toml", ".html", ".css"):
        assert any(name.endswith(suffix) for name in read), f"no {suffix} file is read"
    if any(name.startswith("ui/") for name in names):  # the UI and the captions are not in an sdist
        assert any(name.endswith(".vtt") for name in read) and any(name.endswith(".tsx") for name in read)
        tests = set(rules.ui_test_files(REPO))
        assert {"ui/src/lib/filter.test.ts", "ui/e2e/compose.spec.ts", "ui/e2e/run.spec.ts"} <= tests


# -- self-tests: a planted violation is caught, an allowed look-alike is not ---------------
#
# Every planted string is assembled from pieces, so that this file does not
# itself contain what it shows the guard catching.

FOREIGN_HOST = "intranet." + "corp-internal" + ".zz"
HOME_PATH = "/Us" + "ers/someone/project/x"
LINUX_HOME_PATH = "/ho" + "me/someone/x"
MOUNT_PATH = "/Vol" + "umes/Shared/x"
WINDOWS_PATH = "C:" + "\\Users\\someone\\x"
UNC_PATH = "\\\\" + "fileserver\\share"
FOREIGN_TICKET = "QWX" + "-991"
UNKNOWN_TAG = "Zzz-" + "Unknown"
UNKNOWN_WORD = "Qwertz" + "uiop"
UNKNOWN_CAMEL = "Qwertz" + "Uiop"
TOOL = "runc" + "omposer"
#: A host package metadata names: allowed in a lock file, not elsewhere.
REGISTRY = next(iter(rules.DEPENDENCY_HOSTS))
TAGS = "[Ta" + "gs]"


def plant(tmp_path: Path, files: dict[str, str]) -> dict[str, tuple[Violation, ...]]:
    """Write ``files`` into a fresh tree (not a git checkout: the directory
    walk lists it) and scan it with this repository's vocabulary."""
    root = tmp_path / "tree"
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return rules.scan_tree(root.resolve(), REPO)


def tokens(found: dict[str, tuple[Violation, ...]], rule: str) -> set[tuple[str, int, str]]:
    return {(violation.path, violation.line, violation.token) for violation in found[rule]}


def test_hosts_without_a_scheme_are_caught_everywhere(tmp_path):
    found = plant(tmp_path, {
        "notes.md": f"Reach it at {FOREIGN_HOST}:8443 from the office.\n",
        "deploy/config.yaml": f"url: https://{FOREIGN_HOST}/job/x\nbare: {FOREIGN_HOST}\n",
        "tool.py": f'ENDPOINT = "{FOREIGN_HOST}"  # see also builds.{FOREIGN_HOST}\n',
        "run.sh": f"curl -s {FOREIGN_HOST}/health\n",
        "mail.txt": f"Write to ops@{FOREIGN_HOST}.\n",
        "web.ts": f'const base = "{FOREIGN_HOST}";\n',
    })
    assert tokens(found, "host") >= {
        ("notes.md", 1, FOREIGN_HOST),
        ("deploy/config.yaml", 1, FOREIGN_HOST),
        ("deploy/config.yaml", 2, FOREIGN_HOST),
        ("tool.py", 1, FOREIGN_HOST),
        ("tool.py", 1, "builds." + FOREIGN_HOST),
        ("run.sh", 1, FOREIGN_HOST),
        ("mail.txt", 1, FOREIGN_HOST),
        ("web.ts", 1, FOREIGN_HOST),
    }


def test_host_look_alikes_are_not_hosts(tmp_path):
    found = plant(tmp_path, {
        "notes.md": (
            "Point it at ci.shop.example:8443 or https://shop.example/ or localhost:8100.\n"
            "Edit config.yaml and output.xml; import runcomposer.sdk.robot; version 1.2.3.\n"
            "The item Shop.Checkout.Cards.T001 and `item.id` are no hosts; nor is 127.0.0.1.\n"
            "Docs live at https://github.com/StochasticEntropy/runcomposer and pypi.org.\n"
        ),
        "tool.py": "value = self.store.get(run.id)  # run.id is code\n",
        "ci.yml": "runs-on: ${{ matrix.os }}\n",
    })
    assert found["host"] == ()


def test_a_github_url_must_be_the_projects_own(tmp_path):
    found = plant(tmp_path, {"README.md": "Fork of https://github.com/" + "someone-else/tool.\n"})
    assert tokens(found, "host") == {("README.md", 1, "github.com/someone-else")}


def test_private_addresses_are_caught_documentation_addresses_are_not(tmp_path):
    private = "10.20." + "30.40"
    found = plant(tmp_path, {"hosts.txt": f"good 192.0.2.10 and 127.0.0.1\nbad {private}\n"})
    assert tokens(found, "host") == {("hosts.txt", 2, private)}


def test_generated_files_are_read_for_hosts_and_paths_only(tmp_path):
    """A bundle is minified code: bare dotted names there are identifiers.
    What it says behind a scheme, and the paths it carries, still count."""
    bundle = "src/runcomposer/ui_dist/assets/index.js"
    found = plant(tmp_path, {
        bundle: f'a.name=e.run;fetch("https://{FOREIGN_HOST}/x");p="{HOME_PATH}";t="{UNKNOWN_TAG}"\n',
        "ui/package-lock.json": f'{{"resolved": "https://{REGISTRY}/x/-/x-1.0.0.tgz"}}\n',
    })
    assert tokens(found, "host") == {(bundle, 1, FOREIGN_HOST)}
    assert tokens(found, "path") == {(bundle, 1, HOME_PATH)}
    assert found["tag"] == () and found["prose-token"] == ()


def test_home_and_machine_paths_are_caught(tmp_path):
    found = plant(tmp_path, {
        "notes.md": f"Run it from {HOME_PATH} or {LINUX_HOME_PATH}.\n",
        "job.groovy": f'sh "cp {MOUNT_PATH} ."\n',
        "win.ps1": f"Copy-Item {WINDOWS_PATH}\nnet use {UNC_PATH}\n",
    })
    assert tokens(found, "path") == {
        ("notes.md", 1, HOME_PATH),
        ("notes.md", 1, LINUX_HOME_PATH),
        ("job.groovy", 1, MOUNT_PATH),
        ("win.ps1", 1, WINDOWS_PATH),
        ("win.ps1", 2, UNC_PATH),
    }


def test_generic_paths_routes_and_placeholders_are_not_caught(tmp_path):
    found = plant(tmp_path, {
        "notes.md": (
            "Scratch files go to /tmp/x, the shop lives in /srv/shop, the image works in /data.\n"
            "Your checkout: /ho" "me/<user>/src or $HOME/src; the API: /api/runs/{run_id}.\n"
        ),
    })
    assert found["path"] == ()


def test_ticket_keys_must_be_of_the_example_world(tmp_path):
    found = plant(tmp_path, {"CHANGES.md": f"Fixes {FOREIGN_TICKET}; see SHOP-1203, UTF-8 and ISO-8601.\n"})
    assert tokens(found, "ticket-key") == {("CHANGES.md", 1, FOREIGN_TICKET)}


def test_tags_are_caught_in_every_shape(tmp_path):
    files = {
        "suite.robot": (
            "*** Settings ***\n"
            f"Test Tags    Cart\n"
            "\n*** Test Cases ***\nAdd A Thing\n"
            f"    {TAGS}    Cart    Smoke\n"
            f"    ...    {UNKNOWN_TAG}\n"
        ),
        "manifest.json": json.dumps({"items": [{"id": "a", "tags": ["Payments", UNKNOWN_TAG]}]}) + "\n",
        "spec.yaml": f"selection:\n  tag_filter:\n    op: AND\n    items: [Payments, {{not: {UNKNOWN_TAG}}}]\n",
        "test_x.py": (
            f"Item(id='a', tags=('Cart', '{UNKNOWN_TAG}'))\nTAGS = frozenset({{'Smoke', '{UNKNOWN_TAG}'}})\n"
            f"tags = []\ntags.append('{UNKNOWN_TAG}')\n"
        ),
        "history.csv": f"item,tags,duration_ms\nShop.Cart.T001,Cart;{UNKNOWN_TAG},10\n",
        "guide.md": (
            f"`Cart AND NOT {UNKNOWN_TAG}` selects little.\n"
            f"Pick by prefix:{UNKNOWN_TAG} or regex:^{UNKNOWN_TAG}$.\n"
            "```console\n"
            f"$ {TOOL} compile '{UNKNOWN_TAG}' --config x.yaml\n"
            f"$ {TOOL} run --runner demo {UNKNOWN_TAG}\n"
            "```\n"
        ),
        "ui/e2e/pick.spec.ts": f'await page.getByRole("option", {{ name: "{UNKNOWN_TAG}" }}).click();\n',
    }
    found = tokens(plant(tmp_path, files), "tag")
    assert found >= {
        ("suite.robot", 7, UNKNOWN_TAG),
        ("manifest.json", 1, UNKNOWN_TAG),
        ("spec.yaml", 4, UNKNOWN_TAG),
        ("test_x.py", 1, UNKNOWN_TAG),
        ("test_x.py", 2, UNKNOWN_TAG),
        ("test_x.py", 4, UNKNOWN_TAG),
        ("history.csv", 2, UNKNOWN_TAG),
        ("guide.md", 1, UNKNOWN_TAG),
        ("guide.md", 2, "prefix:" + UNKNOWN_TAG),
        ("guide.md", 2, f"regex:^{UNKNOWN_TAG}$"),
        ("guide.md", 4, UNKNOWN_TAG),
        ("guide.md", 5, UNKNOWN_TAG),
        ("ui/e2e/pick.spec.ts", 1, UNKNOWN_TAG),
    }, found
    assert {token for _path, _line, token in found} <= {
        UNKNOWN_TAG, "prefix:" + UNKNOWN_TAG, f"regex:^{UNKNOWN_TAG}$"
    }


def test_tags_of_the_vocabulary_and_derived_forms_pass(tmp_path):
    found = plant(tmp_path, {
        "suite.robot": f"*** Test Cases ***\nPay\n    {TAGS}    Payments-Cards    Sprint-15    RanOn-eu    SHOP-1207\n",
        "guide.md": "`Payments AND NOT prefix:Quarantine-` and regex:^Cart(V2)?$ select a lot.\n",
        "test_y.py": "Item(id='a', tags=['Checkout-20', 'smoke'])\nrouter = APIRouter(tags=['runs'])\n",
    })
    assert found["tag"] == ()


def test_the_expression_scanner_reads_operands_not_prose():
    """Operands of an upper-case AND/OR/NOT are tags; an English sentence
    with a capitalised word near "OR" is not an expression."""
    operands = [operand for operand, _offset in rules.expression_operands("`A AND B OR NOT (Cart)`")]
    assert {"A", "B", "Cart"} <= set(operands)
    assert rules.expression_operands("this and that, or nothing") == []
    assert rules.expression_operands("An OR of negations is prose") == []


def test_the_tag_vocabulary_has_one_source_of_truth():
    """The demo corpus, the shipped examples' manifests and suites, and the
    reviewed invented tags — nothing else."""
    vocab = rules.vocabulary(REPO)
    corpus = {tag.casefold() for tag in rules.corpus_tags(REPO)}
    examples = {tag.casefold() for tag in rules.example_tags(REPO)}
    invented = {tag.casefold() for tag in vocab.sections[rules.INVENTED_TAGS_HEADING]}
    assert corpus <= vocab.tags and examples <= vocab.tags
    assert vocab.tags == corpus | examples | invented


def test_an_example_cannot_bring_in_a_tag_of_its_own(tmp_path):
    """The shipped examples' tags join the vocabulary only when they are made
    of words it already has — so a word nobody reviewed fails right where an
    example spells it, instead of becoming vocabulary by being there."""
    root = tmp_path / "tree"
    for name in ("src/runcomposer/demo/corpus.json", "src/runcomposer/demo/taxonomy.yaml", rules.WORDS_FILE_NAME):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / name, root / name)
    (root / "examples").mkdir()
    (root / "examples/manifest.yaml").write_text(
        f"items:\n  - id: a\n    tags: [Checkout-Express, {UNKNOWN_TAG}]\n", encoding="utf-8"
    )
    vocab = rules.vocabulary(root.resolve())
    assert "checkout-express" in vocab.tags and UNKNOWN_TAG.casefold() not in vocab.tags
    assert tokens(rules.scan_tree(root.resolve()), "tag") == {("examples/manifest.yaml", 3, UNKNOWN_TAG)}


def test_unknown_prose_words_are_caught(tmp_path):
    found = plant(tmp_path, {
        "notes.md": f"We ship it with {UNKNOWN_WORD} every night.\n{UNKNOWN_CAMEL} starts the day.\n",
        "tool.py": f'"""Talks to {UNKNOWN_WORD} over HTTP."""\n# Ask {UNKNOWN_WORD} first.\n',
        "talk.vtt": f"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nThen {UNKNOWN_WORD} answers.\n",
        "ui/e2e/x.spec.ts": f'test("hands the run to {UNKNOWN_WORD}", async () => {{}});\n',
    })
    assert tokens(found, "prose-token") == {
        ("notes.md", 1, UNKNOWN_WORD),
        ("notes.md", 2, UNKNOWN_CAMEL),
        ("tool.py", 1, UNKNOWN_WORD),
        ("tool.py", 2, UNKNOWN_WORD),
        ("talk.vtt", 4, UNKNOWN_WORD),
        ("ui/e2e/x.spec.ts", 1, UNKNOWN_WORD),
    }


def test_known_prose_words_pass(tmp_path):
    """A sentence-initial ordinary word, a word of the reviewed list, a code
    identifier and a word of the example world all pass; code and URLs are
    not prose."""
    found = plant(tmp_path, {
        "notes.md": (
            "Every night Jenkins runs the Checkout suite through RunnerBase.\n"
            f"Inline `{UNKNOWN_WORD}` code is code, and so is https://shop.example/{UNKNOWN_WORD}.\n"
            "```python\n"
            f"{UNKNOWN_WORD} = 1\n"
            "```\n"
        ),
        "tool.py": f"{UNKNOWN_WORD.lower()} = '{UNKNOWN_WORD}'  # a lower-case comment\n",
    })
    assert found["prose-token"] == ()


def test_a_prose_failure_says_which_file_to_edit():
    message = rules.report([Violation("notes.md", 1, "prose-token", UNKNOWN_WORD)])
    assert f"notes.md:1: [prose-token] '{UNKNOWN_WORD}'" in message
    assert rules.WORDS_FILE_NAME in message and "review" in message


def test_process_words_are_caught_in_commit_messages_with_their_carve_outs():
    word, other = "review" + "er", "another" + " agent"
    message = f"Address the {word} notes from round 2\n\nIt was judge" f"d so; {other} agreed.\n"
    found = rules.scan_message("abc1234", message)
    assert {(f.line, f.token) for f in found if f.rule == "process-word"} == {
        (1, word), (1, "round 2"), (3, "judged"), (3, other),
    }
    fine = rules.scan_message(
        "abc1234",
        "Send a user agent header; the run agent of examples/remote-agent reports back\n",
    )
    assert [f for f in fine if f.rule == "process-word"] == []


def test_work_item_labels_are_caught_in_prose_and_left_alone_in_code(tmp_path):
    label = "H" + "4"  # assembled: this file's own prose carries no label
    found = plant(tmp_path, {
        "tests/test_cart.py": (
            "def test_refusal():\n"
            f'    """{label}: the refusal used to reset the run."""\n'
            f"    # (M{2}) a comment that begins with one\n"
            f"    ROUTE = '{label}: not prose, a string'\n"
        ),
        "docs/notes.md": (
            f"- {label}: a list entry that begins with one\n"
            f"Timestamps are ISO-8601, text is UTF-8, and a sentence may name {label}: it does not begin with it.\n"
            f"`{label}: in inline code` is code.\n"
        ),
    })
    assert {(v.path, v.line, v.token) for v in found["finding-id"]} == {
        ("tests/test_cart.py", 2, label),
        ("tests/test_cart.py", 3, "M2"),
        ("docs/notes.md", 1, label),
    }
    # The hyphenated form is a ticket-key-shaped token anywhere.
    hyphenated = plant(tmp_path / "again", {"docs/notes.md": "Closes BL" + "3-1 of the list.\n"})
    assert [v.token for v in hyphenated["ticket-key"]] == ["BL" + "3-1"]


#: A lower-case word of nobody's language, assembled.
LOWER_FOREIGN = "vex" + "lund"


def test_a_foreign_lower_case_word_is_caught_wherever_it_hides(tmp_path):
    """Lower-case prose, a job name in YAML, Markdown and a Python string, a
    plain JSON value, an SQL table, a class name — each made of a word nobody
    reviewed — and the same shapes made of known words, left alone."""
    job = LOWER_FOREIGN + "-regress-nightly"
    found = plant(tmp_path, {
        "docs/notes.md": (
            f"the {LOWER_FOREIGN} team owns the nightly run\nThe job `x` runs {job} at night.\n"
            f"Its builds: https://ci.shop.example/job/{job}/42/ and https://ci.shop.example/job/shop-nightly/7/\n"
        ),
        "deploy/jobs.yaml": f"job: {job}\nname: shop-nightly\n",
        "tests/test_jobs.py": f'JOB = "{job}"\nOK = "shop-nightly"\n',
        "tests/fixtures/env.json": '{"tenant": "' + LOWER_FOREIGN + '", "title": "Checkout before the release"}\n',
        "tests/fixtures/x.sql": f"select * from {LOWER_FOREIGN}_runs where id = 1;\n",
        "src/runcomposer/extra.py": f"class {LOWER_FOREIGN.title()}Runner:\n    pass\n\n\nclass ShopRunner:\n    pass\n",
    })
    flagged = {(v.path, v.token) for v in found["lowercase-token"]}
    assert flagged == {
        ("docs/notes.md", LOWER_FOREIGN),
        ("docs/notes.md", job),
        ("deploy/jobs.yaml", job),
        ("tests/test_jobs.py", job),
        ("tests/fixtures/env.json", LOWER_FOREIGN),
        ("tests/fixtures/x.sql", LOWER_FOREIGN),
        ("src/runcomposer/extra.py", f"{LOWER_FOREIGN.title()}Runner"),
    }, flagged


def test_a_foreign_word_is_caught_in_a_message_catalog_a_one_word_value_and_a_shell_line(tmp_path):
    """A word with an umlaut, a CamelCase and an ALL-CAPS word inside a JSON
    message; one lower-case word as a YAML value, in a Python string of the
    package and in a shell line (a number at its end dropped) — and known
    words in the same shapes, left alone."""
    umlaut = "Zör" + "kelkasse"
    camel = LOWER_FOREIGN.title() + "Corp"
    caps = LOWER_FOREIGN.upper()
    found = plant(tmp_path, {
        "ui/src/locales/de.json": '{"a": "die ' + umlaut + ' ist da", "b": "die ' + umlaut.lower() + ' auch", "c": "Ausführung"}\n',
        "ui/src/locales/en.json": '{"a": "the ' + camel + ' is here", "b": "the ' + caps + ' is here"}\n',
        "ci/jobs.yaml": f"job: {LOWER_FOREIGN}\nname: nightly\n",
        "src/runcomposer/extra.py": f'HOST = "{LOWER_FOREIGN}"\nKIND = "nightly"\n',
        "ci/deploy.sh": f"ssh deploy@{LOWER_FOREIGN}01 run\necho nightly\n",
    })
    flagged = {(v.path, v.token) for v in found["lowercase-token"]} | {(v.path, v.token) for v in found["prose-token"]}
    assert flagged == {
        ("ui/src/locales/de.json", umlaut),
        ("ui/src/locales/de.json", umlaut.lower()),
        ("ui/src/locales/en.json", camel),
        ("ui/src/locales/en.json", caps),
        ("ci/jobs.yaml", LOWER_FOREIGN),
        ("src/runcomposer/extra.py", LOWER_FOREIGN),
        ("ci/deploy.sh", LOWER_FOREIGN),
    }, flagged


def test_a_tag_shaped_literal_of_a_python_test_is_held_to_the_tag_vocabulary(tmp_path):
    """In a parametrize list, a plain list, a dict key, a call argument."""
    unknown = "Blorp" + "tag"
    found = plant(tmp_path, {
        "tests/test_planted.py": (
            "import pytest\n"
            f'@pytest.mark.parametrize("tag", ["Checkout", "{unknown}"])\n'
            "def test_x(tag):\n"
            f'    WANT = {{"{unknown}": 1}}\n'
            f'    compose(service, "Payments-Cards")\n'
        ),
    })
    assert {(v.line, v.token) for v in found["tag"]} == {(2, unknown), (4, unknown)}


def test_a_path_under_a_root_nobody_reviewed_is_caught(tmp_path):
    """Exact reviewed paths, not whole roots: /opt/<anything> is somebody's
    layout unless it is one of the reviewed paths."""
    planted = "/o" + "pt/" + LOWER_FOREIGN + "/results"
    found = plant(tmp_path, {"docs/notes.md": f"It writes to {planted} and /opt/shop/keywords.\n"})
    assert [v.token for v in found["path"]] == [planted]


def test_work_item_labels_are_caught_in_commit_messages():
    label = "N" + "6"
    found = rules.scan_message("abc1234", f"{label}: name the cause of an interrupted dispatch\n")
    assert [(f.rule, f.token) for f in found if f.rule == "finding-id"] == [("finding-id", label)]


def test_a_commit_message_is_held_to_the_tree_rules():
    message = f"Point the job at {FOREIGN_HOST}, fix {FOREIGN_TICKET}, read {HOME_PATH}\n\nTalk to {UNKNOWN_WORD}.\n"
    found = {(f.rule, f.token) for f in rules.scan_message("abc1234", message)}
    assert found >= {
        ("host", FOREIGN_HOST), ("ticket-key", FOREIGN_TICKET), ("path", HOME_PATH),
        ("prose-token", UNKNOWN_WORD),
    }


def commit_check():
    """``ci/check_commit_messages.py``, loaded from its path (``ci`` is no package)."""
    import importlib.util

    script = REPO / "ci" / "check_commit_messages.py"
    if not script.is_file():
        pytest.skip("ci/ is not part of this tree")
    spec = importlib.util.spec_from_file_location("check_commit_messages", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_commit_check_reads_a_range_and_leaves_trailers_alone(tmp_path):
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    check = commit_check()
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=shop", "-c", "user.email=ci@shop.example", "-c", "commit.gpgsign=false", *args],
            cwd=repo, check=True, capture_output=True,
        )

    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "Add the cart page")
    git("commit", "-q", "--allow-empty", "-m", (
        f"Point the job at {FOREIGN_HOST}\n\nIt now talks to {UNKNOWN_WORD}.\n\n"
        f"Co-Authored-By: {UNKNOWN_CAMEL} <bot@{FOREIGN_HOST}>\n"
    ))
    found = {(f.line, f.rule, f.token) for f in check.check("HEAD~1..HEAD", repo)}
    assert found == {(1, "host", FOREIGN_HOST), (3, "prose-token", UNKNOWN_WORD)}
    assert check.main(["check", "HEAD~1..HEAD"], repo) == 1
    assert check.main(["check", "HEAD~1"], repo) == 0  # the first commit alone passes


def test_without_a_main_to_compare_with_the_commit_check_asks_for_a_range(tmp_path, capsys):
    """The whole history is never the default: exit 2, asking for a range."""
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    check = commit_check()
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "work"], ["commit", "-q", "--allow-empty", "-m", f"Talk to {FOREIGN_HOST}"]):
        subprocess.run(
            ["git", "-c", "user.name=shop", "-c", "user.email=ci@shop.example", "-c", "commit.gpgsign=false", *args],
            cwd=repo, check=True, capture_output=True,
        )
    assert check.main(["check"], repo) == 2
    assert "give the range of commits to check" in capsys.readouterr().err


def test_the_commit_check_skips_outside_a_checkout(tmp_path, capsys):
    check = commit_check()
    assert check.main(["check"], tmp_path) == 0
    assert "skipped — not a git checkout" in capsys.readouterr().out


def test_the_walk_outside_a_checkout_skips_caches_installs_and_ignored_files(tmp_path):
    root = tmp_path / "unpacked"
    files = {
        "src/a.py": "x\n", "node_modules/x/index.js": "x\n", ".venv/lib/y.py": "x\n",
        "pkg.egg-info/PKG-INFO": "x\n", "docs/b.vtt": "x\n", "Dockerfile": "x\n",
        ".gitignore": "# local state\n*.tmp\nstate/\nconfig.yaml\n!examples/config.yaml\n",
        "notes.tmp": "x\n", "examples/state/run.json": "x\n",
        "config.yaml": "x\n", "examples/config.yaml": "x\n", "ui/.gitignore": "out/\n", "ui/out/b.js": "x\n",
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    assert rules.list_files(root.resolve()) == (
        ".gitignore", "Dockerfile", "docs/b.vtt", "examples/config.yaml", "src/a.py", "ui/.gitignore",
    )


def test_the_guard_runs_without_a_git_checkout(tmp_path):
    """In an sdist or a container there is no ``.git``: the walk lists the
    same files the checkout tracks, and the scan works on them."""
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout: this run already is the walk")
    copy = tmp_path / "copy"
    for name in ("src/runcomposer/demo/corpus.json", "src/runcomposer/demo/taxonomy.yaml", rules.WORDS_FILE_NAME, "README.md"):
        (copy / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / name, copy / name)
    listed = rules.list_files(copy.resolve())
    assert "README.md" in listed and rules.WORDS_FILE_NAME in listed
    assert rules.scan_tree(copy.resolve(), REPO)["host"] == ()


def test_an_identifier_used_only_inside_an_f_string_is_known_on_every_python():
    """The words the code spells are part of the vocabulary. A name that
    appears only inside an f-string is a token of its own only from Python
    3.12 on, so the names are also read from the syntax tree — the guard's
    verdict must not depend on the interpreter that runs it."""
    parts = rules.python_parts('label = f"{re.escape(tag)}-{shop.region_code}"\n')
    assert parts is not None
    assert {"re", "escape", "tag", "shop", "region_code", "label"} <= parts.names
