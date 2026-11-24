# Contributing to runcomposer

Thanks for looking. Some honest framing first: this is a young project — 0.2.0,
one maintainer, no company behind it. Issues and pull requests are genuinely
welcome, but replies come in evenings and weekends, and a large PR that arrives
unannounced may sit for a while or be declined on scope grounds. **Open an issue
before writing anything substantial** and you will save yourself that risk.

## Getting set up

```bash
git clone https://github.com/StochasticEntropy/runcomposer
cd runcomposer

pip install -e ".[dev]"          # Python 3.10 – 3.13
pytest                           # a few thousand tests, green in a couple of minutes
ruff check src tests ci          # lint (CI also lints examples/connector-template)
mypy                             # type check (strict, configured in pyproject.toml)

pip install -e ".[dev,robot]"    # adds Robot Framework: the robot plugin tests
                                 # skip cleanly without it, so run both
```

All three — tests, `ruff`, `mypy` — are expected to be clean on every change.

An editable install copies the one-file consumer, `src/runcomposer_exec.py`,
into the environment instead of linking it: after editing that file, install
again before using the `runcomposer-exec` command (the tests load the file
from `src/` and are not affected).

To *use* runcomposer rather than work on it, `pip install runcomposer` is
enough — the clone above is for development.

For the web UI (TypeScript, React, Vite — `ui/`):

```bash
cd ui && npm ci
npm run dev                      # dev server, proxies the API on :8100
npm test                         # unit and component tests (vitest), under a second
npm run lint                     # eslint + tsc --noEmit
npm run build                    # rebuilds src/runcomposer/ui_dist
npm run e2e:install              # once: the browser the end-to-end tests drive
npm run e2e                      # rebuilds, then drives the built UI in a browser
```

The end-to-end tests start real servers with stand-in runners from the test
suite, so they need the development install (`pip install -e ".[dev,robot]"`),
not the package alone.

The end-to-end tests start a real `runcomposer serve` per test, on a free port
in a temp directory, with the Python they find in `.venv/` (or the one named by
`RUNCOMPOSER_PYTHON`); the robot extra must be installed. They wait on
conditions and are run without retries — a test that needs a second attempt is
a flaky test.

**If you touch `ui/`, commit the rebuilt `src/runcomposer/ui_dist` in the same
change.** CI runs `git diff --exit-code src/runcomposer/ui_dist` against a fresh
build and fails if the committed bundle does not match the source. The bundle is
committed on purpose: evaluators get the UI from the wheel without a Node
toolchain.

## The rules that actually get PRs rejected

These are not style preferences — they are the boundaries the project exists to
hold. [DESIGN.md](DESIGN.md) is the source of truth for all of them.

1. **The core knows no framework vocabulary.** No "suite", "longname",
   "output.xml", "nodeid", "pytest", "Jenkins" in core code. A guard test fails
   the build if it appears. Framework knowledge lives in a plugin, always
   (§5, §6.1).
2. **Native names resolve through the source that owns the id space.** The core
   compares ids for equality and does nothing else with them — no parsing, no
   splitting, no normalising. If you find yourself writing a regex over an item
   id in core code, the design has been broken (§2).
3. **Core run-spec sections are generic and closed; only `runner.*` is open.**
   Adding a field to a core section is a spec-version decision, not an
   implementation detail (§3).
4. **Every capability claim ships with a runnable example or a test.** Not an
   assertion in a README. If the docs say it works, something in `tests/` or
   `examples/` proves it.
5. **Plugins import only `runcomposer.sdk`.** The reference connectors under
   `src/runcomposer/plugins/` use exactly the surface a connector outside this
   repository has; a guard test fails the build when one reaches past it. If a
   plugin needs something the SDK lacks, the SDK grows — deliberately, with its
   frozen name list in `tests/test_sdk.py` updated in the same change.
6. **Tests wait on conditions, never on the clock.** No `time.sleep` in a test:
   use `wait_until`, `Service.wait`, an event. A sleep is a failure waiting for
   a slow machine.
7. **A `[planned]` clause in DESIGN.md has no implementation.** If you implement
   one, delete its marker in the same change. If you add an unbuilt idea, mark it.
8. **Only the example world appears in the repository.** Code, tests, docs,
   examples and commit messages speak of the invented web shop and nothing
   else: hosts under `shop.example`, item ids like
   `Shop.Checkout.Cards.Pay By Card`, the demo corpus's tags, tickets
   `SHOP-12xx`. A guard test (`tests/test_example_world.py`) reads every text
   file of the tree — prose included — and fails on a host, an absolute
   path, a ticket key, a tag, or a word it does not know: a capitalised,
   CamelCase or ALL-CAPS word of prose (Markdown, HTML text, comments,
   docstrings, the text of a JSON string, the UI's messages included); a
   lower-case word of prose, of a JSON string, of any YAML key or value, of
   a string in the package (`src/`), of a shell script's lines; a
   hyphenated lower-case name (`shop-nightly`) in any string or URL; the
   words of a class name. Not read word by word: one lower-case word inside
   a string of TypeScript, of a test module or of TOML (hyphenated names
   there are), inside a Markdown code span or code block, in a Docker build file, a CSV
   or a `.robot` file, as a JSON key or as a JSON value ending in digits
   (hosts, paths and ticket keys are caught in all of these), and code
   identifiers, which are the code's own. A new word is added to `tests/example_world_words.txt`,
   under the heading it belongs to (a lower-case one to
   `tests/example_world_lowercase.txt`), in the same change — an English word, a technical term or a
   name of the example world; never the name of a person, or of a company,
   product or system from outside this project. `python
   ci/check_commit_messages.py [RANGE]` (default `origin/main..HEAD`) holds
   the messages of your commits to the same rules; it exits `1` naming each
   violation, `2` for a range it cannot read — and `2`, asking for a range,
   when no `origin/main` or `main` exists to default to.

## Pull requests

- Branch off `main`, keep the change focused, and say in the description *what
  problem it solves* rather than what files it touches.
- Tests pass on 3.10 through 3.13 on Linux, and on macOS and Windows —
  CI runs the whole matrix (Windows also with the robot extra; none of its
  jobs continues on error — which checks a merge must wait for is a
  setting of the repository, not of the workflow), plus the pipx
  quickstart, the export round trip, the Docker build, and the UI bundle check.
  It also runs the whole suite inside the shipped image (`shipped-image`),
  installs the wheel without its dependencies to check that an executing
  machine needs nothing else (the `wheel` job), and holds the messages of
  the commits your change adds to rule 8 (`commit-messages`).
- New behaviour comes with a test. Bug fixes come with the test that would have
  caught it.
- Update `CHANGELOG.md` under an `## Unreleased` heading.
- Docs that describe behaviour (`README.md`, `ADOPTING.md`, `DESIGN.md`, the
  homepage) get updated in the same PR as the behaviour.

The homepage is two hand-written pages, `docs/index.html` and its German
counterpart `docs/index.de.html`. A change to one goes into the other in the
same PR, and every statement, command and sample on them is held to the same
rule as the other documents: it is true of the version on `main`.

## Good first contributions

- A `TestSource` for a framework that is not Robot or pytest — the manifest
  source shows the zero-dependency shape, and this is the cleanest way to prove
  the boundary holds.
- A `ResultParser` for a format `junit-xml` does not cover. Read the defusing
  obligation in [SECURITY.md](SECURITY.md) first; it is not optional.
- Better failure messages. Anywhere runcomposer says something unhelpful when
  your corpus, config, or bundle is shaped unexpectedly is worth an issue.
- Translations. The UI ships EN and DE; the locale files are plain JSON.

## Reporting things

- **Bugs and features:** [open an issue](https://github.com/StochasticEntropy/runcomposer/issues/new/choose)
  — there are templates.
- **Security:** do not use the issue tracker. See [SECURITY.md](SECURITY.md).
- Please scrub internal hostnames, test names, and ticket ids out of anything you
  paste. A reduced repro on the demo corpus (`runcomposer demo`) is worth more
  than a redacted dump of yours.

## Conduct

There is no separate code of conduct document, because a one-maintainer project
cannot honestly promise an enforcement process. The expectation is simply this:
be straightforward and civil, assume the other person is trying to help, and
accept that "no, that is out of scope" is a normal answer. Behaviour that makes
the tracker unpleasant gets the thread locked and the account blocked, without a
committee.

## Licence

By contributing you agree your contributions are licensed under the
[MIT licence](LICENSE), same as the rest of the project.
