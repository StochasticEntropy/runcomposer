# Adopting runcomposer

How to connect runcomposer to *your* test corpus, *your* machines, and *your*
result formats — and how little you should have to build to do it.

This guide is written for the person adopting runcomposer. It assumes you
have read the [README](README.md) and skimmed [DESIGN.md](DESIGN.md); it does
not repeat them. Everything here is about the seam between the public tool and
your environment. When you do write plugins, the
[connector guide](docs/connectors.md) is the reference — the ports, the runner
contract, the conformance kit, packaging — and
[`examples/connector-template/`](examples/connector-template) the package to
copy.

---

## The short version

Work down this list and stop at the first line that describes you. Most
adopters stop in the first half.

| Your situation | What you build |
|---|---|
| Robot Framework tests, executed on the runcomposer host | **Nothing.** Config: `robotframework` source + `robot-pool` runner. |
| Any framework, tests executed on the runcomposer host | A wrapper script, if that. Config: `manifest` source + the `command` runner (your command line per shard, results read by a configured parser). |
| Tests executed on machines runcomposer cannot reach | **A shell script.** Copy `runcomposer_exec.py` there; return the bundle. See [§6](#6-when-your-tests-run-somewhere-runcomposer-cannot-reach). |
| Tests executed by an existing Jenkins job | Config: the `jenkins` runner (your job's own parameter names, mapped in config) + a job stage that runs `runcomposer-exec`. |
| The taxonomy tree should show *your* structure, not the demo's | **A YAML file, no code.** `core.taxonomy_file`; format and a worked example in [docs/taxonomy.md](docs/taxonomy.md). |
| Your test ids don't match the names in your result files | A `TestSource` — or, often, just `aliases` in a manifest. See [§3](#3-the-rule-that-matters-most-the-id-space). |
| A result format nobody has written a parser for | A `ResultParser`. ~60 lines. |
| Somebody should be told when a run finishes | Config: an observer — `webhook`, `jsonl` or `command`. |
| Something genuinely exotic in how runs are dispatched | A `Runner`. |
| Postgres, or an existing results database | A `RunStore`. The largest of the ports, and rarely needed. |

If you find yourself writing a lot of code, that is a signal something is
wrong with the fit — please open an issue describing what you had to do.

---

## 1. What you do not have to build

The point of adopting rather than rebuilding is that these already exist and
are not your problem: the tag filter language and its compiler, the selection
model, the run spec document and its schema, run lifecycle and completion
accounting, dispatching off the request path with cancellation and restart
recovery, dispatch and delivery records, idempotent ingestion, the quarantine
inbox, the event stream, retention and `gc`, the HTTP API, and the web UI in
English and German.

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
  taxonomy_file: taxonomy.yaml  # the curated tree in the UI's tag picker
  artifact_dir: artifacts/
  ingestion:
    tokens: required          # HTTP pushes need the ingest token of their dispatch; "disabled" on closed networks
    inbox: results_inbox/     # watched directory for returned bundles
    quarantine_dir: quarantine/
store:
  sqlite: { path: runcomposer.db }
sources:
  robotframework: { root: tests/ }
  # …or several suite trees in one catalog, each parsed as its own top-level
  # suite (so the ids stay the ones results come back under):
  # robotframework: { roots: [api/tests, ui/tests] }
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

The sections are `core`, `store`, `sources`, `runners`, `parsers`,
`observers`, `ui` and `connectors`; anything else is refused. The rules for
the plugin sections are the same everywhere:

- **The key of an entry is its id** — for a runner, the id you dispatch to.
  The plugin behind it is the entry point of that name, unless the entry says
  otherwise with `plugin: <entry-point name>` or `module: "pkg.mod:Class"`. So
  one plugin can be configured several times under different keys
  (`staging-pool: { plugin: robot-pool, … }`).
- **Every other key is an option** handed to the plugin's constructor, and is
  **checked at startup**: an unknown or missing option fails `serve` (and
  `runcomposer doctor`) with a message naming what the plugin accepts —
  it does not surface later as a broken request.
- **`sources:` may hold several entries**; they form one catalog.
  `store:` holds exactly one. **Only runners listed under `runners:` can be
  dispatched to.**
- **`parsers:`** configures result parsers by format id, **`observers:`**
  names who is told about run events (`webhook`, `jsonl`, `command` ship).
- **Secrets stay out of the file**: a string option written `${env:NAME}` (or
  `${env:NAME:-default}`) is taken from the environment when the plugin is
  built; a missing variable fails startup naming it. Such a value is a
  secret, blanked out of every message, health detail and log line that
  quotes it, and must have at least four characters; a value from the
  environment that is no secret — an address, a region, a port — is written
  `${env-public:NAME}`. A number or a boolean is converted for an option
  that takes one ([connector guide §5](docs/connectors.md#5-configuration)).
- **A callable named in an option** (a hook, a provider, a check) is
  `"pkg.mod:callable"`, or `{call: "pkg.mod:callable", with: {…}}` to hand
  it settings, which it receives as `settings=`.
- **`connectors:`** is yours: `connectors.<name>` is a free-form mapping the
  core never interprets, for your connector package's own settings. A plugin
  class that declares `CONNECTOR = "<name>"` receives it as the keyword
  argument `connector_settings` (embedding code: `config.connector_settings("<name>")`).

**Every relative path above is relative to this file's directory** — the
`core:` keys and the plugin sections alike. So a config directory is a
portable unit: `envs/staging/config.yaml` finds `envs/staging/tests/` and
writes `envs/staging/runcomposer.db` whether you invoke it from the repository
root, from a CI workspace, or from `/`. Absolute paths are used as written.
(In 0.1.0 the plugin sections resolved against the *working* directory
instead, which quietly produced a second, empty database the first time
somebody ran the same `--config` from somewhere else. If you added an absolute
`store.sqlite.path` to work around that, it still behaves identically.)

The taxonomy is the other thing you write instead of code: `taxonomy_file` is
a YAML tree of tag patterns that the UI renders on the left and clicks into
the filter builder. It is validated on the same terms as the rest of the
config — a wrong-shaped file **fails the boot** with a message naming the
offending node, and a file broken under a running server answers `500` with
that same message rather than `200` with an empty panel. Write it against
[docs/taxonomy.md](docs/taxonomy.md), which has the node format, the
one-pattern-per-leaf limitation, the rules the validator enforces, and a
worked example.

Write it broadly rather than exhaustively: the UI does not render your file,
it renders the file **resolved against your catalog**, so a leaf carrying one
pattern comes out with a child for every concrete tag that pattern matches,
each selectable on its own. Leaves that match nothing collapse away, and tags
no leaf claims are gathered under one synthetic node — so an unfinished tree
still reaches the whole corpus, and a catch-all `regex:.*` leaf is no longer
worth writing. `runcomposer taxonomy-check` reports both directions of the
drift and `--tree` prints the resolved tree.

**One rule before you write any pattern**, in a taxonomy leaf or a filter: a
bare literal is matched case-**insensitively**, but `regex:` is compiled
without flags and `prefix:X` is exactly `regex:^X` — so both of those are
case-**sensitive**. A pattern written in the wrong case is valid and selects
nothing; `regex:(?i)…` makes one case-insensitive.

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
and skips them, the planned items that consequently never reported count as
`MISSING`, and the run is `INCOMPLETE` rather than green — but a run where
half the verdicts vanished is still a bad day.

---

## 4. The ports

An overview; the [connector guide](docs/connectors.md) is the full
reference, and `runcomposer.testing` checks an implementation against it.

There are five — `TestSource`, `Runner`, `ResultParser`, `RunStore` and
`RunObserver` — all `typing.Protocol`s in `runcomposer.sdk`, each with a base
class there (`SourceBase`, `RunnerBase`, `ParserBase`, `StoreBase`,
`ObserverBase`) that supplies its optional members. Derive from the base
class, or define every member yourself: runcomposer checks each configured
plugin against its protocol at startup and refuses one that lacks a member,
naming it. **Import everything from `runcomposer.sdk`** — and, for a runner
that executes Robot Framework or drives Jenkins its own way, from its toolkits
`runcomposer.sdk.robot` and `runcomposer.sdk.jenkins` — and from nowhere else
in the package; that is the connector surface, and the reference plugins
themselves import nothing else. 0.2 is a breaking revision of these ports: a
plugin written against 0.1 has to be ported.

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

Worth overriding: `fingerprint() -> str | None`, a *cheap* token (file sizes
and modification times, say) that changes when the corpus on disk may have
changed. With a token the catalog is reloaded only when needed; with the
`SourceBase` default (`None`), before every dispatch and on request
(`POST /api/v1/catalog/reload`).

Ships: `manifest` (JSON/YAML, only `id` + `tags` required), `robotframework`
(walks `.robot` files, ids are longnames).

### `Runner` — how a spec gets executed

```python
def describe(self) -> RunnerInfo                  # id, title, capabilities, per-run parameters
def dispatch(self, ctx: DispatchContext) -> None  # prepare → declare → deliver
# optional (defaults in RunnerBase):
def plan(self, ctx: PlanContext) -> PlanPreview   # what a dispatch would do, doing nothing
def cancel(self, ctx: DispatchContext) -> None
def health(self) -> Health
```

`dispatch` receives a `DispatchContext` — everything runcomposer gives a
runner for one dispatch, and everything the runner may do. There are no hooks
looked up by attribute and nothing to bind. A complete runner:

```python
from runcomposer.sdk import DispatchContext, Parameter, RunnerBase, RunnerInfo, Verdict


class MyRunner(RunnerBase):
    def __init__(self, endpoint: str, token: str | None = None):
        self.endpoint, self.token = endpoint, token      # options from the config entry

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="my-runner",
            title="My runner",
            parameters=(Parameter("stage", kind="choice", choices=("test", "staging"), default="test"),),
        )

    def dispatch(self, ctx: DispatchContext) -> None:
        if ctx.source.snapshot() != ctx.spec["source"]["snapshot"]:
            ctx.refuse("the corpus changed since this run was composed — compose it again")
        ctx.declare(["1"], plan=f"my-runner: {len(ctx.item_ids)} item(s) on {ctx.parameters['stage']}")
        verdicts = [Verdict(item_id, "PASS", duration_ms=120) for item_id in ctx.item_ids]
        ctx.deliver("1", verdicts=verdicts)
```

The shape is always the same:

- **Prepare, and refuse if you must** — `ctx.refuse(reason)` (it raises
  `DispatchRefused`). A refusal before the hand-off leaves no dispatch behind;
  say in the message what to do about it.
- **`ctx.declare(shards, plan=…, links=…)` — the hand-off, exactly once.** It
  names the shards you will deliver (or maps each shard to the item ids it
  will report) and moves the run to `RUNNING`. Completion is computed against
  that shard set and against the planned items: an item nobody reports is
  `MISSING`, and its run is not `PASS`.
- **`ctx.deliver(shard, verdicts=…)`** for verdicts you hold, or
  **`ctx.deliver(shard, bundle=path, format=…)`** for native result files —
  those are parsed with the configured `ResultParser` and correlated through
  the catalog for you. Whatever you raise after the hand-off is a `FAILED`
  dispatch whose delivered shards are kept.
- **Return with shards outstanding** when the results come home by ingestion
  (a CI job that posts back): the run then waits in `AWAITING_RESULTS`.
  `ctx.export_spec()` gives you the exact document bytes to hand to that
  executor and `ctx.ingest_token()` the token it needs to push.

Also on the context: `ctx.item_ids`, `ctx.spec` (read-only), `ctx.options`
(your runner's config entry as written, and nothing else) and
`ctx.parameters` (what this run chose: the validated per-run parameters,
defaults applied — a parameter never appears in `ctx.options`),
`ctx.workdir` (a directory for this dispatch under the artifact root — a file
there is served once `ctx.add_artifact(...)` recorded it, not before),
`ctx.private_dir` (for files that must never be served), `ctx.source` (the
live catalog), `ctx.history.durations(...)`, `ctx.live(...)` and the picklable
`ctx.live_channel()` for per-item status (`RUNNING`, then the verdict) and log
lines from worker processes, `ctx.set_link(...)`, `ctx.outstanding_shards()`,
`ctx.log(...)`, `ctx.cancelled` / `ctx.raise_if_cancelled()`. A runner with
the `plan` capability also answers `plan(ctx)` — a preview of the dispatch
that executes, records and triggers nothing (`runcomposer plan`).

**`dispatch` may block for as long as the execution takes.** runcomposer calls
it off the request path: `POST /api/v1/runs` with a dispatch answers at once,
and the run is followed through its state and event stream. (The CLI's `run`
and `dispatch` execute the same code in the foreground.) One runner instance
is built per configured id and may be asked to dispatch several runs at once,
so keep everything that belongs to one dispatch on its context, not on `self`.

**A run spec cannot reconfigure your runner.** Only what you declare in
`RunnerInfo.parameters` can be set per run; the accepted values are written
into `runner.<id>` of the document, and any other key found there is ignored
with a warning.

Ships: `robot-pool` (Robot Framework in worker processes: partitions,
duration-balanced chunking, exact id selection, readable `log.html`, reruns of
failures, live verdicts, cancellation), `jenkins` (a parameterized Jenkins job
with configurable parameter names and artifact paths; callback or polling
completion), `command` (a configured command line per shard — the connector
for any framework that has one), and `demo` (simulated). Before writing a
runner, read the
[connector guide §9](docs/connectors.md#9-composing-the-reference-runners):
`robot-pool` and `jenkins` have switches for much that looks like it needs
code (partitions as lanes, every chunk a shard, preparation before the
hand-off, a job that fans out, results read from a build's console log), and
the toolkits they are built from are there for when it does.

### `ResultParser` — what results look like

```python
format_id: str
def parse(self, path) -> list[ParsedVerdict]   # ParsedVerdict(native_name, status, …)
```

Emit the native name **exactly as the artifact spells it**. Do not try to
reconstruct catalog ids here — that is the source's job (see §3), and doing it
here is the one design mistake that reliably makes a corpus unmaintainable.

`status` is one of `PASS`, `FAIL`, `SKIP`, `ERROR`. Raise `ParseError` (from
`runcomposer.sdk`) for input you cannot read, and nothing else — that is what
lets every transport quarantine a bad bundle the same way. If your format is
XML, read it through `parse_xml(path)` (or call `forbid_dtd(path)` before your
own reader): a document carrying a DTD or an entity declaration is then
refused by the parser itself, wherever the declaration sits and whatever the
encoding. Both shipped XML parsers do this, and there are tests that prove it.

A parser that takes options is configured under `parsers:`, keyed by its
format id (`parsers: { my-format: { module: "mypkg.parsers:MyParser", … } }`).
A spec names a format id only — never an import path.

Ships: `runcomposer-verdicts` (a trivial JSON format any executor can emit),
`robot-output-xml`, `junit-xml`.

### `RunStore` — where runs live

The widest protocol, and the one you almost certainly do not need. `sqlite`
ships and is zero-setup (safe for several threads and processes on one file —
every lifecycle decision is one write transaction — needs SQLite 3.24.0 or
newer with the JSON functions, and has a versioned schema that migrates a
0.1 database in place). Implement your own only if you must live in an
existing database; it then provides `atomic()`, the block of calls
runcomposer takes each lifecycle decision in
([connector guide §4](docs/connectors.md#runstore--persistence)).

What it holds: runs and their spec documents, dispatches (each with its own
lifecycle state, declared shards, plan, links, the exact document bytes handed
out, and the hash of its ingest token), deliveries, delivered verdicts, live
status (kept apart from delivered verdicts), artifact references, and the
per-run event stream. **The store records; it decides nothing** — lifecycle
state is computed by the core and written through `update_dispatch` and
`set_run_headline`. Every method must be safe to call from several threads.

The idempotency contract lives here and is not optional: `record_delivery` is
keyed on `(run, dispatch, shard)` and must be atomic — the same content hash
again is a no-op, different content for a shard that already delivered
replaces that shard's verdicts, and there is no monotonic merge: a correction
must be able to turn a `FAIL` back into a `PASS`.

**Read paths are as much of the contract as write paths.** Three members exist
because the write half alone leaves a question nobody can answer:

```python
def verdicts_for(self, run_id, dispatch_id=None, *, shard=None) -> list[Verdict]
def artifact_refs(self, run_id, dispatch_id=None) -> list[ArtifactRef]
def latest_completed_run(self, *, completed_before=None, labels=None) -> RunRecord | None
```

- Every `Verdict` you return **must carry the `shard` it was delivered
  under**. `record_delivery` is what assigns it (the producer never knows it),
  and one selection fanned out over two partitions is otherwise two
  indistinguishable rows per item. `attempt` likewise round-trips.
- `artifact_refs` reads back what `add_artifact_ref` wrote. Without it a
  failed shard's log file is recorded and unreachable.
- `latest_completed_run` takes a **label scope**. Unscoped, "latest" ranges
  over every completed run in your store, so where several streams of runs
  share one instance a `failed@latest` rerun can follow the wrong one — and
  still be a valid run.

**0.2 is a breaking revision of this port.** A store written against 0.1 has
to be ported: `add_dispatch` no longer re-declares (dispatches are changed
with `update_dispatch`), `set_run_state` is gone, `list_runs` pages and
returns a total, and the event stream, live verdicts, dispatch spec bytes,
ingest-token hashes, `delete_run`, `owned_dispatches` and `close` are new
required members. The full list, with what each member must guarantee, is the
`RunStore` protocol in [`core/ports.py`](src/runcomposer/core/ports.py).

### `RunObserver` — who is told

```python
def notify(self, event: RunEvent) -> None
```

Called for every lifecycle event of every run (composed, dispatched, running,
completed, failed, cancelled, stale, a delivery recorded, a dispatch refused),
on a thread of its own. An observer that raises is recorded as a warning on
the run and affects nothing else. Configure which kinds an observer hears with
`events:` on its entry. Most adopters need no code here: `webhook` (JSON POST,
optionally HMAC-signed), `jsonl` (append to a file) and `command` (run a
command with the event in its environment) ship.

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
`runcomposer.stores`, `runcomposer.observers`. `runcomposer doctor` lists what
is installed, with the distribution and version each plugin comes from.

**Or an import path in the config**, which needs no packaging at all — the
hack-it-in-an-afternoon path:

```yaml
sources:
  my-source:
    module: "mypkg.sources:MySource"
    root: some/where           # every other key is an option of your __init__
runners:
  my-runner:
    module: "mypkg.runners:MyRunner"
    endpoint: https://executor.shop.example
    token: ${env:MY_RUNNER_TOKEN}     # taken from the environment at startup
```

Options are checked against your constructor's parameters at startup — a
misspelled key is refused, naming the ones you accept. (A constructor that
takes `**kwargs` opts out of that check; a class attribute `OPTIONS = (…)`
states the accepted names explicitly.) Construction must have no side
effects: every configured plugin is built once when the server starts.

### Paths in your plugin's options

Your options reach your `__init__` as written, and a relative path among them
would mean "relative to the process's working directory" — which makes your
plugin behave differently depending on where the operator stood when they
typed the command. To get the same rule the rest of the config follows
("relative to the config file"), say which of your options are paths:

```python
class MySource(SourceBase):
    PATH_OPTIONS = ("root",)          # a string, or a list of strings
```

The core resolves exactly those before constructing you: absolute paths, empty
strings and non-strings are left untouched, anything else is joined onto the
config file's directory — which is absolute, also when `--config` named the
file by a relative path, so your plugin receives absolute paths
(`tests/test_config_paths_absolute.py`). It deliberately does not guess — plenty of plugin
options look path-shaped and are not (a listener spec, a shell hook, a URL).
When the rule has exceptions, decide yourself instead:

```python
class MyStore(StoreBase):
    @classmethod
    def resolve_config_paths(cls, options: dict, resolve) -> dict:
        if "path" in options and options["path"] != ":memory:":
            options["path"] = resolve(options["path"])
        return options
```

A plugin that declares no path options is constructed with its options
verbatim.

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
`{out_dir}` into your command — those three placeholders and nothing else, so
the rest of your command line is passed through untouched — and, when the
command succeeded (`--ok-exit-codes` says which exit codes count), writes
`runcomposer_run.json` beside the output: the marker carrying the run id and
the spec hash that lets ingestion correlate the bundle and detect tampering,
and whose presence tells a file-drop inbox that the bundle is complete.

**The transport is your business.** Commit the bundle to a results branch,
`rsync` it, drop it on a share, attach it to a build artifact. Point
`core.ingestion.inbox` at wherever bundles land and the server picks them up
on its own; or push them to `POST /api/v1/runs/{id}/results` with the ingest
token that the export printed (it is shown once, belongs to that export
dispatch, and is never part of the spec document); or run `runcomposer ingest`
from cron on your side.

An executor that can reach the API may also report while it works, with
the same token: `runcomposer_exec.py --callback <results URL> [--push]` sends
log lines (and pushes the finished bundle with `--push`), and a Robot
execution started with `--listener runcomposer.sdk.robot.RemoteLiveStatusListener`
streams which test is running — that one needs runcomposer installed where
Robot runs ([connector guide
§3.3](docs/connectors.md#33-dispatchctx--prepare-declare-execute-deliver)).
Behind a private certificate authority, give the executing side the CA
bundle to trust — `--ca-bundle`, or `RUNCOMPOSER_CA_BUNDLE` in its
environment, which the listener reads too. And the server must answer to
the name the executor uses: listed in `core.api.hosts` once that is set
(a server bound to a loopback address answers to the loopback names only
until it is; [docs/cli.md](docs/cli.md#the-names-it-answers-to)).

**A minimal install for a machine that only executes.** Where runcomposer
itself must be installed on the executing side — for the Robot listener, the
Robot toolkit's worker, or `push_results` from a script of yours —
`pip install --no-deps runcomposer` is enough: the SDK, `RemoteLiveChannel`,
`push_results` and `runcomposer-exec` import without the reading and
serving dependencies, and `pip install robotframework` beside it lets the
Robot toolkit execute a shard. Reading a YAML spec, validating a document
and every other `runcomposer` command need the full install; on a minimal
one they raise an error naming the missing package
(`tests/test_minimal_imports.py`).

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

A typical connector is small — and
[`examples/connector-template/`](examples/connector-template) is one to copy:

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
