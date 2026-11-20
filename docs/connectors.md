# Writing a connector

A **connector** is what joins runcomposer to an environment: where the tests
come from, where they run, what their results look like, who is told when a
run ends. It is a Python package of plugins — or a single module named in a
config file — written against one import, `runcomposer.sdk`, and tested with
one more, `runcomposer.testing`.

Read [ADOPTING.md](../ADOPTING.md) first: most adoptions need configuration,
not code, and it says when you need which. This guide is for when you do
write code. The fastest start is to copy
[`examples/connector-template/`](../examples/connector-template) — a complete
installable connector package with tests — and change it.

Every behaviour described here is pinned by a test or a runnable example; the
references are given where it matters.

**Contents.**
[The surface](#1-the-surface) ·
[The ports](#2-the-ports-and-their-base-classes) ·
[The runner contract](#3-the-runner-contract) ·
[Sources, parsers, stores, observers](#4-sources-parsers-stores-observers) ·
[Configuration](#5-configuration) ·
[The conformance kit](#6-the-conformance-kit) ·
[Packaging](#7-packaging) ·
[Four ways to execute](#8-four-ways-to-execute) ·
[Known limits](#9-known-limits)

---

## 1. The surface

| Import | When | What |
|---|---|---|
| `runcomposer.sdk` | at run time, in your plugins (worker processes included) | the protocols, the base classes, the records, the errors, the helpers, and `Service` / `load_config` for driving runcomposer from Python |
| `runcomposer.testing` | in your tests | the conformance checks, `FakeDispatchContext`, `ConnectorHarness` |

Both keep their names and meaning within a minor line (`runcomposer>=0.2,<0.3`);
a removal is a breaking change listed in the changelog. Their names are frozen
in `tests/test_sdk.py` and `tests/test_testing_kit.py`, so no change to either
surface is accidental. **Import nothing else of runcomposer** — every other
module may change in any release. runcomposer holds its own reference plugins
to that rule (`tests/test_self_containment.py`), which is the evidence that
the SDK is enough; the template carries the same guard for your package
(`examples/connector-template/tests/test_guard.py`).

Importing `runcomposer.sdk` loads the core only — no web framework, no
application layer — so it is cheap in a worker process
(`tests/test_sdk.py::TestLazyEmbedding`).

---

## 2. The ports and their base classes

Five roles, each a `typing.Protocol` in `runcomposer.sdk`, each with a base
class that supplies its optional members:

| Role | Base class | You write | The base supplies | Reference plugins |
|---|---|---|---|---|
| `TestSource` | `SourceBase` | `provider_id`, `items()`, `snapshot()`, `resolve()` | `fingerprint()` → `None`, `duplicate_ids` → `()` | `manifest`, `robotframework`, `demo-corpus` |
| `Runner` | `RunnerBase` | `describe()`, `dispatch(ctx)` | `cancel(ctx)` (nothing more to stop), `health()` → `unknown` | `robot-pool`, `jenkins`, `command`, `demo` |
| `ResultParser` | `ParserBase` | `format_id`, `parse(path)` | — | `runcomposer-verdicts`, `junit-xml`, `robot-output-xml` |
| `RunStore` | `StoreBase` | every member but `close()` | `close()` | `sqlite` |
| `RunObserver` | `ObserverBase` | `notify(event)` | — | `webhook`, `jsonl`, `command` |

All five bases derive from `PluginBase`, which supplies the configuration
members every plugin class has (`Configurable`): `OPTIONS`, `PATH_OPTIONS`,
`CONNECTOR` and `resolve_config_paths` — [§5](#5-configuration).

**The contract is checked, never guessed.** When runcomposer builds a
configured plugin it checks the class for the `Configurable` members and the
built object for every member of its role's protocol. A plugin that lacks one
does not start:

```text
error: runners.bare: RunnerWithoutCancel does not provide ['cancel'] of the Runner contract —
derive it from runcomposer.sdk.RunnerBase, which supplies the optional members, or define them
```

After that check every member is called directly; nothing is looked up by
attribute convention, and a guard keeps it that way
(`tests/test_contract.py`). Deriving from the base class is therefore the
easy way; a class that does not derive from it must define every member
itself. `contract_members(Runner)` returns the list the check uses.

**Construction must be free of side effects.** Every configured plugin is
built once at startup to validate the configuration (`serve`, `doctor`, and
every command that loads a deployment). Check and store your options in
`__init__`; connect to things in `dispatch`, `health`, `items`, `parse`.

---

## 3. The runner contract

```python
from runcomposer.sdk import DispatchContext, Health, Parameter, RunnerBase, RunnerInfo, Verdict


class ShopRunner(RunnerBase):
    def __init__(self, base_url: str, workers: int = 4):   # the options of its config entry
        self.base_url, self.workers = base_url, workers

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="shop",
            title="Shop checks",
            capabilities=("cancel",),
            parameters=(Parameter("region", kind="choice", choices=("eu", "us"), default="eu"),),
        )

    def dispatch(self, ctx: DispatchContext) -> None:
        if ctx.source.snapshot() != ctx.spec["source"]["snapshot"]:
            ctx.refuse("the corpus changed since this run was composed — compose it again")
        region = ctx.options["region"]
        ctx.declare([region], plan=f"{len(ctx.item_ids)} check(s) in {region}")
        verdicts = []
        for item_id in ctx.item_ids:
            ctx.raise_if_cancelled()
            verdicts.append(Verdict(item_id, "PASS", duration_ms=12))
        ctx.deliver(region, verdicts=verdicts)
```

The template's [`http_runner.py`](../examples/connector-template/src/shop_connector/http_runner.py)
is the same shape doing real work: HTTP requests on a thread pool, live
status, an artifact, health.

### 3.1 `describe()` — who it is and what a run may choose

`RunnerInfo` carries `id` (the plugin's name), `title`, `description`,
`capabilities`, `parameters` and `result_format` (when it is fixed).
Capabilities with meaning to runcomposer and its clients: `live_status`,
`partitions`, `cancel`, `external_delivery` (results arrive by ingestion after
`dispatch` returns), `simulated` (nothing is really executed).

`parameters` are the **per-run options** whoever starts a run may set — a
`Parameter` has `name`, `kind` (`string`, `integer`, `boolean`, `choice`,
`multi_choice`), `label`, `help`, `default`, `choices`, `required`. The API
lists them (`GET /api/v1/runners`), the UI renders a form from them, the CLI
takes them as `--param name=value`, and every submitted value is validated
against them — an unknown, missing or ill-typed value is a `400` naming the
parameter, before a run exists (`tests/test_api.py::…per_run_parameters`,
`tests/test_dispatch.py::TestParameters`). Accepted values are written into
`runner.<id>` of the run's document and reach you overlaid on your configured
options in `ctx.options` (on their own in `ctx.parameters`).

**A document cannot reconfigure your runner.** Of a spec's `runner.<id>`
section you see exactly the keys you declared; anything else is ignored with a
warning. Hooks, listeners, paths and credentials come from the config file
only (DESIGN.md §6.2).

The id a deployment dispatches to is the **config key**, not `RunnerInfo.id`:
one plugin may be configured several times (`examples/jenkins/config.yaml`
configures `jenkins` twice, once per completion mode).

### 3.2 `dispatch(ctx)` — prepare, declare, execute, deliver

`dispatch` runs off the request path, on the dispatch manager's bounded pool
(the CLI's `run` executes the same code in the foreground). It may block for
as long as the execution takes. One runner instance serves every dispatch of
its config key, possibly several at once: keep what belongs to one dispatch
on `ctx`, never on `self`.

| Phase | Calls | If you raise |
|---|---|---|
| **prepare** | read `ctx.item_ids`, `ctx.spec`, `ctx.options`, `ctx.source`, `ctx.history`; check drift, readiness; `ctx.refuse(reason)` | before `declare`: a **refusal** — no dispatch remains, the run is as it was, the reason is a `dispatch.refused` event and the answer to the request |
| **hand-off** | `ctx.declare(shards, plan=…, links=…)` — exactly once | — |
| **execute** | `ctx.live(…)`, `ctx.add_artifact(…)`, `ctx.log(…)`, `ctx.raise_if_cancelled()` | after `declare`: the dispatch is **FAILED** with the error text; whatever was delivered is kept |
| **deliver** | `ctx.deliver(shard, verdicts=… \| bundle=…)` per declared shard | |
| **return** | — | all declared shards delivered → **COMPLETE**; shards outstanding → **AWAITING_RESULTS** (they come by ingestion); returned without `declare` → recorded as a refusal |

The order is enforced, with the same code in runcomposer and in the kit's
fake context (`runcomposer.core.handoff`, `tests/test_testing_kit.py::TestFakeAndRealAgree`):

- `refuse()` after `declare()` → `RuntimeError` (the dispatch fails instead);
- a second `declare()` → `RuntimeError`;
- `deliver()` before `declare()` → `RuntimeError`;
- `declare("eu")` (one string) → `TypeError`; duplicate shard names → `ValueError`;
- `deliver()` with both or neither of `verdicts=` / `bundle=` → `TypeError`;
- a delivered verdict with status `MISSING` → `ValueError` (an executor reports
  `PASS`, `FAIL`, `SKIP` or `ERROR`; `MISSING` is derived).

**`declare(shards, *, plan="", links=None, spec_sha256=None)`.** `shards` is
a list of names, or a mapping of names to the item ids each shard will
report — then an item a shard never reports is `MISSING` for that shard.
Completion is computed against the names you declared and the planned items:
a run with an unreported item is never `PASS`. `plan` is text shown with the
dispatch; `links` are named URLs — and how `cancel` finds your executor again
later, since they are stored as given.

**`deliver(shard, *, verdicts=None, bundle=None, format=None)`** — exactly one
of the two payloads:

- `verdicts=[Verdict(item_id, status, duration_ms, message, attempt=…)]`
  when you hold correlated results;
- `bundle=path` (a file or a directory of native result artifacts): parsed
  with the configured `ResultParser` for `format` (default: what the run
  expects, `results.expect[0].format`), every native name correlated through
  `TestSource.resolve`; names the catalog does not know become warnings,
  their items `MISSING`. A bundle carrying a `runcomposer_run.json` marker
  for another run or dispatch is not delivered.

Deliveries are idempotent by content hash per `(run, dispatch, shard)`: the
same content again is a no-op (`duplicate`), different content replaces the
shard (`replaced`) — a correction can turn `FAIL` into `PASS`. The return
value says which. `ctx.parse(bundle, format=…)` gives you the correlated
verdicts without delivering, for a runner that relabels them first (attempt
numbers for reruns, as `robot-pool` does).

**Live status.** `ctx.live(shard, item_id, status, duration_ms=…, message=…)`
records per-item status while a shard executes; it is shown flagged `live`
and replaced by the shard's delivery. It never invents an item or a status:
an id that was not planned, or a status no executor reports, is ignored
(`tests/test_dispatch.py::…never_invents_an_item_or_a_status`). From a worker
process use `ctx.live_channel()`: a small picklable object with the same
`live` method. It carries no store — it appends to a file in `ctx.workdir`
that the dispatching process tails — so it works with any `RunStore`
(`tests/test_dispatch.py::TestLiveStatus`).

**Artifacts and log.** `ctx.workdir` is a directory for this dispatch under
the artifact root, already created; files there are servable.
`ctx.add_artifact(name, path_or_url, media_type=…, shard=…)` records a file
or a link (an `http(s)` URL passes through as a link; anything else is a
local path, served only from inside the artifact root).
`ctx.log(message, level="info")` appends a line to the run's event stream;
`level="warning"` makes it a warning event.

**External executors.** A runner that hands the work to something
runcomposer will not hear from again until results arrive (a CI job, a
remote agent) uses `ctx.export_spec()` — the document as the exact JSON bytes
to hand over, their hash recorded so a marker written from them verifies —
and `ctx.ingest_token()` — the token that executor pushes results with
(`POST /api/v1/runs/{id}/results`), bound to this dispatch, stored only as a
hash, `None` when the deployment disabled tokens. It declares its shards,
returns, and declares the `external_delivery` capability; the run waits in
`AWAITING_RESULTS` (`jenkins` with `completion: callback` does exactly this).

**History.** `ctx.history.durations(labels=…, last_n=…)` gives the mean
duration per item over earlier dispatches; `plan_chunks(item_ids, n,
durations)` from the SDK cuts a duration-balanced plan with it (round-robin
on a cold start). Both are empty on a fresh store.

### 3.3 `cancel(ctx)` and `health()`

Cancellation is cooperative first: runcomposer sets `ctx.cancelled`, then
calls your `cancel(ctx)` from another thread while `dispatch` may still run.
Check `ctx.cancelled` / call `ctx.raise_if_cancelled()` between units of
work; stop processes or remote builds in `cancel`. The dispatch becomes
`CANCELLED` when `dispatch` returns. A dispatch that only awaits results
(your `dispatch` returned long ago) is cancelled at once: runcomposer calls
`cancel` with a context rebuilt from the stored record — `ctx.links` is how
you find the build — and closes the dispatch (`tests/test_dispatch.py::TestCancel`,
`tests/test_jenkins.py::…awaits_results`). A cancel requested by another
process (the CLI against a server) reaches the process that executes the
dispatch within half a second. Declare the `cancel` capability when you
honour it.

`health()` answers "could you take a dispatch right now?" with a
`Health(status="ok" | "degraded" | "down" | "unknown", detail=…, data=…)`.
It is cached for 30 s in the runner list and shown by `runcomposer doctor`.

### 3.4 What a refusal means to the person who started the run

A refusal that happens within `core.dispatch.handoff_wait_s` (default 2 s)
of the request is the request's answer (`409 dispatch-refused`), and a run
that request composed is removed again unless it asked for `keep_run`. The
answer to a dispatch request says `handoff: done` or `handoff: pending`; a
refusal after a `pending` answer leaves the run — which the answer has named
— `COMPOSED`, with the reason as a `dispatch.refused` event. A run an answer
has named is never removed implicitly (`tests/test_api.py::TestRefusedDispatch`).
So refuse early and say in the reason what to do about it.

---

## 4. Sources, parsers, stores, observers

### `TestSource` — the catalog and the id space

`items()` lists `Item(id, tags, name="", hierarchy=(), meta={})`.
`snapshot()` is a content hash of what a selection depends on — the ids and
their tags, not file times — recorded at compose time and compared before
execution (drift). `resolve(native_name)` maps a name as a result artifact
spells it to an item id, or `None`. **Your source owns the id space**: every
native name any parser emits must resolve to exactly one id, and every item id
must resolve to itself (the reference result format names items by id). The
core compares ids for equality only; every naming quirk of your framework
belongs in `resolve` ([ADOPTING.md §3](../ADOPTING.md#3-the-rule-that-matters-most-the-id-space)).

`fingerprint()` returns a cheap token that changes when the corpus may have
changed — file sizes and modification times, no parsing — and runcomposer
reloads the catalog exactly when it does; `None` (the `SourceBase` default)
means "cannot tell": rebuilt on request and before every dispatch.
`duplicate_ids` lists ids more than one item carries — reported, not raised.
Several `sources:` entries form one catalog; `item.meta` is yours (the
template's runner reads its requests from it).

### `ResultParser` — native artifacts in, native names out

`format_id` is a name (no `:` or `/` — a spec names a format, never an import
path). `parse(path)` takes a file or a directory and returns
`ParsedVerdict(native_name, status, duration_ms=0, message="", attempt=1)`:
the native name exactly as the artifact spells it — never reconstruct ids
here. Bad input is a `ParseError` and nothing else. Read XML through
`parse_xml(path)` (or call `forbid_dtd(path)` first): a document with a DTD
is refused wherever the declaration sits. A parser with options is configured
under `parsers:`, keyed by its format id.

### `RunStore` — persistence

The widest port and rarely needed (`sqlite` ships). The store records; it
decides nothing — lifecycle state is computed by the core. Every method must
be thread-safe; `record_delivery` must be atomic per `(run, dispatch, shard)`
across processes; `add_dispatch(exclusive=True)` must refuse with `RunActive`
when the run's latest dispatch is in flight, checked in the same transaction
as the insert (that is what keeps two processes from both dispatching one
run); `latest_events` is a bounded read of the tail. `check_store` runs all
of it, concurrency included, against your implementation.

### `RunObserver` — the completion hook

`notify(event)` is called on a thread of its own, in event order, for the
kinds its config entry selects (`events:`, `*` wildcards; default: the
lifecycle events). Whatever it raises is recorded on the run as a warning and
changes nothing else (`tests/test_observers.py::TestAnObserverNeverAffectsARun`).
A `RunEvent` has `seq`, `run_id`, `at`, `kind`, `dispatch_id`, `data`.

---

## 5. Configuration

One file, chosen with `--config`; relative paths in it resolve against its
own directory.

```yaml
sources:                       # one or more → one catalog
  shop-ui:  { plugin: robotframework, root: ui/tests }
  checks:   { plugin: http-checks, path: checks.yaml }       # your entry point
runners:                       # the key is the id people dispatch to
  shop:     { plugin: http-checks, environment: local }
  hotfix:   { module: "shop_hotfix.runner:HotfixRunner" }     # an import path, no packaging
parsers:                       # options per result format id
  junit-xml: { pattern: "TEST-*.xml" }
observers:
  summary:  { plugin: run-summary, path: state/summary.txt, events: ["run.*"] }
connectors:                    # free-form: your package's shared settings
  shop-checks:
    environments: { local: "http://127.0.0.1:18765", staging: "${env:SHOP_STAGING_URL}" }
```

- **Selecting the class.** `plugin: <entry-point name>`, `module:
  "package.module:Class"`, or neither — then the key is the entry-point name.
- **Options are validated at startup.** Every other key of an entry is an
  option: by default the constructor's keyword parameters are the accepted
  options (a constructor taking `**kwargs` accepts anything); a class
  attribute `OPTIONS = ("a", "b")` declares them explicitly. An unknown or
  missing option is a `ConfigError` naming the entry and the accepted keys —
  `serve`, `doctor` and the harness refuse to start
  (`tests/test_config.py`, `examples/connector-template/tests/test_connector.py::test_a_typo_in_the_runner_entry_fails_startup`).
- **Paths.** Name the options that are file paths in `PATH_OPTIONS = ("root",)`
  and they arrive resolved against the config file's directory; override the
  classmethod `resolve_config_paths(options, resolve)` when the rule has
  exceptions (the sqlite store leaves `:memory:` alone). A path inside a run
  spec is never rewritten.
- **Shared settings.** `connectors:` is never interpreted by runcomposer. A
  plugin class that declares `CONNECTOR = "shop-checks"` is built with
  `connectors.shop-checks` as the keyword argument `connector_settings`
  (`${env:…}` resolved) — one place for what several plugins of your package
  share (`tests/test_contract.py::TestConnectorSettings`). Embedding code
  reads the same mapping with `config.connector_settings("shop-checks")`.
  Any other unknown top-level key is an error that names `connectors:`.
- **Secrets.** A string option `${env:NAME}` or `${env:NAME:-default}` — at
  any depth, in plugin entries and under `connectors:` — is replaced from the
  environment when the plugin is built; a missing variable is a `ConfigError`
  naming it. Configured options never travel into a spec or an API response,
  and where your plugin quotes one anyway — a refusal reason, an error, its
  health, a log line, a plan — every value that came from the environment is
  blanked out first (`tests/test_hardening.py`). See [§9](#9-known-limits) for
  the edges of that.
- **Several entries of one plugin** are independent instances; `runcomposer
  doctor --config …` lists what is installed and what is configured, with the
  distribution and version each class comes from, and every runner's health.

---

## 6. The conformance kit

`runcomposer.testing` runs the contract against your plugins, from any test
runner (it never imports pytest). Every reference plugin of runcomposer is
tested with it (`tests/test_testing_kit.py`), and a deliberately broken toy
per rule is caught with its rule named.

### The checks

```python
from runcomposer.testing import check_observer, check_parser, check_runner, check_source, check_store

items = check_source(MySource("catalog.yaml"), native_names={"Cart.Total": "shop.cart.total"},
                     rebuild=lambda: MySource("catalog.yaml"))
parsed = check_parser(MyParser(), "fixtures/good-bundle", expected={"Cart.Total": "PASS"},
                      bad_bundles=["fixtures/truncated.xml", xml_with_dtd(tmp_path / "dtd.xml")])
ctx = check_runner(MyRunner(...), FakeDispatchContext(item_ids), expect="complete")
check_runner(MyRunner(...), FakeDispatchContext(item_ids), cancel_after_declare=True, expect="cancelled")
check_store(lambda: MyStore(tmp_path / f"{uuid4()}.db"))   # a fresh, empty store per call
check_observer(MyObserver(...), seen=lambda: received)      # what your destination got so far
```

Each raises `AssertionError` whose message begins with the violated rule:

```text
[runner.delivers-declared-shards] delivered shard 'eu', which was not declared (['us'])
[source.resolve-own-ids] resolve('Shop.Cart.Core.T001') returned 'shop.cart.core.t001', not the id itself
[parser.bad-input-is-parse-error] truncated.xml: raised KeyError (…) — bad input is a ParseError and nothing else
```

| Check | Rules (abridged) |
|---|---|
| `check_source` | contract members; `provider_id`; items are `Item`s with string tags, stable across calls; `duplicate_ids` lists exactly the duplicated ids; snapshot and fingerprint stable; every id resolves to itself; an unknown name resolves to `None`; the native names you give resolve; a rebuilt instance agrees |
| `check_parser` | contract; `format_id` is a name; `parse` returns `ParsedVerdict`s with reportable statuses, non-negative durations, attempts ≥ 1; deterministic; non-empty; `expected` statuses; every bad bundle is a `ParseError` |
| `check_runner` | contract; `describe` well-formed, parameter defaults validate; `health` well-formed; the dispatch keeps the hand-off order; declares at least one shard; delivers only declared shards and only planned items; ends as `expect`ed; outstanding shards only with `external_delivery`; with `cancel_after_declare`, returns within the timeout of being cancelled |
| `check_store` | contract; runs, documents, paging/filtering/search; dispatches incl. the exclusive claim; spec bytes; token hashes; the delivery idempotency contract; shard and attempt on read-back; live status; artifacts; the event cursor and `latest_events`; history queries; delete and prune; concurrent deliveries leave exactly one |
| `check_observer` | contract; every event kind accepted, `None` returned; concurrent calls; with `seen`, every notification arrives exactly once |

### `FakeDispatchContext` — unit tests for a runner

A recording, in-memory `DispatchContext`. Give it the planned items (ids,
`Item`s with tags, or a whole spec), options, parameters, a source, history
durations, parsers; run your runner; read what it did:

```python
ctx = FakeDispatchContext(["shop.cart.add", "shop.checkout.page"], parameters={"environment": "qa"})
assert ctx.run(MyRunner(...)) == "complete"          # or refused / undeclared / cancelled / awaiting
assert ctx.declared == ("qa",) and ctx.summary() == {"PASS": 2}
assert ctx.refusal is None and ctx.outstanding == []
ctx.deliveries, ctx.live_updates, ctx.artifacts, ctx.logs, ctx.exported, ctx.tokens
```

The hand-off order is enforced by the same code as in runcomposer, so the
same misuse raises the same error with the same message
(`TestFakeAndRealAgree`). `ctx.live_channel()` is the real file transport:
what a worker process writes shows up in `ctx.live_updates`.
`ctx.request_cancel()` does what a cancel does.

### `ConnectorHarness` — a real runcomposer around your plugins

```python
with ConnectorHarness(
    sources={"checks": {"plugin": "http-checks", "path": "example/checks.yaml"}},
    runners={"shop": {"plugin": "http-checks"}},
    connectors={"shop-checks": {"environments": {"local": "${env:SHOP_URL}"}}},
    env={"SHOP_URL": url},
) as harness:
    result = harness.run("Smoke", runner="shop", timeout=60)
    assert (result.state, result.completion) == ("COMPLETE", "PASS")
    assert result.event_kinds[-1] == "run.completed"
```

It writes a config into a temporary directory (sqlite store, the demo corpus
unless you give `items=` or `sources=`), builds a `Service` and validates it
as `serve` would, composes the run, dispatches it on the dispatch manager and
waits — with a timeout, after which it cancels and fails — until the runner
returned and the dispatch settled. The result is read from the store:
`run`, `dispatch`, `verdicts`, `live`, `events`, `artifacts`, `refusal`,
`warnings`, `logs()`. For results that arrive later: `harness.ingest(bundle,
run_id=…)` and `harness.wait(run_id)`; `harness.flush_observers()` before
reading what an observer wrote.

---

## 7. Packaging

```toml
[project]
name = "runcomposer-shop-connector"
dependencies = ["runcomposer>=0.2,<0.3"]

[project.entry-points."runcomposer.sources"]
http-checks = "shop_connector.checks_source:HttpChecksSource"

[project.entry-points."runcomposer.runners"]
http-checks = "shop_connector.http_runner:HttpCheckRunner"

[project.entry-points."runcomposer.observers"]
run-summary = "shop_connector.summary_observer:RunSummaryObserver"
```

Groups: `runcomposer.sources`, `.runners`, `.parsers`, `.stores`,
`.observers`. The entry-point name is what a config writes after `plugin:`.
The template is installed into a fresh environment next to runcomposer, its
tests run and an end-to-end `runcomposer run` made in CI (the
`connector-template` job; locally `python example/try_it.py`). A connector
lives in its own repository, depending on runcomposer — nothing of your
environment needs to appear in runcomposer for it to work
([ADOPTING.md §9](../ADOPTING.md#9-where-your-connector-lives)).

---

## 8. Four ways to execute

The same contract drives four structurally different executors. Each has a
runnable example whose own config file is run end to end by a test:

| | Executor | Example | End-to-end test |
|---|---|---|---|
| 1 | **In-process pool** — Robot Framework in worker processes on this machine; partitions, duration-balanced chunks, reruns, live status from the workers | [`examples/robot-pool/config.yaml`](../examples/robot-pool/config.yaml) | `tests/test_four_ways.py::test_1_in_process_pool_robot` |
| 2 | **Remote CI trigger** — a parameterized Jenkins job; `callback` (the job pushes its bundle with the ingest token) or `poll` (runcomposer follows the queue item to *its* build and downloads the results) | [`examples/jenkins/config.yaml`](../examples/jenkins/config.yaml), [`ci/jenkins/`](../ci/jenkins) | `tests/test_four_ways.py::test_2_…` (protocol stand-in), [`ci/jenkins/e2e.py`](../ci/jenkins/e2e.py) (a real Jenkins in docker, both modes, cancel, health) |
| 3 | **Local command template** — a command per shard; what it writes is read by the configured parser | [`examples/pytest-shop/config.yaml`](../examples/pytest-shop/config.yaml) | `tests/test_four_ways.py::test_3_local_command_pytest` |
| 4 | **Export / remote agent** — no runner: the document is exported, executed by the vendored single-file `runcomposer-exec` somewhere runcomposer never talks to, and the bundle returns by file drop | [`examples/remote-agent/`](../examples/remote-agent) | `tests/test_remote_agent.py` |

```bash
runcomposer run Cart-Basics --runner robot-pool --config examples/robot-pool/config.yaml
runcomposer run Cart --runner pytest --config examples/pytest-shop/config.yaml
python ci/jenkins/e2e.py
./examples/remote-agent/sync.sh
```

And a fifth, yours: the template's runner executes HTTP checks on a thread
pool in this process and delivers verdicts it built itself.

---

## 9. Known limits

Deliberate, each for a reason:

- **A taxonomy leaf holds one pattern, not a filter expression.** The tree
  hands the builder one pattern per leaf; combining is the filter builder's
  job, and a leaf carrying an expression would put selections — the part
  that drifts — into the file ([docs/taxonomy.md](taxonomy.md#known-limitation-one-pattern-per-leaf-and-only-a-pattern)).
- **No per-runner custom "actions" endpoint.** A runner offers `dispatch`,
  `cancel`, `health` and declared per-run parameters; an endpoint whose
  meaning only one runner knows (a pool reload, say) would be an unchecked,
  undocumented surface of the API. Do such operations in your own tooling.
- **No catalog cache shared across CLI invocations.** Each CLI command is its
  own process and parses the corpus once; a server reloads only what its
  `fingerprint` says changed. A cache file would be one more thing to go
  stale or be shared between deployments by accident.
- **No `source.root` in the run spec.** The document names items by id; where
  the corpus lives is the executing side's configuration. A path in the
  document would tie it to one checkout layout (the schema still accepts the
  field as optional provenance, and nothing reads it).

Edges you should know:

- **Short secrets are not redacted.** A value of fewer than four characters
  taken from the environment is not blanked out of recorded text, since
  blanking `4` would mangle ordinary text; a `:-default` written in the file
  is not a secret either. Links a runner records (`declare(links=…)`) are
  kept as given — `cancel` needs them — so do not put a secret into a link
  (`tests/test_config.py::test_values_shorter_than_four_characters_are_not_redacted`).
- **Restart recovery cannot tell a reused process id** from the process that
  owned a dispatch; `cancel --force` closes such a dispatch.
- **One instance owns a store.** The store refuses a second in-flight dispatch
  per run across processes, but runcomposer does not coordinate instances or
  queue across them (DESIGN.md §13).
- **Windows** has a process-liveness code path of its own and CI runs the
  suite there without blocking merges; it has not been seen green yet.
  Tests that need a POSIX shell skip there with that reason.
