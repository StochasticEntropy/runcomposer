# Writing a connector

A **connector** is what joins runcomposer to an environment: where the tests
come from, where they run, what their results look like, who is told when a
run ends. It is a Python package of plugins — or a single module named in a
config file — written against one import, `runcomposer.sdk` (plus one of its
two toolkits when it executes Robot Framework or drives Jenkins in a way of
its own), and tested with one more, `runcomposer.testing`.

Read [ADOPTING.md](../ADOPTING.md) first: most adoptions need configuration,
not code, and it says when you need which. This guide is for when you do
write code. Before writing a runner, read
[§9](#9-composing-the-reference-runners): the two reference runners may do
what you need once configured, and their building blocks are exported for
when they do not. The fastest start for everything else is to copy
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
[Composing the reference runners](#9-composing-the-reference-runners) ·
[Known limits](#10-known-limits)

---

## 1. The surface

| Import | When | What |
|---|---|---|
| `runcomposer.sdk` | at run time, in your plugins (worker processes included) | the protocols, the base classes, the records, the errors, the helpers, and `Service` / `load_config` for driving runcomposer from Python |
| `runcomposer.sdk.robot` | in a runner that executes Robot Framework its own way | the building blocks of `robot-pool`: exact-id selection, the live listener (local and remote), the shard worker and its supervisor, delivery and merging ([§9.2](#92-compose-when-configuration-is-not-enough)) |
| `runcomposer.sdk.jenkins` | in a runner that drives a Jenkins job its own way | `JenkinsClient`, the HTTP client of the `jenkins` runner, and the compressed form a document travels in |
| `runcomposer.testing` | in your tests | the conformance checks, `FakeDispatchContext`, `ConnectorHarness` |

All four keep their names and meaning within a minor line
(`runcomposer>=0.2,<0.3`); a removal is a breaking change listed in the
changelog. Their names are frozen in `tests/test_sdk.py::TestSurface` (the SDK
and both toolkits) and `tests/test_testing_kit.py::TestSurface`, so no change
to any of them is accidental. **Import nothing else of runcomposer** — every
other module may change in any release. runcomposer holds its own reference
plugins to that rule, and the toolkits to a stricter one: they import nothing
of runcomposer but `runcomposer.sdk`, and the `robot-pool` and `jenkins`
runners are built on them (`tests/test_self_containment.py`). That is the
evidence that the SDK is enough; the template carries the same guard for your
package (`examples/connector-template/tests/test_guard.py`).

Importing `runcomposer.sdk` loads the core only — no web framework, no
application layer, neither toolkit and not Robot Framework — so it is cheap
in a worker process; `runcomposer.sdk.robot` imports Robot Framework only
inside the functions that execute it, so it and `robot-pool` import without it
(`tests/test_sdk.py::TestLazyEmbedding`).

---

## 2. The ports and their base classes

Five roles, each a `typing.Protocol` in `runcomposer.sdk`, each with a base
class that supplies its optional members:

| Role | Base class | You write | The base supplies | Reference plugins |
|---|---|---|---|---|
| `TestSource` | `SourceBase` | `provider_id`, `items()`, `snapshot()`, `resolve()` | `fingerprint()` → `None`, `duplicate_ids` → `()` | `manifest`, `robotframework`, `demo-corpus` |
| `Runner` | `RunnerBase` | `describe()`, `dispatch(ctx)` | `plan(ctx)` (says it offers no preview), `cancel(ctx)` (nothing more to stop), `health()` → `unknown` | `robot-pool`, `jenkins`, `command`, `demo` |
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
`__init__`; connect to things in `dispatch`, `health`, `items`, `parse` —
never in `plan`, which previews a dispatch.

---

## 3. The runner contract

```python
from runcomposer.sdk import (
    DispatchContext, Parameter, PlanContext, PlanPreview, RunnerBase, RunnerInfo, Verdict,
)


class ShopRunner(RunnerBase):
    def __init__(self, base_url: str, workers: int = 4):   # the options of its config entry
        self.base_url, self.workers = base_url, workers

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="shop",
            title="Shop checks",
            capabilities=("cancel", "live_status", "plan"),
            parameters=(Parameter("region", kind="choice", choices=("eu", "us"), default="eu"),),
        )

    def _shards(self, ctx: PlanContext) -> dict[str, list[str]]:
        """Everything decided before anything runs: shared by plan() and dispatch()."""
        if ctx.source.snapshot() != (ctx.spec.get("source") or {}).get("snapshot"):
            ctx.refuse("the corpus changed since this run was composed — compose it again")
        return {ctx.parameters["region"]: list(ctx.item_ids)}

    def plan(self, ctx: PlanContext) -> PlanPreview:
        shards = self._shards(ctx)
        return PlanPreview(text=f"{len(ctx.item_ids)} check(s) in {list(shards)}", shards=shards)

    def dispatch(self, ctx: DispatchContext) -> None:
        shards = self._shards(ctx)
        ctx.declare(shards, plan=self.plan(ctx).text)
        for region, items in shards.items():
            verdicts = []
            for item_id in items:
                ctx.raise_if_cancelled()
                ctx.live(region, item_id, "RUNNING")
                verdicts.append(Verdict(item_id, "PASS", duration_ms=12))
            ctx.deliver(region, verdicts=verdicts)
```

The template's [`http_runner.py`](../examples/connector-template/src/shop_connector/http_runner.py)
is the same shape doing real work: HTTP requests on a thread pool, a plan
preview, live status, an artifact, health.

### 3.1 `describe()` — who it is and what a run may choose

`RunnerInfo` carries `id` (the plugin's name), `title`, `description`,
`capabilities`, `parameters` and `result_format`. When `result_format` is set,
a run composed for the runner expects that format back
(`results.expect[0].format`), so `ctx.deliver(shard, bundle=…)` without
`format=` parses it with that parser — leave it `None` for a runner that
delivers verdicts it built itself.
Capabilities with meaning to runcomposer and its clients: `live_status`,
`partitions`, `cancel`, `plan` (`plan(ctx)` answers with a real preview,
[§3.2](#32-planctx--a-preview-without-side-effects)), `external_delivery`
(results arrive by ingestion after `dispatch` returns), `simulated` (nothing
is really executed).

`parameters` are the **per-run options** whoever starts a run may set — a
`Parameter` has `name`, `kind`, `label`, `help`, `default`, `choices`,
`required` and `pattern`. Seven kinds:

| Kind | Value |
|---|---|
| `string` | one line of text |
| `text` | several lines (a form renders a text area) |
| `integer`, `boolean` | as named |
| `choice` | one of `choices` |
| `multi_choice` | a list drawn from `choices` |
| `mapping` | names to strings — name/value pairs, such as extra variables of a test framework |

`pattern` is an optional regular expression the whole value must match
(`re.fullmatch`): a `string`, the whole of a `text`, every *value* of a
`mapping`. The API lists the parameters (`GET /api/v1/runners`, with their
`pattern`), the UI renders a form from them, the CLI takes them as `--param
name=value`, and every submitted value is validated against them — an
unknown, missing, ill-typed or non-matching value is a `400` naming the
parameter, before a run exists (`tests/test_api.py::…per_run_parameters`,
`tests/test_dispatch.py::TestParameters`). Accepted values are written into
`runner.<id>` of the run's document.

**Options and parameters are two things.** `ctx.options` is the runner's
config entry as written — the options its constructor received, paths
anchored and `${env:…}` resolved — read-only, and nothing else.
`ctx.parameters` is what whoever started the run chose: validated, declared
defaults applied. A parameter never shows up in `ctx.options`, so it cannot
shadow an option of the same name; a runner with `stages` both as an option
(what exists) and as a parameter (what was chosen) reads each where it
lives (`tests/test_runner_contract.py::TestOptionsAndParameters`).

**A document cannot reconfigure your runner.** Of a spec's `runner.<id>`
section you see exactly the keys you declared; anything else is ignored with a
warning. Hooks, listeners, paths and credentials come from the config file
only (DESIGN.md §6.2).

The id a deployment dispatches to is the **config key**, not `RunnerInfo.id`:
one plugin may be configured several times (`examples/jenkins/config.yaml`
configures `jenkins` four times).

### 3.2 `plan(ctx)` — a preview without side effects

`plan(ctx)` answers "what would a dispatch of this run do?" — every item with the
shard it would land in — and does nothing else: no execution, no record, no trigger. It gets
a `PlanContext`: `run_id`, `runner_id`, `spec`, `item_ids`, `options`,
`parameters`, `source`, `history`, `refuse(reason)` and `export_spec()`. Each
of them is a member of `DispatchContext` too, with the same meaning, and
nothing else is there — no `declare`, `deliver`, `log`, `live`, artifact,
token, link or work directory. So one planning function serves both: `plan`
calls it and describes the result, `dispatch` calls it and acts on it (the
example above; the template's `_prepare`). What acts on something — booking
an environment, probing a server — stays in `dispatch`; say in the preview's
`notes` that it will happen.

It returns a `PlanPreview`:

| Field | What |
|---|---|
| `text` | the plan as people read it — the text the dispatch records with `declare(plan=…)` |
| `shards` | every shard the dispatch would declare → the item ids it would report; an empty list where the runner does not decide that (a job that distributes on its own) |
| `groups` | `{shard: label}`, as `declare(groups=…)` |
| `estimates_ms` | an estimated duration per shard, where history allows one |
| `notes` | what is worth knowing before starting: what a preview cannot check, a cold start |
| `data` | runner-defined, shown verbatim |

A plan may `ctx.refuse(reason)` for every reason the dispatch would; the
preview is then that refusal. A plan that raises is a refusal with the
exception as its reason. Declare the `plan` capability when you override it;
`RunnerBase.plan` says that it offers no preview.

Who asks, and what they get:

- `POST /api/v1/plan` `{selection, runner, parameters?, title?, labels?}`
  plans a selection; nothing is stored. `POST /api/v1/runs/{id}/plan`
  `{runner, parameters?}` plans a stored run, with its own parameters unless
  others are given, and leaves it untouched. The answer is `{runner, run_id,
  item_count, parameters, refused, refusal, plan, warnings}`, `plan` being
  `{text, shards: [{shard, group, count, item_ids, estimate_ms}], groups:
  [{group, shards, count, estimate_ms}], notes, data}` or `null`. A refusal is
  a `200` with `refused: true`. What a dispatch request is refused for up
  front — an unknown runner, an invalid parameter, a selection with nothing to
  execute — is a `400` here too, and so is a runner without the `plan`
  capability (`tests/test_runner_contract.py::TestPlanApi`, including
  `test_every_reference_runner_previews`).
- `runcomposer plan [FILTER | --run ID] --runner ID [--param k=v] [--items]
  [--json]` prints the same and exits `1` when the dispatch would be refused
  (`tests/test_runner_contract.py::TestPlanCommand`).
- In your tests, `check_runner` holds the preview to the dispatch that
  follows, `FakeDispatchContext.preview(runner)` calls it the way runcomposer
  does, and `ConnectorHarness.plan` asks a real deployment
  ([§6](#6-the-conformance-kit)).

### 3.3 `dispatch(ctx)` — prepare, declare, execute, deliver

`dispatch` runs off the request path, on the dispatch manager's bounded pool
(the CLI's `run` executes the same code in the foreground). It may block for
as long as the execution takes. One runner instance serves every dispatch of
its config key, possibly several at once: keep what belongs to one dispatch
on `ctx` or keyed by `ctx.dispatch_id`, never in plain attributes of `self`.
An in-process executor that `cancel(ctx)` must find again (a subprocess, a
pool) goes into a lock-protected mapping on `self` keyed by
`ctx.dispatch_id`, removed when `dispatch` returns; a remote one is found
through `ctx.links`, which also survive a restart.

| Phase | Calls | If you raise |
|---|---|---|
| **prepare** | read `ctx.item_ids`, `ctx.spec`, `ctx.options`, `ctx.parameters`, `ctx.source`, `ctx.history`; check drift, readiness; `ctx.refuse(reason)` | before `declare`: a **refusal** — no dispatch remains, the run is as it was, the reason is a `dispatch.refused` event and the answer to the request |
| **hand-off** | `ctx.declare(shards, plan=…, links=…, groups=…)` — exactly once | — |
| **execute** | `ctx.live(…)`, `ctx.set_link(…)`, `ctx.add_artifact(…)`, `ctx.log(…)`, `ctx.raise_if_cancelled()` | after `declare`: the dispatch is **FAILED** with the error text; whatever was delivered is kept |
| **deliver** | `ctx.deliver(shard, verdicts=… \| bundle=…)` per declared shard | |
| **return** | — | all declared shards delivered → **COMPLETE**; shards outstanding → **AWAITING_RESULTS** (they come by ingestion); returned without `declare` → recorded as a refusal |

The order is enforced, with the same code in runcomposer and in the kit's
fake context (`runcomposer.core.handoff`, `tests/test_testing_kit.py::TestFakeAndRealAgree`):

- `refuse()` after `declare()` → `RuntimeError` (the dispatch fails instead);
- a second `declare()` → `RuntimeError`;
- `deliver()` before `declare()` → `RuntimeError`;
- `declare("eu")` (one string) → `TypeError`; duplicate shard names → `ValueError`;
- `groups` naming a shard that was not declared → `ValueError`;
- `deliver()` with both or neither of `verdicts=` / `bundle=` → `TypeError`;
- a delivered verdict with status `MISSING` → `ValueError` (an executor reports
  `PASS`, `FAIL`, `SKIP` or `ERROR`; `MISSING` is derived).

**`declare(shards, *, plan="", links=None, spec_sha256=None, groups=None)`.**
`shards` is a list of names, or a mapping of names to the item ids each shard
will report — then an item a shard never reports is `MISSING` for that shard.
Completion is computed against the names you declared and the planned items:
a run with an unreported item is never `PASS`. `plan` is text shown with the
dispatch; `links` are named URLs — and how `cancel` finds your executor again
later, since they are stored as given. `groups` labels shards that belong
together (`{shard: label}` — the partition each chunk runs on): the run detail
then has a roll-up for every group next to the one for every shard (`groups`: declared and
delivered shards, counts, summary, live status, completion per group)
(`tests/test_runner_contract.py::TestShardGroups`).

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
streams one item's status while its shard executes: `RUNNING` when it starts,
then how it ended. `LIVE_STATUSES` is `RUNNING` and the four reportable
statuses. A live status is a `LiveStatus` record, kept apart from verdicts —
a `Verdict` cannot be `RUNNING`, and a delivery or a results push that claims
it is refused — shown flagged `live`, replaced per shard and item, and
cleared by the shard's delivery; the run detail sums it up in `live_summary`
(`{shard: {status: count}}`) and `GET /api/v1/runs/{id}/verdicts?status=RUNNING`
lists what is executing now (`tests/test_runner_contract.py::TestRunningAndWorkerLogs`).
It never invents an item or a status: an id that was not planned, or a status
outside `LIVE_STATUSES`, is ignored
(`tests/test_dispatch.py::…never_invents_an_item_or_a_status`). From a worker
process use `ctx.live_channel()`: a small picklable `LiveChannel` with the same
`live` method and a `log(message, level)` whose lines land in the run's event
stream like `ctx.log`'s. It carries no store — it appends to a file in
`ctx.workdir` that the dispatching process tails — so it works with any
`RunStore` (`tests/test_dispatch.py::TestLiveStatus`,
`tests/test_runner_contract.py::TestRunningAndWorkerLogs::test_a_worker_process_logs_into_the_event_stream`).

**Artifacts, private files, log.** `ctx.workdir` is a directory for this
dispatch under the artifact root, already created. A file there is **not**
downloadable because it is there: only recorded artifacts are served — a file
recorded with `ctx.add_artifact`, or any file under a directory recorded that
way (a report and the images it refers to). Everything else under
`/artifacts/` answers `404`, like a file that does not exist
(`tests/test_api.py::TestArtifactRoute`).
`ctx.add_artifact(name, path_or_url, media_type=…, shard=…, group=…)` records
a file, a directory or a link (an `http(s)` URL passes through as a link;
anything else is a local path) — of the dispatch, of one shard, or of a group
of shards. What must never be downloadable — a booking key, a console log
with credentials in it — goes into `ctx.private_dir`: a directory of the
dispatch outside the artifact directory (`core.private_dir`), created on first
use, removed with the run and swept by `runcomposer gc`
(`tests/test_runner_contract.py::TestOwnDispatch`).
`ctx.log(message, level="info")` appends a line to the run's event stream;
`level="warning"` makes it a warning event.

**Your own dispatch.** `ctx.set_link(name, url)` records a link only known
after the hand-off — the URL of the build a queued job became. Set before
`declare`, it joins the hand-off's links; after, it is stored at once (and
logged), and a context rebuilt for `cancel` carries it.
`ctx.delivered_shards()` is the set of shards that have delivered — by this
runner or by a push from outside — and `ctx.outstanding_shards()` the declared
shards that have not, in declaration order. **The dispatch may be complete
before the runner is done with it** — when the executor pushed results itself while `dispatch`
was still running: fetch and deliver what is outstanding, not everything you
declared (`tests/test_runner_contract.py::TestOwnDispatch`,
`tests/test_jenkins_toolkit.py::TestShards::test_a_shard_a_job_pushed_itself_is_not_downloaded_again`).

**External executors.** A runner that hands the work to something
runcomposer will not hear from again until results arrive (a CI job, a
remote agent) uses `ctx.export_spec()` — the document as the exact JSON bytes
to hand over, their hash recorded so a marker written from them verifies —
and `ctx.ingest_token()` — the token that executor pushes results with
(`POST /api/v1/runs/{id}/results`), bound to this dispatch, stored only as a
hash, `None` when the deployment disabled tokens. It declares its shards,
returns, and declares the `external_delivery` capability; the run waits in
`AWAITING_RESULTS` (`jenkins` with `completion: callback` does exactly this).

While it works, such an executor reports progress with the same token, sent
as `X-Runcomposer-Token` or `Authorization: Bearer`:

- `POST /api/v1/runs/{id}/live` `{dispatch?, updates: [{shard, item_id,
  status, duration_ms?, message?}]}` → `{dispatch_id, accepted, ignored,
  reason}`. An update for an item the run did not plan or a shard that has
  delivered is ignored; once the dispatch is over everything is, and
  `reason` says so.
- `POST /api/v1/runs/{id}/log` `{dispatch?, lines: [{message, level?}]}` →
  `{dispatch_id, accepted}`: the lines become `log` / `warning` events marked
  `source: push`.

A token reports for its own dispatch only (`403` otherwise)
(`tests/test_runner_contract.py::TestPushedProgress`). The client side ships
in three forms (`tests/test_runner_contract.py::TestTheRemoteChannelAndTheConsumer`):
`RemoteLiveChannel(callback_url, token, dispatch_id=…)` in the SDK is a
`LiveChannel` over HTTP — best-effort: what cannot be pushed is counted in
`failures`, never raised into the execution;
`runcomposer.sdk.robot.RemoteLiveStatusListener` is the Robot listener for an
execution runcomposer did not start (`robot --listener
runcomposer.sdk.robot.RemoteLiveStatusListener tests/`, configured from
`RUNCOMPOSER_CALLBACK_URL`, `RUNCOMPOSER_INGEST_TOKEN`, `RUNCOMPOSER_SHARD` and
`RUNCOMPOSER_DISPATCH_ID`); and `runcomposer-exec --callback URL [--token T]
[--push]` reports log lines (with `--simulate`, live status too) while it
works and with `--push` uploads its finished bundle, exiting `4` when the push
is refused.

**Results that never come.** A dispatch left in `AWAITING_RESULTS` waits for
ever unless the deployment says how long: with `core.dispatch.awaiting_timeout_s`
set, a dispatch that has waited longer — the clock restarts with every
delivery — becomes `FAILED` with a `run.failed` event carrying `expired`,
`waited_s`, `awaiting_timeout_s` and `outstanding_shards`. Results that arrive
late still complete it. A serving deployment applies the timeout on its own,
`runcomposer gc` once (report key `expired_dispatches`)
(`tests/test_runner_contract.py::TestAwaitingExpiry`).

**History.** `ctx.history.durations(labels=…, last_n=…, statuses=…)` gives the
mean duration per item over the last `last_n` dispatches that delivered;
`statuses=("PASS",)` counts only executions that ended that way (a test that fails
usually stops short of its normal length, which would skew a balance). An execution that
reported 0 ms is not counted: 0 means "unknown", to the store and to the
planner. `plan_chunks(item_ids, n, durations)` from the SDK cuts a
duration-balanced plan with it: longest first onto the lightest chunk, an
item without a known duration weighted with the *mean of the known ones* —
so a test that reports 0 ms counts as an average test, not as a free one —
and round-robin on a cold start (`ChunkPlan.balanced` says which). Both are empty on a fresh store — unless it
was seeded: `runcomposer history import FILE [--label k=v]` reads a CSV
(`item,duration_ms` or `item,duration_s`) or JSON (`{"durations": {item:
ms}}`, or a list of `{item, duration_ms}`) and keeps it as the answer for
items no execution has measured yet
([`examples/history/durations.csv`](../examples/history/durations.csv));
`runcomposer history durations [--label] [--last N] [--status S]` prints what
a planner sees (`tests/test_runner_contract.py::TestDurationHistory`).

### 3.4 `cancel(ctx)` and `health()`

Cancellation is cooperative first: runcomposer sets `ctx.cancelled`, then
calls your `cancel(ctx)` from another thread while `dispatch` may still run.
Check `ctx.cancelled` / call `ctx.raise_if_cancelled()` between units of
work; stop processes or remote builds in `cancel`. The dispatch becomes
`CANCELLED` when `dispatch` returns. A dispatch that only awaits results
(your `dispatch` returned long ago) is cancelled at once: runcomposer calls
`cancel` with a context rebuilt from the stored record — `ctx.links`, those
given to `declare` and those set with `set_link`, are how you find the build;
nothing you kept in memory survives into such a context — and closes the
dispatch (`tests/test_dispatch.py::TestCancel`,
`tests/test_jenkins.py::…awaits_results`). A cancel requested by another
process — `runcomposer cancel` against a server, or against a
`runcomposer run` in another shell — reaches the process that executes the
dispatch within half a second, and your `cancel(ctx)` is called there
(`tests/test_dispatch.py::TestCancel::test_a_cancel_from_another_process_reaches_a_foreground_dispatch`). Declare the `cancel` capability when you
honour it.

`health()` answers "could you take a dispatch right now?" with a
`Health(status="ok" | "degraded" | "down" | "unknown", detail=…, data=…)`.
It is cached for 30 s in the runner list and shown by `runcomposer doctor`;
`data` is yours, printed by `runcomposer doctor -v`, where a list of
`{check, ok, detail}` reads as a checklist ([§5](#5-configuration)).

### 3.5 What a refusal means to the person who started the run

A refusal that happens within `core.dispatch.handoff_wait_s` (default 2 s)
of the request is the request's answer (`409 dispatch-refused`), and a run
that request composed is removed again unless it asked for `keep_run`. The
answer to a dispatch request says `handoff: done` or `handoff: pending`; a
refusal after a `pending` answer leaves the run — which the answer has named
— `COMPOSED`, with the reason as a `dispatch.refused` event. A run an answer
has named is never removed implicitly (`tests/test_api.py::TestRefusedDispatch`).
So refuse early and say in the reason what to do about it — and let `plan`
refuse for the same reasons, so that a preview shows it first.

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
path). **The format id a run asks for is looked up by name**: a `parsers:`
entry with that key, else the `runcomposer.parsers` entry point of that name.
A parser without options needs no `parsers:` entry. A `parsers:` entry may
configure a class under a format id of your own (`shop-junit: {module:
…JunitXmlParser, pattern: "shop*.xml"}`); the key is then the name, not the
class's `format_id`. `parse(path)` takes a file or a directory and returns
`ParsedVerdict(native_name, status, duration_ms=0, message="", attempt=1)`:
the native name exactly as the artifact spells it — never reconstruct ids
here. Bad input is a `ParseError` and nothing else. Read XML through
`parse_xml(path)` (or call `forbid_dtd(path)` first): a document with a DTD
is refused wherever the declaration sits. A parser with options is configured
under `parsers:`, keyed by its format id.

**The reference format, `runcomposer-verdicts`,** is what an executor writes
when it has no native format worth a parser — one JSON document:

```json
{"format": "runcomposer-verdicts",
 "verdicts": [
   {"name": "Shop.Checkout.Express.T001", "status": "PASS", "duration_ms": 420},
   {"name": "Shop.Checkout.Express.T002", "status": "FAIL", "duration_ms": 95,
    "message": "the gateway refused the card", "attempt": 1}
 ]}
```

`format` and `verdicts` are required; of an entry, `name` (the native name,
resolved through `TestSource.resolve` like any other) and `status` (`PASS`,
`FAIL`, `SKIP` or `ERROR`) are; `duration_ms` (default 0), `message` and
`attempt` (default 1) are optional, other keys are ignored. A bundle is that
file, or a directory holding one or more of them — JSON files in it that are
not this format are skipped (`tests/test_parsers.py`).

### `RunStore` — persistence

The widest port and rarely needed (`sqlite` ships). The store records; it
decides nothing — lifecycle state is computed by the core. Every method must
be thread-safe; `record_delivery` must be atomic per `(run, dispatch, shard)`
across processes; `add_dispatch(exclusive=True)` must refuse with `RunActive`
when the run's latest dispatch is in flight, checked in the same transaction
as the insert (that is what keeps two processes from both dispatching one
run); `latest_events` is a bounded read of the tail. Beside runs, dispatches
and deliveries it keeps a dispatch's shard groups (`update_dispatch(shard_groups=…)`),
an artifact's group (`add_artifact_ref(…, group=…)`), live status as
`LiveStatus` records, `RUNNING` included (`record_live_verdict`,
`live_verdicts`), and imported durations: `seed_durations` stores them,
`duration_aggregates(…, statuses=…)` answers with a measured mean over
durations above 0 and falls back to the seed for an item nothing was measured
for. `check_store` runs all of it, concurrency included, against your
implementation.

### `RunObserver` — the completion hook

`notify(event)` is called on a thread of its own, in event order, for the
kinds its config entry selects (`events:`, `*` wildcards; default: the
lifecycle events). Whatever it raises is recorded on the run as a warning and
changes nothing else (`tests/test_observers.py::TestAnObserverNeverAffectsARun`).
A `RunEvent` has `seq`, `run_id`, `at`, `kind`, `dispatch_id`, `data`.

---

## 5. Configuration

One file, chosen with `--config`; relative paths in it resolve against its
own directory. Left out, `store:` is a sqlite file `runcomposer.db`,
`core.artifact_dir` is `artifacts/` and `core.private_dir` is `private/`, all
next to the file; the private directory must lie outside the artifact
directory, or the configuration is refused
(`tests/test_runner_contract.py::TestOwnDispatch::test_a_private_directory_inside_the_artifact_directory_is_a_config_error`).
The taxonomy is the bundled demo one until you set `core.taxonomy_file`
(`doctor` then reports its leaves against your catalog — see
[docs/taxonomy.md](taxonomy.md)).

```yaml
core:
  dispatch: { awaiting_timeout_s: 86400 }  # close dispatches whose results never came
sources:                       # one or more → one catalog
  shop-ui:  { plugin: robotframework, root: "${env:SHOP_CHECKOUT}/ui/tests" }
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
ui:                            # selections people start from
  default_filter: { not: "prefix:Quarantine-" }
  quick_filters:
    - Checkout
    - { label: Stable payments, filter: { op: AND, items: [Payments, { not: "prefix:Quarantine-" }] } }
  presets:
    - { name: release-gate, label: Release gate, filter: { op: OR, items: [Checkout, Payments-Cards] },
        runner: shop, parameters: { environment: staging }, labels: { suite: release } }
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
  exceptions (the sqlite store leaves `:memory:` alone). A `${env:…}` inside a
  path is substituted *before* the path is anchored: `root:
  "${env:SHOP_CHECKOUT}/ui/tests"` with an absolute value stays where the
  variable points, a relative value is anchored like any other path — in
  plugin `PATH_OPTIONS` and in the core's own paths (`artifact_dir`,
  `private_dir`, the inbox, the quarantine, `taxonomy_file`)
  (`tests/test_deployment_options.py::TestPathsFromTheEnvironment`). A path
  inside a run spec is never rewritten.
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
  blanked out first (`tests/test_hardening.py`). See [§10](#10-known-limits)
  for the edges of that.
- **Dispatching.** `core.dispatch` holds `max_concurrent` (default 2
  dispatches at once), `handoff_wait_s` (default 2 s,
  [§3.5](#35-what-a-refusal-means-to-the-person-who-started-the-run)) and
  `awaiting_timeout_s` (default `null`: a dispatch awaiting results never
  expires — [§3.3](#33-dispatchctx--prepare-declare-execute-deliver)).
- **Selections people start from.** `ui:` carries three that runcomposer
  validates at startup: `quick_filters` — `{label, filter}` entries whose
  `filter` is one pattern or a whole filter expression (a bare string is
  shorthand for a pattern labelled by itself); `default_filter` — the
  expression a new selection starts from; `presets` — named, complete
  starting points `{name, label?, description?, filter, runner?,
  parameters?, title?, labels?}`, whose runner must be configured and whose
  parameters must validate against it. They are served normalized — each with
  `tag_filter`, `text` and `kind` — by `GET /api/v1/ui-config`,
  `GET /api/v1/meta` and `GET /api/v1/presets`; any other key under `ui:` is
  served as it is (`tests/test_deployment_options.py::TestUiSelections`).
- **Several entries of one plugin** are independent instances; `runcomposer
  doctor --config …` lists what is installed and what is configured, with the
  distribution and version each class comes from, and every runner's health;
  `runcomposer doctor -v` (or the global `-v`) adds each runner's health
  `data`, a list of `{check, ok, detail}` printed as a checklist
  (`tests/test_deployment_options.py::TestDoctorDetails`).

---

## 6. The conformance kit

`runcomposer.testing` runs the contract against your plugins, from any test
runner (it never imports pytest). Every reference plugin of runcomposer is
tested with it (`tests/test_testing_kit.py`), and a deliberately broken toy
per rule is caught with its rule named (`tests/test_testing_kit.py`,
`tests/test_kit_additions.py`).

### The checks

```python
from runcomposer.testing import (
    FakeDispatchContext, check_observer, check_parser, check_runner, check_source, check_store,
)

items = check_source(MySource("catalog.yaml"), native_names={"Cart.Total": "shop.cart.total"},
                     rebuild=lambda: MySource("catalog.yaml"))
parsed = check_parser(MyParser(), "fixtures/good-bundle", expected={"Cart.Total": "PASS"},
                      bad_bundles=["fixtures/truncated.xml", xml_with_dtd(tmp_path / "dtd.xml")])
ctx = check_runner(MyRunner(...), FakeDispatchContext.from_source(source, "Checkout"),
                   expect="complete")
check_runner(MyRunner(...), FakeDispatchContext.from_source(source),
             cancel_after_declare=True, cancel_within=5, expect="cancelled")
check_store(lambda: MyStore(tmp_path / f"{uuid4()}.db"))   # a fresh, empty store per call
check_observer(MyObserver(...), seen=lambda: received)      # what your destination got so far
```

Each raises `AssertionError` whose message begins with the violated rule:

```text
[runner.delivers-declared-shards] delivered shard 'eu', which was not declared (['us'])
[runner.plan-matches-dispatch] plan() previewed the shards ['all'], the dispatch declared ['eu', 'us'] — a preview shows what the dispatch does
[source.resolve-own-ids] resolve('Shop.Cart.Core.T001') returned 'shop.cart.core.t001', not the id itself
[parser.bad-input-is-parse-error] truncated.xml: raised KeyError (…) — bad input is a ParseError and nothing else
```

| Check | Rules (abridged) |
|---|---|
| `check_source` | contract members; `provider_id`; items are `Item`s with string tags, stable across calls; `duplicate_ids` lists exactly the duplicated ids; snapshot and fingerprint stable; every id resolves to itself; an unknown name resolves to `None`; the native names you give resolve; a rebuilt instance agrees |
| `check_parser` | contract; `format_id` is a name; `parse` returns `ParsedVerdict`s with reportable statuses, non-negative durations, attempts ≥ 1; deterministic; non-empty; `expected` statuses; every bad bundle is a `ParseError` |
| `check_runner` | contract; `describe` well-formed, parameter defaults validate; `health` well-formed; the dispatch keeps the hand-off order; declares at least one shard; delivers only declared shards and only planned items; a `complete` dispatch reports every planned item under names the catalog resolves (`allow_missing=True` for a runner that reports partially on purpose); ends as `expect`ed; outstanding shards only with `external_delivery`; with `cancel_after_declare`, returns within `cancel_within` seconds of the cancel and does not go on to deliver every shard. With the `plan` capability, first a preview: a `PlanPreview` listing only planned items and labelling only its own shards (`runner.plan`), that declares, delivers, records, exports and logs nothing and reaches for nothing a `PlanContext` lacks (`runner.plan-is-read-only`), and that names exactly the shards the dispatch then declares (`runner.plan-matches-dispatch`); a preview that refuses is accepted |
| `check_store` | contract; runs, documents, paging/filtering/search; dispatches incl. the exclusive claim and shard groups (`store.shard-groups`); spec bytes; token hashes; the delivery idempotency contract; shard and attempt on read-back; live status, `RUNNING` included (`store.live-running`); artifacts, with their group; the event cursor and `latest_events`; history queries, restricted by status (`store.duration-statuses`) and falling back to seeds (`store.duration-seeds`); delete and prune; concurrent deliveries leave exactly one |
| `check_observer` | contract; every event kind accepted, `None` returned; concurrent calls; with `seen`, every notification arrives exactly once |

### `FakeDispatchContext` — unit tests for a runner

A recording, in-memory `DispatchContext`. Give it the planned items (ids;
`items=` with tags and `meta`; or a whole `spec=`), `source=` (a runner that
reads `item.meta` from `ctx.source` needs the real one), `options=` and
`parameters=` — each on its own, as in runcomposer, a parameter never showing
up in `options`; once the fake sees the runner, the parameters are validated
against its declaration and its defaults applied, as for a real run
(`tests/test_kit_additions.py::TestTheFake::test_parameters_are_validated_and_defaulted_for_the_runner`)
— history `durations=`, `parsers=` (format id →
parser), `expect_format=`; run your runner; read what it did. Unless you say
otherwise, the document expects the runner's own `result_format` once the fake
sees the runner (`ctx.run`, `ctx.preview`, `check_runner`), as a run composed
for it would:

```python
ctx = FakeDispatchContext(["shop.cart.add", "shop.checkout.page"], parameters={"environment": "qa"})
assert ctx.run(MyRunner(...)) == "complete"          # or refused / undeclared / cancelled / awaiting
assert ctx.declared == ("qa",) and ctx.summary() == {"PASS": 2}
assert [s.status for s in ctx.live_updates if s.item_id == "shop.cart.add"] == ["RUNNING", "PASS"]
assert ctx.live_now == [] and ctx.outstanding_shards() == [] and ctx.refusal is None
ctx.deliveries, ctx.shard_groups, ctx.links, ctx.artifacts, ctx.logs, ctx.exported, ctx.tokens
```

- **How `run` ends.** `ctx.run(runner)` returns `complete`, `awaiting`,
  `refused`, `undeclared` or `cancelled`. Any other exception the runner
  raises comes out of `run` — there is no "failed" return: in runcomposer one
  raised after the hand-off is a `FAILED` dispatch, and a test should see it.
- **What is recorded.** `ctx.logs` is a list of `(level, message)` pairs —
  the runner's `ctx.log` lines and what its workers logged through the
  channel; `ctx.warnings()` the messages of the warnings among them.
  `ctx.history.calls` records every `durations(...)` call with its `labels`,
  `last_n` and `statuses` — the way to assert in a unit test that a plan was
  balanced by passed executions only.
- **The live stream, two ways.** `live_updates` is everything the runner
  streamed, in order — `RUNNING` included, and kept after the shard delivered:
  the whole stream, for asserting what a runner reported while it ran.
  `live_now` is what a store holds at this moment: the latest status per shard
  and item, nothing for shards that have delivered
  (`tests/test_kit_additions.py::TestTheFake`).
- **Worker processes.** `ctx.live_channel()` is the real file transport: what
  a worker writes shows up in `live_updates`, its log lines in `logs`.
- **Your own dispatch.** `delivered_shards()` and `outstanding_shards()` are
  methods, as on the real context (`outstanding` is the same list as an
  attribute, for assertions); `set_link`, `shard_groups`, `private_dir` (a
  directory of its own beside `workdir`, removed by `close()`) and
  `spec_bytes()` (the document as `export_spec` would hand it out, without
  recording that it did) are there too.
- **A preview.** `ctx.preview(runner)` calls `runner.plan` the way
  runcomposer does — with the members of `PlanContext` only, so a plan that
  reaches for `declare` or `log` fails here as it would there — and returns
  the `PlanPreview`, or `None` with the reason in `ctx.refusal`. Nothing of
  the fake is touched.
- **Cancelling.** `ctx.request_cancel()` does what a cancel does.

**A runner that looks at the catalog itself** — where an item comes from,
what its `Item.meta` holds — needs real items, and gets them with
`FakeDispatchContext.from_source(source, selection=None, **kwargs)`: the
planned items are the source's own `Item` objects, `ctx.source` is the source,
and the document carries its snapshot, so there is no drift. `selection` is
`None` for every item, a filter expression (`"Smoke"`, `{"op": "AND", …}`) or a
list of ids (an id the source does not have is a `ValueError`); everything
else goes to the constructor:

```python
source = HttpChecksSource("example/checks.yaml")
runner = HttpCheckRunner(connector_settings={"environments": {"local": shop_url}})
ctx = FakeDispatchContext.from_source(source, "Smoke", parameters={"environment": "local"})
assert ctx.preview(runner).shards == {"local": ["shop.home", "shop.cart.add"]}
assert ctx.run(runner) == "complete"
```

The hand-off order is enforced by the same code as in runcomposer, so the
same misuse raises the same error with the same message
(`TestFakeAndRealAgree`).

### `ConnectorHarness` — a real runcomposer around your plugins

```python
with ConnectorHarness(
    sources={"checks": {"plugin": "http-checks", "path": str(CHECKS)}},   # an absolute path
    runners={"shop": {"plugin": "http-checks"}},
    connectors={"shop-checks": {"environments": {"local": "${env:SHOP_URL}"}}},
    env={"SHOP_URL": url},
) as harness:
    assert harness.plan("Smoke", runner="shop").preview.shards     # nothing is stored
    result = harness.run("Smoke", runner="shop", timeout=60)
    assert (result.state, result.completion) == ("COMPLETE", "PASS")
    assert result.event_kinds[-1] == "run.completed"
```

It writes a config into a temporary directory — so relative paths in the
entries you pass resolve against *that* directory: give absolute ones, or
pass `directory=` (a directory of yours the config is then written into, and
kept) to test that a path option really resolves against the config file —
(sqlite store, the demo corpus unless you give `items=` or `sources=`), builds a `Service` and validates it
as `serve` would, composes the run, dispatches it on the dispatch manager and
waits — with a timeout, after which it cancels and fails — until the runner
returned and the dispatch settled. The result is read from the store:
`run`, `dispatch`, `verdicts`, `live` (`LiveStatus` records), `links`,
`events`, `artifacts`, `refusal`, `warnings`, `logs()`. For results that
arrive later: `harness.ingest(bundle, run_id=…)` and `harness.wait(run_id)`;
`harness.flush_observers()` before reading what an observer wrote.
`harness.plan(selection | run_id=…, runner=…, parameters=…)` asks for a
preview as `runcomposer plan` does and returns the outcome (`.preview` or
`.refusal`).

**Meeting a dispatch while it executes.** `harness.start(...)` composes (or
takes a stored `run_id=`) and dispatches in the background, returning a
`DispatchHandle` at once:

```python
handle = harness.start("Regression", runner="shop")
mid = handle.wait_until(lambda run: any(s.status == "RUNNING" for s in run.live), timeout=30)
handle.cancel()                                   # as a person would: runcomposer cancel
assert handle.wait(timeout=30).state == "CANCELLED"
```

`wait_until(predicate, timeout)` reads the run from the store until
`predicate(run)` is true and returns that reading — `lambda run: run.live`
for "a test has started", `lambda run: "build" in run.links` for "the build is
known" — and fails with an `AssertionError` saying where the run stood;
`wait()` waits for the runner to return, as `run` does; `cancel()`, `read()`,
`done` and `refusal` are there too (`tests/test_kit_additions.py::TestTheHarnessInTheBackground`,
`examples/connector-template/tests/test_connector.py::test_a_check_is_seen_running_and_the_run_cancelled_in_a_real_deployment`).

**An executor that pushes.** `ConnectorHarness(serve=True, …)` also serves the
HTTP API of the deployment on a free port of 127.0.0.1: `harness.url` is its
base address, `harness.api` the API root. The address is known before the
plugins are built, so an entry refers to it as
`${env:RUNCOMPOSER_HARNESS_URL}` — a `callback_base`, say — and an executor
that pushes results or progress is tested against the real endpoints
(`tests/test_kit_additions.py::TestTheServingHarness`).
`harness.artifacts(run_id)` lists a run's artifacts as the API does — `name`,
`kind`, `shard`, `group`, `href` — so the address of a served file is
`harness.url + href`, and a test can ask for it, and for a file beside it
that was not recorded.

**A stand-in for Jenkins.** A runner that drives Jenkins — your own, built on
`runcomposer.sdk.jenkins`, or the reference one with your configuration — is
tested against `runcomposer.testing.jenkins.FakeJenkins`: a real HTTP server
on a local port speaking the protocol such a runner uses (the crumb and its
session, `buildWithParameters` → queue item → build, a queue item that can be
forgotten, stop and cancel, archived artifacts and anything else below a
build's URL, Basic auth, injected faults and stalled answers).

```python
from runcomposer.testing.jenkins import Build, FakeJenkins

def nightly(build: Build) -> None:            # what the job "does", on a thread per build
    regions = build.params["REGIONS"].split(",")
    build.pages["consoleText"] = "\n".join(f"=== results {r} ===\n…\n=== end ===" for r in regions).encode()
    build.artifacts["reports/junit.xml"] = b"<testsuite/>"     # archived files, by relative path
    # build.result = "UNSTABLE"; a job that holds waits on build.stop

fake = FakeJenkins()                          # auto_start=False: builds stay queued until fake.start(n)
fake.add_job("shop-nightly", nightly)
try:
    ...                                       # your runner against fake.url
    fake.triggers()[0].form                   # the build parameters it sent
    fake.builds[("shop-nightly", 1)].stop.is_set()
finally:
    fake.close()
```

It is the stand-in runcomposer's own `jenkins` runner is tested with
(`tests/test_jenkins.py`, `tests/test_kit_additions.py::TestSmallHelpers::test_the_jenkins_stand_in_is_part_of_the_kit`);
what no stand-in covers is a real server's own behaviour.

**A reference plugin beside yours.** `plugin(section, name, **options)` from
the kit builds an installed plugin by the name a config file gives it,
without importing its module — the Robot source as the catalog of a fake:

```python
from runcomposer.testing import FakeDispatchContext, check_runner, plugin

source = plugin("sources", "robotframework", root="tests/suites")
check_runner(LaneRunner("tests/suites", ["staging", "preview"]),
             FakeDispatchContext.from_source(source, "Checkout"), expect="complete")
```

**What `check_runner`'s cancel does and does not meet.**
`cancel_after_declare=True` cancels the moment the runner has declared — for
a runner that starts worker processes, before any of them runs. It checks
that the runner stops; to cancel *in the middle of an execution*, use
`harness.start(...)` and `wait_until` a `RUNNING` status, as above.

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
| 1 | **In-process pool** — Robot Framework in worker processes on this machine; partitions run once each or as lanes the selection is split over, duration-balanced chunks (each a shard of its own if you like), reruns, live status from the workers | [`examples/robot-pool/config.yaml`](../examples/robot-pool/config.yaml) | `tests/test_four_ways.py::test_1_in_process_pool_robot`, `::test_1b_the_pool_configured_as_lanes` |
| 2 | **Remote CI trigger** — a parameterized Jenkins job; `callback` (the job pushes its bundle with the ingest token) or `poll` (runcomposer follows the queue item to *its* build and downloads the results); one build may report several shards | [`examples/jenkins/config.yaml`](../examples/jenkins/config.yaml), [`ci/jenkins/`](../ci/jenkins) | `tests/test_four_ways.py::test_2_…`, `::test_2b_…` (protocol stand-in), [`ci/jenkins/e2e.py`](../ci/jenkins/e2e.py) (a real Jenkins in docker: both modes, a fan-out job polled and pushing, plan, cancel, health) |
| 3 | **Local command template** — a command per shard; what it writes is read by the configured parser | [`examples/pytest-shop/config.yaml`](../examples/pytest-shop/config.yaml) | `tests/test_four_ways.py::test_3_local_command_pytest` |
| 4 | **Export / remote agent** — no runner: the document is exported, executed by the vendored single-file `runcomposer-exec` somewhere runcomposer never talks to, and the bundle returns by file drop | [`examples/remote-agent/`](../examples/remote-agent) | `tests/test_remote_agent.py` |

```bash
runcomposer run Cart-Basics --runner robot-pool --config examples/robot-pool/config.yaml
runcomposer plan Cart --runner robot-lanes --config examples/robot-pool/config.yaml
runcomposer run Cart --runner pytest --config examples/pytest-shop/config.yaml
python ci/jenkins/e2e.py
./examples/remote-agent/sync.sh
```

And a fifth, yours: the template's runner executes HTTP checks on a thread
pool in this process and delivers verdicts it built itself.

---

## 9. Composing the reference runners

Most of what a Robot Framework or Jenkins connector needs is in the two
reference runners already. Configure them first; when that is not enough,
compose your own runner from the same building blocks instead of restating
them.

### 9.1 Configure before you code

**`robot-pool`** (`src/runcomposer/plugins/robot_pool.py`). Beside the
options in [ADOPTING.md §2](../ADOPTING.md#2-start-with-configuration-not-code)
(`suite_root`, `partitions`, `variables`, `pre_run_hooks`, `listener`, …):

| Option | What it does |
|---|---|
| `partition_mode: each \| split` | `each` (default): the selection runs once *per* chosen partition — the same tests against every environment. `split`: every test runs *once*; the chosen partitions are parallel lanes the selection is distributed over, balanced by duration history |
| `max_tests_per_chunk: N`, `chunk_shards: true` | a chunk is executed in pieces of at most `N` tests, one after another. With `chunk_shards` every piece is a declared shard of its own, `<lane>-NN`, delivered as soon as it is done instead of merged into one result per chunk; shards are grouped by partition. `chunk_shards` without `max_tests_per_chunk` is a `ConfigError` |
| `pre_dispatch: ["pkg.mod:callable"]` | each is run in the dispatching process, before the hand-off, as `callable(ctx, info)`; `info` is `{partitions, parameters, shards, groups, plan, item_ids, variables, workdir}`. It may `ctx.refuse(…)` — nothing has been handed off — `ctx.log`, and keep files in `ctx.private_dir`; a mapping it returns is added to the Robot variables of every shard |
| `worker_setup: ["pkg.mod:callable"]` | called in each worker process before its first execution as `callable(info)`; `info` is `{shard, partition, run_id, dispatch_id, out_dir, variables}`; a mapping it returns is added to that shard's variables |
| `run_parameters: [{name, kind, …}]` | further per-run parameters, declared with the fields of `Parameter` — beside the runner's own `partitions` and `allow_drift` |
| `parameter_variables: {param: VARIABLE}` | a per-run parameter as a Robot variable (a list comma-joined); `"*"` for a `mapping` parameter makes each of its entries a variable |
| `rpa: true \| false \| null` | the execution mode. `null` (default) lets Robot decide — and Robot refuses a selection that keeps both tests and tasks, so a root that mixes them needs `false` (everything executed as tests) or `true`; otherwise the dispatch fails with Robot's own message and that hint |
| `robot_options: {…}` | further keyword options for `robot.run` (`{loglevel: DEBUG, variablefile: [env.py]}`); the options the runner sets itself win |
| `history_statuses: [PASS]` | balance by the durations of executions that ended with these statuses only |

What it does on its own: `RUNNING` streams when a test starts, workers log
their progress into the run's event stream, each shard's directory is
recorded as a directory artifact `files (<shard>)` so that what a log refers
to is servable, and outputs of several executions are merged without "added
from merged output" notes. Its `plan` preview shows lanes, shards, groups and
estimates, and notes the readiness steps (`pre_run_hooks`, `pre_dispatch`)
that only a dispatch runs. Its health `data` carries `robot_version`,
`max_workers`, `suite_roots`, `partitions` and `partition_mode`. Pinned by
`tests/test_robot_toolkit.py` (`TestPartitionModeSplit`, `TestChunkShards`,
`TestPreDispatchAndRunParameters`, `TestExecutionMode`, `TestPlanPreview`,
`TestTheWorker::test_robot_options_reach_the_execution_and_the_toolkits_own_win`)
and `tests/test_robot_pool.py::TestWorkerSetup`. The example's second entry,
run end to end by `tests/test_four_ways.py::test_1b_the_pool_configured_as_lanes`:

```yaml
  robot-lanes:
    plugin: robot-pool
    suite_root: ../robot-shop/tests
    max_workers: 2
    partitions: [eu, us]
    partition_mode: split
    max_tests_per_chunk: 2
    chunk_shards: true
    run_parameters:
      - { name: variables, kind: mapping, label: "Extra variables" }
      - { name: browser, kind: choice, choices: [firefox, chromium], default: firefox }
    parameter_variables: { variables: "*", browser: BROWSER }
    history_statuses: [PASS]      # balance by how long tests take when they pass
```

**`jenkins`** (`src/runcomposer/plugins/jenkins.py`). Beside `base_url`,
`job`, `completion`, `callback_base` and the credentials:

| Option | What it does |
|---|---|
| `parameters: {NAME: template}` | the build parameters of *your* job. Fields: the document as `{spec_b64}` (the exact document, gzipped, as base64 — one line of ASCII that survives any form or shell; `base64 -d \| gunzip` restores it, `decode_spec` in Python), `{spec_url}` (where the job downloads the exact bytes; needs `callback_base`) or `{spec_json}` (as text — small documents only); `{run_id}`, `{dispatch_id}`, `{title}`, `{labels[key]}`; `{callback_url}`, `{token}`; `{item_ids}` (one per line), `{shards}` (comma-joined), `{params[name]}` (a list comma-joined, a `mapping` as `name=value` lines) |
| `shards` | what the build reports separately: a list of names, or a template rendering to a comma- or newline-separated list (`"{params[regions]}"`); default `["1"]`. A template that renders to nothing, or to duplicates, refuses the dispatch |
| `artifact_paths`, `result_urls` | with `completion: poll`, what is downloaded per shard: archived files matching `artifact_paths` (globs), and paths below the build's URL listed in `result_urls` (`consoleText`, `robot/report/output.xml` — what a publisher serves without archiving it). `{shard}` in either tells several shards' results apart. `artifact_paths` defaults to `["results/*"]` only when no `result_urls` are given. Several shards with `poll` and neither `{shard}` nor a `postprocess` are refused before the trigger |
| `postprocess: "pkg.mod:callable"` | called as `callable(ctx, shard, directory) -> Path \| None` over what was downloaded for one shard, before parsing; the path it returns is what is delivered, `None` delivers the directory as it is |
| `field_providers: {name: "pkg.mod:callable"}` | each called as `callable(ctx, fields) -> str` with the fields computed so far; its text is the template field `{name}`. It may `ctx.refuse` before anything is triggered. It is called for a plan preview too, with a `PlanContext` — so it must be free of side effects |
| `max_parameter_bytes`, `max_request_bytes` | a trigger with one build parameter over `max_parameter_bytes` (default 128 000) or a whole request over `max_request_bytes` (default 1 000 000) is refused before anything is sent, with a message naming the way out (`{spec_b64}`, `{spec_url}`, or a higher limit); `null` lifts a limit |
| `run_parameters: [{name, kind, …}]` | the per-run parameters, declared with the fields of `Parameter` |

How it cancels: once a polled build has started, its URL is recorded as the
link `build` (`ctx.set_link`) and `cancel` stops that build directly — also
from a context rebuilt after a restart. Before that, `cancel` follows the
queue item to the build it became — by the queue id the build records when
Jenkins has already forgotten the queue item, as it does minutes after the
build started — and takes an item that is still queued out of the queue. A
cancel that arrives while the trigger request is under way finds nothing to
stop yet: the runner, back from the trigger, takes the build back itself, and
the dispatch ends `CANCELLED`. With `poll`, a shard the job pushed itself while
it was polled is not downloaded again. Its `plan` preview shows the build
parameters that would be sent — long values as `‹N bytes›`, the token and the
dispatch id as `‹at dispatch›` — and triggers nothing; its health `data`
carries `version`, `job_url`, `completion` and a `checks` list for
`doctor -v`. Pinned by `tests/test_jenkins_toolkit.py`
(`TestTheScenarioByConfiguration`, `TestShards`,
`TestTheDocumentAsABuildParameter`, `TestCancelBeyondTheQueueItem`) against
the protocol stand-in. The example's fan-out entries — one build, one shard
per region — with their two callables in
[`examples/jenkins/shop_job.py`](../examples/jenkins/shop_job.py):

```yaml
  jenkins-fanout:
    plugin: jenkins
    base_url: "${env:JENKINS_URL:-http://localhost:8080}"
    job: runcomposer-fanout
    completion: poll
    run_parameters:
      - { name: regions, kind: multi_choice, choices: [eu, us, apac], default: [eu, us] }
    shards: "{params[regions]}"
    field_providers: { include: "shop_job:include_tags" }
    parameters:
      SPEC_B64: "{spec_b64}"          # the exact document, gzipped, as base64
      REGIONS: "{shards}"
      INCLUDE: "{include}"            # computed from the selection
      DISPATCH_ID: "{dispatch_id}"
    result_urls: [consoleText]        # below the build's URL; nothing is archived
    postprocess: "shop_job:results_from_console"
  jenkins-fanout-push:                # the same job, pushing each region's bundle itself
    plugin: jenkins
    completion: callback
    callback_base: "${env:RUNCOMPOSER_CALLBACK_BASE:-http://host.docker.internal:8100}"
    # base_url, job, run_parameters, shards, field_providers as above, and the
    # parameters plus CALLBACK_URL: "{callback_url}" and INGEST_TOKEN: "{token}"
```

`tests/test_four_ways.py::test_2b_a_job_that_fans_out_configured_not_coded`
runs `jenkins-fanout` from that file against the stand-in;
[`ci/jenkins/e2e.py`](../ci/jenkins/e2e.py) runs both entries against a real
Jenkins (steps 8–11: plan, fan-out polled, fan-out pushing with progress, a
cancel after the queue item was forgotten).

### 9.2 Compose when configuration is not enough

`robot-pool` and `jenkins` are built from the toolkits and import nothing
else of runcomposer
(`tests/test_self_containment.py::test_the_reference_runners_are_built_on_the_toolkits`).

**`runcomposer.sdk.robot`** — Robot Framework is imported inside the functions
that execute it; the id space is the Robot plugin family's (an item id is a
test's longname):

| Name | What |
|---|---|
| `ExactSelection(ids, tag=None)` | a pre-run modifier keeping exactly the tests whose longname is in `ids` — an id is never read as a pattern; `kept` counts what was found |
| `attribute_roots(suite_roots, item_ids, items=())` | which suite root each item is executed from |
| `LiveStatusListener(channel, shard, stopped=None)`, `RemoteLiveStatusListener(...)` | Robot listeners: `RUNNING` when a test starts, its status when it ends — to a `LiveChannel`, or pushed to the API by an execution runcomposer did not start. A test cut short by a stop is not reported as failed: what stays is that it was `RUNNING` |
| `Execution(root, item_ids)`, `ShardPayload(...)` | one Robot execution; everything one worker process needs — `shard`, `out_dir`, `executions`, `variables`, `partition`, `lane`, `channel`, `listeners`, `worker_setup`, `rerun_failed`, `rpa`, `robot_options`, … |
| `run_shard(payload)` | the worker's entry point: executes the shard and writes `RESULT_FILE` into its directory; `read_shard_result(out_dir)` reads that back as a `ShardResult` of `ShardOutput`s |
| `supervise(payloads, *, max_workers, on_done=None, cancelled=…, timeout_s=None)` | runs payloads on spawned worker processes: a bound on parallelism, lanes (payloads of one `lane` never overlap and keep their order), a per-shard timeout, the stop protocol; `on_done(outcome)` is called with a `ShardOutcome` in the calling thread as each payload ends |
| `stop_worker(process, out_dir)`, `STOP_FILE` | the stop protocol on its own: no further execution, Robot ends the one in flight, then terminate and kill |
| `deliver_shard(ctx, outcome, *, format="robot-output-xml", group=None)`, `collect_verdicts(ctx, outcome, *, format)` | parse a finished shard through `ctx.parse`, label every attempt, deliver, record `log.html` / `report.html` / `output.xml` and the shard's directory as artifacts; returns the reason when there is nothing to deliver |
| `merge_outputs(outputs, out_dir, ...)`, `MergeNoiseRemover` | several `output.xml` merged into one, without merge notes |
| `robot_version()` | the installed version, `None` without Robot Framework |

**`runcomposer.sdk.jenkins`** — stdlib `urllib`, no dependency:

| Name | What |
|---|---|
| `JenkinsClient(base_url, *, user, api_token, verify_tls, ca_bundle, request_timeout_s, retries, retry_backoff_s)` | one server: a session whose crumb belongs to it, bounded retry on transient errors (a trigger is repeated only when it provably did not take effect), TLS with an optional CA bundle; thread-safe |
| `trigger(job, parameters)`, `follow`, `build_of`, `locate_build`, `find_build`, `wait_for_build`, `wait_until_finished`, `wait`, `build_info` | trigger → queue item → build — never "the latest build of the job"; once the queue item is forgotten, by the queue id the build records. `follow(job, queue_url, timeout_s=…, check=…, on_build=…)` is the whole way from queue item to the finished build in one call, and takes the build back when the dispatch was cancelled before there was a link to stop it by |
| `stop_build`, `cancel_queue_item`, `cancel(job, queue_url)` | stopping a build, or whatever a queue item is by now |
| `artifacts`, `download_artifact`, `download_build_file`, `download`, `get_json`, `request`, `version`, `job_exists`, `job_url` | results — archived artifacts and any URL below the build — and the plumbing |
| `JenkinsError` (`.status`), `JenkinsTimeout` (`.what`, `.timeout_s`) | what went wrong; `status == 404` for something Jenkins no longer knows |
| `encode_spec`, `decode_spec`, `queue_id_of` | the compressed document form, and the id in a queue item URL |

Two composed runners, each written against the SDK and one toolkit only.
**A Robot runner of your own:** each test once, distributed over the chosen
stages; every stage executes its share in chunks, one after another, each
chunk a shard of its own:

```python
from runcomposer.sdk import (
    DispatchContext, Parameter, PlanContext, PlanPreview, RunnerBase, RunnerInfo,
    corpus_drifted, plan_chunks, split_max,
)
from runcomposer.sdk.robot import Execution, ShardOutcome, ShardPayload, deliver_shard, supervise


class LaneRunner(RunnerBase):
    PATH_OPTIONS = ("root",)

    def __init__(self, root: str, stages: list[str], chunk_size: int = 2):
        self.root, self.stages, self.chunk_size = root, list(stages), int(chunk_size)

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="lanes",
            capabilities=("live_status", "cancel", "plan"),
            parameters=(Parameter("stages", kind="multi_choice", choices=tuple(self.stages),
                                  default=list(self.stages)),),
            result_format="robot-output-xml",
        )

    def _shards(self, ctx: PlanContext) -> dict[str, tuple[str, list[str]]]:
        """shard → (stage, item ids): shared by plan() and dispatch()."""
        if corpus_drifted(ctx):
            ctx.refuse("the corpus changed since this run was composed")
        stages = list(ctx.parameters.get("stages") or self.stages)
        lanes = plan_chunks(ctx.item_ids, len(stages), ctx.history.durations(statuses=("PASS",)))
        return {
            f"{stage}-{index:02d}": (stage, piece)
            for stage, lane in zip(stages, lanes.chunks, strict=False)
            for index, piece in enumerate(split_max(lane, self.chunk_size), start=1)
        }

    def plan(self, ctx: PlanContext) -> PlanPreview:
        shards = self._shards(ctx)
        return PlanPreview(
            text=f"{len(ctx.item_ids)} test(s) in {len(shards)} chunk(s)",
            shards={shard: items for shard, (_stage, items) in shards.items()},
            groups={shard: stage for shard, (stage, _items) in shards.items()},
        )

    def dispatch(self, ctx: DispatchContext) -> None:
        preview = self.plan(ctx)
        channel = ctx.live_channel()
        payloads = [
            ShardPayload(shard=shard, out_dir=str(ctx.workdir / shard),
                         executions=(Execution(self.root, tuple(items)),),
                         variables={"STAGE": preview.groups[shard]}, partition=preview.groups[shard],
                         lane=preview.groups[shard], channel=channel,
                         run_id=ctx.run_id, dispatch_id=ctx.dispatch_id)
            for shard, items in preview.shards.items()
        ]
        ctx.declare(preview.shards, plan=preview.text, groups=preview.groups)
        problems: list[str] = []

        def finished(outcome: ShardOutcome) -> None:
            problem = deliver_shard(ctx, outcome, group=outcome.payload.partition)
            if problem:
                problems.append(problem)

        supervise(payloads, max_workers=len(set(preview.groups.values())), on_done=finished,
                  cancelled=lambda: ctx.cancelled)
        ctx.raise_if_cancelled()
        if problems:
            raise RuntimeError("; ".join(problems))
```

Configured as `lanes: {module: "shop_connector.lanes:LaneRunner", root:
tests/, stages: [staging, preview]}`. Workers are *spawned* processes: a
script that drives such a runner directly (not through `runcomposer` or
pytest) needs the usual `if __name__ == "__main__":` guard around what it
runs, or every worker re-executes the script. Its fuller twin,
`tests/test_robot_toolkit.py::LaneRunner`, passes `check_runner` against a
real catalog (`FakeDispatchContext.from_source`), runs in a real deployment
and is cancelled there in the middle of a lane
(`tests/test_robot_toolkit.py::TestAComposedRunner`).

**A Jenkins runner of your own:** one build of the shop's nightly job per
run, one shard per region, the results cut out of the build's console log:

```python
from runcomposer.sdk import DispatchContext, Parameter, PlanContext, PlanPreview, RunnerBase, RunnerInfo
from runcomposer.sdk.jenkins import JenkinsClient, encode_spec


class NightlyRunner(RunnerBase):
    def __init__(self, base_url: str, job: str, regions: list[str], timeout_s: float = 600.0,
                 poll_interval_s: float = 5.0):
        self.client = JenkinsClient(base_url)
        self.job, self.regions = job, list(regions)
        self.timeout_s, self.poll_interval_s = float(timeout_s), float(poll_interval_s)

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="nightly",
            capabilities=("cancel", "plan"),
            parameters=(Parameter("regions", kind="multi_choice", choices=tuple(self.regions),
                                  default=list(self.regions)),),
            result_format="runcomposer-verdicts",
        )

    def _regions(self, ctx: PlanContext) -> list[str]:
        regions = list(ctx.parameters.get("regions") or self.regions)
        if len(ctx.item_ids) < len(regions):
            ctx.refuse(f"{len(ctx.item_ids)} test(s) cannot be spread over {len(regions)} regions")
        return regions

    def plan(self, ctx: PlanContext) -> PlanPreview:
        regions = self._regions(ctx)
        return PlanPreview(text=f"nightly job, regions {regions}", shards={r: [] for r in regions})

    def dispatch(self, ctx: DispatchContext) -> None:
        regions = self._regions(ctx)
        queue_url = self.client.trigger(
            self.job, {"SPEC_B64": encode_spec(ctx.export_spec()), "REGIONS": ",".join(regions)})
        ctx.declare(regions, plan=self.plan(ctx).text, links={"queue": queue_url} if queue_url else None)
        if queue_url is None:
            raise RuntimeError("Jenkins did not say where it queued the build")
        # Queue item → build → its end. A cancel that arrived while the trigger was under
        # way found no link to stop anything by: `follow` sees it and takes the build back.
        build = self.client.follow(
            self.job, queue_url, timeout_s=self.timeout_s, poll_interval_s=self.poll_interval_s,
            check=ctx.raise_if_cancelled,
            on_build=lambda url: ctx.set_link("build", url))   # from now on a cancel stops the build itself
        if build["result"] not in ("SUCCESS", "UNSTABLE"):
            raise RuntimeError(f"the build ended {build['result']}: {build['url']}")
        console = self.client.download_build_file(build["url"], "consoleText", ctx.private_dir / "console.txt")
        text = console.read_text(encoding="utf-8")
        for region in ctx.outstanding_shards():   # a region the job pushed itself is done
            _, found, rest = text.partition(f"=== results {region} ===\n")
            if not found:
                raise RuntimeError(f"the console log has no results for {region}")
            results = ctx.workdir / region / "results.json"
            results.parent.mkdir(parents=True, exist_ok=True)
            results.write_text(rest.partition("\n=== end ===")[0], encoding="utf-8")
            ctx.add_artifact(f"results ({region})", results, shard=region)
            ctx.deliver(region, bundle=results.parent)

    def cancel(self, ctx: DispatchContext) -> None:
        if "build" in ctx.links:
            self.client.stop_build(ctx.links["build"])
        elif "queue" in ctx.links:
            ctx.log(self.client.cancel(self.job, ctx.links["queue"]))
```

The console log is downloaded into `ctx.private_dir`, so it is never served;
only the results it recorded are. Its twin,
`tests/test_jenkins_toolkit.py::NightlyRunner`, passes `check_runner`, runs in
a serving deployment where its private file answers `404`, and is cancelled
while its build runs — and during its trigger, where the build is taken back
(`tests/test_jenkins_toolkit.py::TestAComposedRunner`). The results it
delivers are in the reference format ([§4](#resultparser--native-artifacts-in-native-names-out)).

---

> **Migrating a connector written against an earlier 0.2 state.** The 0.2
> contract grew before its release. A connector written against an earlier
> state of it needs these changes:
>
> - `ctx.options` no longer contains the per-run parameters: it is the
>   runner's config entry and nothing else. Read what a run chose from
>   `ctx.parameters` ([§3.1](#31-describe--who-it-is-and-what-a-run-may-choose)).
> - `FakeDispatchContext.delivered_shards` is a method now,
>   `delivered_shards()`, beside `outstanding_shards()`.
> - `live_updates` of the fake context is the complete stream — `RUNNING`
>   included, and after the shard delivered; what a store holds at the moment
>   is `live_now` ([§6](#6-the-conformance-kit)).
> - A `RunStore` implements `seed_durations`, `duration_aggregates(…,
>   statuses=…)`, `shard_groups` in `update_dispatch`, an artifact's `group`
>   in `add_artifact_ref`, and takes and returns `LiveStatus` records for live
>   status ([§4](#runstore--persistence)).
> - A runner that does not derive from `RunnerBase` must define `plan(ctx)`
>   ([§3.2](#32-planctx--a-preview-without-side-effects)).
> - Only recorded artifacts are served: a file in `ctx.workdir` is
>   downloadable once `ctx.add_artifact` recorded it, or a directory above
>   it — not because it is there ([§3.3](#33-dispatchctx--prepare-declare-execute-deliver)).

---

## 10. Known limits

Deliberate, each for a reason:

- **A taxonomy leaf holds one pattern, not a filter expression.** The tree
  hands the builder one pattern per leaf; combining is the filter builder's
  job, and a leaf carrying an expression would put selections — the part
  that drifts — into the file ([docs/taxonomy.md](taxonomy.md#known-limitation-one-pattern-per-leaf-and-only-a-pattern)).
  Whole expressions belong in `ui.quick_filters` and `ui.presets`
  ([§5](#5-configuration)).
- **No per-runner custom "actions" endpoint.** A runner offers `dispatch`,
  `plan`, `cancel`, `health` and declared per-run parameters; an endpoint
  whose meaning only one runner knows would be an
  unchecked, undocumented surface of the API. Do such operations in your own
  tooling.
- **No catalog cache shared across CLI invocations.** Each CLI command is its
  own process and parses the corpus once; a server reloads only what its
  `fingerprint` says changed. A cache file would be one more thing to go
  stale or be shared between deployments by accident.
- **No `source.root` in the run spec.** The document names items by id; where
  the corpus lives is the executing side's configuration. A path in the
  document would tie it to one checkout layout (the schema still accepts the
  field as optional provenance, and nothing reads it).

Edges you should know:

- **A served file must lie under the recording run's directory.**
  `add_artifact` takes any local path, but only one inside that run's
  directory under `core.artifact_dir` is served; a path elsewhere is listed
  without a link (`tests/test_api.py::TestArtifactRoute::test_a_path_outside_the_artifact_dir_is_listed_without_a_link`).
  Write what should be downloadable into `ctx.workdir`.
- **`ctx.options` carries what the entry writes,** not the defaults of your
  constructor's keyword parameters: an option the entry leaves out is not in
  it. Read your configuration from `self`, where the constructor put it.
- **The `jenkins` runner's size limits are defaults,** not properties of
  anyone's server: 128 000 bytes per build parameter and 1 000 000 per
  trigger request are guesses on the safe side. Raise them, or set `null`,
  when your Jenkins and its agents take more
  (`tests/test_jenkins_toolkit.py::TestTheDocumentAsABuildParameter`).
- **Short secrets are not redacted.** A value of fewer than four characters
  taken from the environment is not blanked out of recorded text, since
  blanking `4` would mangle ordinary text; a `:-default` written in the file
  is not a secret either. Links a runner records (`declare(links=…)`,
  `set_link`) are kept as given — `cancel` needs them — so do not put a
  secret into a link
  (`tests/test_config.py::test_values_shorter_than_four_characters_are_not_redacted`).
- **Restart recovery cannot tell a reused process id** from the process that
  owned a dispatch; `cancel --force` closes such a dispatch.
- **One instance owns a store.** The store refuses a second in-flight dispatch
  per run across processes, but runcomposer does not coordinate instances or
  queue across them (DESIGN.md §13).
- **Windows** has a process-liveness code path of its own and CI runs the
  suite there without blocking merges; it has not been seen green yet.
  Tests that need a POSIX shell skip there with that reason.
