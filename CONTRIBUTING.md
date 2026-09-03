# Contributing to runcomposer

Thanks for looking. Some honest framing first: this is a young project — 0.1.0,
one maintainer, no company behind it. Issues and pull requests are genuinely
welcome, but replies come in evenings and weekends, and a large PR that arrives
unannounced may sit for a while or be declined on scope grounds. **Open an issue
before writing anything substantial** and you will save yourself that risk.

## Getting set up

```bash
git clone https://github.com/StochasticEntropy/runcomposer
cd runcomposer

pip install -e ".[dev]"          # Python 3.10 – 3.13
pytest                           # ~290 tests, should be green in under a minute

pip install -e ".[dev,robot]"    # adds Robot Framework: the robot plugin tests
                                 # skip cleanly without it, so run both
```

To *use* runcomposer rather than work on it, `pip install runcomposer` is
enough — the clone above is for development.

For the web UI:

```bash
cd ui && npm ci && npm run dev   # dev server, proxies the API on :8100
npm run build                    # rebuilds src/runcomposer/ui_dist
```

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
5. **A `[planned]` clause in DESIGN.md has no implementation.** If you implement
   one, delete its marker in the same change. If you add an unbuilt idea, mark it.

## Pull requests

- Branch off `main`, keep the change focused, and say in the description *what
  problem it solves* rather than what files it touches.
- Tests pass on 3.10 through 3.13 — CI runs the whole matrix, plus the pipx
  quickstart, the export round trip, the Docker build, and the UI bundle check.
- New behaviour comes with a test. Bug fixes come with the test that would have
  caught it.
- Update `CHANGELOG.md` under an `## Unreleased` heading.
- Docs that describe behaviour (`README.md`, `ADOPTING.md`, `DESIGN.md`, the
  homepage) get updated in the same PR as the behaviour.

The homepage at `docs/index.html` is **generated** — do not hand-edit it. If you
need to change it, say so in the issue and the maintainer will regenerate it from
the authoring source.

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
