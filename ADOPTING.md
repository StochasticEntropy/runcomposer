# Adopting runcomposer

How to connect runcomposer to *your* test corpus, *your* machines, and *your*
result formats — and how little you should have to build to do it.

This guide is written for the person building the connector. It assumes you
have read the [README](README.md) and skimmed [DESIGN.md](DESIGN.md); it does
not repeat them. Everything here is about the seam between the public tool and
your environment.

---

## The short version

Work down this list and stop at the first line that describes you. Most
adopters stop in the first half.

| Your situation | What you build |
|---|---|
| Robot Framework tests, executed on the runcomposer host | **Nothing.** Config: `robotframework` source + `robot-pool` runner. |
| Any framework, tests executed on the runcomposer host | A `--command` wrapper script. Config: `manifest` source + the export loop. |
| Tests executed on machines runcomposer cannot reach | **A shell script.** Copy `runcomposer_exec.py` there; return the bundle. See [§6](#6-when-your-tests-run-somewhere-runcomposer-cannot-reach). |
| Tests executed by an existing CI job | Config: `ci-trigger` + a job stage that runs `runcomposer-exec`. |
| The taxonomy tree should show *your* structure, not the demo's | **A YAML file, no code.** `core.taxonomy_file`; format and a worked example in [docs/taxonomy.md](docs/taxonomy.md). |
| Your test ids don't match the names in your result files | A `TestSource` — or, often, just `aliases` in a manifest. See [§3](#3-the-rule-that-matters-most-the-id-space). |
| A result format nobody has written a parser for | A `ResultParser`. ~60 lines. |
| Something genuinely exotic in how runs are dispatched | A `Runner`. |
| Postgres, or an existing results database | A `RunStore`. The largest of the four, and rarely needed. |

If you find yourself writing a lot of code, that is a signal something is
wrong with the fit — please open an issue describing what you had to do.

---

## 1. What you do not have to build

The point of adopting rather than rebuilding is that these already exist and
are not your problem: the tag filter language and its compiler, the selection
model, the run spec document and its schema, run lifecycle and completion
accounting, dispatch and delivery records, idempotent ingestion, the
quarantine inbox, retention and `gc`, the HTTP API, and the web UI in English
and German.

You are connecting *edges*: where tests come from, where they run, and what
their results look like.

---

## 2. Start with configuration, not code

runcomposer has exactly one config file and no environment-variable magic.
Plugin *selection* lives there too, so a surprising amount of adaptation is
configuration rather than Python:

```yaml
core:
  api: { host: 127.0.0.1, port: 8100 }
  taxonomy_file: taxonomy.yaml  # the curated tree in the UI's left panel
  artifact_dir: artifacts/
  ingestion:
    tokens: required          # the per-run ingest token; "disabled" on closed networks
    inbox: results_inbox/     # watched directory for returned bundles
    quarantine_dir: quarantine/
store:
  sqlite: { path: runcomposer.db }
sources:
  robotframework: { root: tests/ }
runners:
  robot-pool:
    suite_root: tests/
    max_workers: 8
    partitions: [env1, env2]        # fan the same selection across environments
    variables: { STAGE: test }      # passed into every execution
    pre_run_hooks:                  # run once per dispatch, before any chunk
      - "./prepare-environment.sh"
    listener: "MyListener.py"       # your own framework listener, alongside ours
    history_selector: { labels: { suite: nightly } }
```

The core validates only the `core` section. Everything under `sources`,
`runners`, and `store` is owned and validated by the plugin named by the key —
and an unknown plugin id **fails startup loudly** rather than surfacing later
as a broken request.

The taxonomy is the other thing you write instead of code: `taxonomy_file` is
a YAML tree of tag patterns that the UI renders on the left and clicks into
the filter builder. Nothing validates it — a file with the wrong shape is
parsed, served, and rendered as an empty panel with no error anywhere — so
write it against [docs/taxonomy.md](docs/taxonomy.md), which has the node
format, the one-pattern-per-leaf limitation, and a worked example.

**One rule before you write any pattern**, in a taxonomy leaf or a filter: a
bare literal is matched case-**insensitively**, but `regex:` is compiled
without flags and `prefix:X` is exactly `regex:^X` — so both of those are
case-**sensitive**. Rules carried over from a case-insensitive tool match
nothing, silently, until you write `regex:(?i)…`.

---

## 3. The rule that matters most: the id space

Everything else is detail. This is the part that will bite you if you get it
wrong.

> A **test source owns an id space.** Every native name that any result parser
> can emit must resolve, through that source's `resolve()`, to exactly one
> item id — or to `None`.

The core never parses, splits, normalises, or pattern-matches ids or native
names. It compares them for equality. That is deliberate: every framework has
naming quirks, and they all belong inside the one plugin that understands
them, not scattered through the orchestrator.

So when your result files spell a test differently from your catalog — and
they usually do — the mapping is *your source's* job. Two ways:

**Declare it in the manifest** (no code). Each item may carry `aliases`, and
collisions are refused at load time:

```json
{"items": [
  {"id": "tests/test_cart.py::test_checkout[visa]",
   "tags": ["Cart", "Payments"],
   "aliases": ["test_cart.test_checkout[visa]"]}
]}
```

**Or implement `resolve()`** in a custom source, where you can do whatever
normalisation your framework needs.

Either way, prove it. The one test worth writing before anything else:

```python
def test_id_space_invariant():
    source = MySource(...)
    parsed = MyParser().parse(a_real_results_file)
    resolved = [source.resolve(p.native_name) for p in parsed]
    assert all(r is not None for r in resolved)      # nothing unmatched
    assert len(set(resolved)) == len(resolved)       # nothing double-matched
```

Unresolvable names are not silently dropped into the void — ingestion warns
and skips them — but a run where half the verdicts vanished is a bad day.

---

## 4. The four ports

All four are `typing.Protocol`s in
[`runcomposer.core.ports`](src/runcomposer/core/ports.py). You do not subclass
anything; you write a class with the right methods. Read that file — it is
short, and it is the contract.

### `TestSource` — where tests come from

```python
provider_id: str
def items(self) -> list[Item]              # Item(id, tags, name?, hierarchy?, meta?)
def snapshot(self) -> str                  # "sha256:…" content hash of the catalog
def resolve(self, native_name) -> str | None
```

`snapshot()` is what makes drift detectable: it is recorded at compose time
and compared before execution. Hash the things that would change the meaning
of a run — the ids and their tags — not file mtimes.

Ships: `manifest` (JSON/YAML, only `id` + `tags` required), `robotframework`
(walks `.robot` files, ids are longnames).

### `Runner` — how a spec gets executed

```python
def describe(self) -> RunnerInfo                    # id + capability flags
def dispatch(self, spec: Mapping) -> DispatchHandle # takes the DOCUMENT only
```

`dispatch` receives the run spec, nothing else. Those two methods are the
whole required contract; everything below is optional, looked up on your
runner object by name and skipped when it is not there.

- **`bind(*, store, source, artifact_root)`** — called before `dispatch` if
  you want the store and the live catalog: live status, duration history, the
  drift check of [§7](#7-the-executor-contract). The source you are handed is
  a *fresh* one, so a drift check compares against the corpus as it is now,
  not as it was at compose time.
- **`deliveries`** — a list the service drains *after* `dispatch` returns.
  Each entry is a mapping `{"shard": "1", "verdicts": [Verdict, …]}` — `shard`
  defaults to `"1"`, `verdicts` holds `runcomposer.core.model.Verdict` objects
  — and each becomes one delivery under the dispatch, in the
  `runcomposer-verdicts` format. It is the short path for a runner that
  already holds its verdicts in memory; a runner that records deliveries into
  the store itself does not need it. Your runner is built fresh for every
  dispatch, so the list starts empty each time.
- **`last_plan`** — a string, read after `dispatch`. `runcomposer dispatch`
  prints it verbatim, so put in it whatever a human should see about the
  hand-off: `robot-pool` writes its chunk plan there (including the
  round-robin cold-start note), `ci-trigger` the job URL it triggered.
- **`bind_dispatch(reservation)`** — called before `dispatch` with a
  `DispatchReservation`: use its `dispatch_id` as your dispatch id, and call
  its `record(...)` at the moment you hand the work to your executor. Worth
  implementing when `dispatch` blocks while the work runs — without it the
  dispatch is recorded from the handle you return, so the hand-off is
  invisible for exactly as long as the run lasts. The contract, as always, is
  in [`core/ports.py`](src/runcomposer/core/ports.py).

Return a `DispatchHandle` with the number of shards you will deliver. Setting
`spec_sha256` to the hash of the exact bytes you handed out enables marker
verification when the results come back. Refuse a dispatch by raising
`DispatchRefused` with a message that says what to do about it.

**`dispatch` is called synchronously, and everything waits for it.** The
service calls it inline, so `POST /api/v1/runs` with `dispatch: {runner: …}`
returns only when your `dispatch` does — and `robot-pool` runs the entire
suite in there, which means the HTTP request stays open for the whole
execution: two seconds for a two-second suite, forty minutes for a forty-minute
nightly. Plan for that. If your executor is long-running, return the handle as
soon as the work has *started* and let the results come back through
ingestion, the way `ci-trigger` does with `completion: callback` — that is
also what makes a run observable in `AWAITING_RESULTS` at all, since a runner
that delivers inline is already `COMPLETE` by the time it returns.
Live progress during a blocking dispatch is still possible, but only for other
readers: take `bind`, stream verdicts into the store, and a concurrent
`GET /runs/{id}` shows `RUNNING` while the dispatching call is still open.

Ships: `robot-pool` (in-process pool, partitions, duration-balanced chunking,
live verdicts), `ci-trigger` (parameterized CI job, webhook or polling
completion), `demo`.

### `ResultParser` — what results look like

```python
format_id: str
def parse(self, path) -> list[ParsedVerdict]   # ParsedVerdict(native_name, status, …)
```

Emit the native name **exactly as the artifact spells it**. Do not try to
reconstruct catalog ids here — that is the source's job (see §3), and doing it
here is the one design mistake that reliably makes a corpus unmaintainable.

`status` is one of `PASS`, `FAIL`, `SKIP`, `ERROR`. If your format is XML,
parse defensively: refuse documents carrying DOCTYPE or ENTITY declarations
(both shipped XML parsers do this, and there are tests that prove it).

Ships: `runcomposer-verdicts` (a trivial JSON format any executor can emit),
`robot-output-xml`, `junit-xml`.

### `RunStore` — where runs live

The widest protocol, and the one you almost certainly do not need. `sqlite`
ships and is zero-setup. Implement your own only if you must live in an
existing database. The persisted schema is normative — runs, specs,
dispatches, deliveries, verdicts, artifact_refs — and deliberately contains no
runner-lifecycle fields: pool state and runner health are ephemeral runner
memory, never storage.

The idempotency contract lives here and is not optional: a byte-identical
bundle is a no-op, a new bundle for the same `(run, dispatch, shard)` replaces
that shard's verdicts, and there is no monotonic merge — a correction must be
able to turn a `FAIL` back into a `PASS`.

`add_dispatch` is a *declaration*, not an append: called again with a
`dispatch_id` you already hold, it updates that same row and keeps its
original `created_at`. A dispatch is recorded when the hand-off happens — when
the shard count may still be provisional — and refined from the handle the
runner returns.

---

## 5. Wiring your plugins in

Two first-class mechanisms, both explicit. No scanning, no env vars.

**Entry points**, if your connector is a distributed package:

```toml
[project.entry-points."runcomposer.sources"]
my-source = "mypkg.sources:MySource"

[project.entry-points."runcomposer.runners"]
my-runner = "mypkg.runners:MyRunner"
```

Groups: `runcomposer.sources`, `runcomposer.runners`, `runcomposer.parsers`,
`runcomposer.stores`.

**Or an import path in the config**, which needs no packaging at all — the
hack-it-in-an-afternoon path:

```yaml
sources:
  my-source:
    module: "mypkg.sources:MySource"
    root: /some/where          # every other key is passed to your __init__
```

---

## 6. When your tests run somewhere runcomposer cannot reach

This is the common case in real organisations, and it needs no plugin at all.

runcomposer composes the plan; something else executes it; a bundle comes
back. runcomposer never connects to your test machines, holds no credentials
for them, and does not care how the files travel.

```bash
# 1 — here: compose and freeze the plan, minting an export dispatch
runcomposer spec 'Regression' --title "Nightly" \
    --format json -o spec.json --export

# 2 — there: ONE vendored stdlib-only file. No install, no venv, no network.
python3 runcomposer_exec.py spec.json --out results \
    --command "./run-tests.sh {ids_file} {out_dir}"

# 3 — here: the bundle returned however you like
runcomposer ingest results
```

`runcomposer_exec.py` is published as its own release asset precisely so you
can vendor it. It expands `{ids_file}` (one item id per line), `{run_id}` and
`{out_dir}` into your command, and writes `runcomposer_run.json` beside the
output — the marker carrying the run id and the spec hash that lets ingestion
correlate the bundle and detect tampering.

**The transport is your business.** Commit the bundle to a results branch,
`rsync` it, drop it on a share, attach it to a build artifact. Point
`core.ingestion.inbox` at wherever bundles land and the server picks them up
on its own; or push them to `POST /api/v1/runs/{id}/results` with the run's
ingest token; or run `runcomposer ingest` from cron on your side.

A worked, runnable version of this whole loop ships in
[`examples/remote-agent/`](examples/remote-agent). Start from there.

---

## 7. The executor contract

Whatever ends up running the spec — our runner, your script, a CI job — owes
exactly three things, and may ignore everything else in the document including
the entire `runner` section:

1. **Execute exactly `selection.materialized.item_ids`.** Not the filter.
   Executors do not re-compile selections; that is what makes the run
   reproducible.
2. **Check drift before executing.** If the live corpus snapshot differs from
   `source.snapshot`, refuse by default. With an explicit override, execute
   the intersection and report the difference as `SKIP` verdicts with reason
   `drift`.
3. **Reference `run.id` in the results**, plus a `shard` label if the work was
   split.

Three obligations, one document. If your connector honours these, everything
else in runcomposer works.

---

## 8. Order of work

1. Get a catalog. Try `manifest` first, even if you generate the JSON with a
   throwaway script — it tells you immediately whether your tags are good
   enough to select on, which is the real question.
2. Write the id-space test from §3. Before anything else.
3. Compose a selection with `runcomposer compile` and look at it. No
   execution yet.
4. Export a spec and run it by hand on a real test machine with
   `runcomposer_exec.py --simulate`. This proves the round trip without
   involving your test framework at all.
5. Swap `--simulate` for your real command.
6. Automate the return transport, then point the inbox at it.
7. Only now consider whether any custom plugin is actually needed.

---

## 9. Where your connector lives

**In its own private repository**, depending on the public `runcomposer`
package. Not as a fork, and not as a patch.

That separation is the whole architecture: everything specific to your
organisation — machine names, job names, environment vocabulary, credentials,
corpus layout — belongs on your side of the line, and nothing about your
environment should ever need to appear in this project to make your adoption
work. If you hit a case where it does, that is a bug in runcomposer's
extension surface, and worth reporting as one.

A typical connector is small:

```
my-connector/
├── pyproject.toml          # depends on runcomposer; registers entry points
├── config/                 # the environment configs
├── src/my_connector/
│   ├── sources.py          # only if resolve() needs your quirks
│   └── runners.py          # only if dispatch is genuinely exotic
└── scripts/
    ├── run-tests.sh        # the {ids_file} → your framework adapter
    └── return-results.sh   # the transport home
```
