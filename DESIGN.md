# runcomposer — design

An open-source, tag-based test run composer & orchestrator.

Status: rev 5 — describes runcomposer 0.2 as built. Decisions final unless
marked open; §15 dates them.

**Reading this document.** This is a design document, and not everything
designed here is built. A clause marked `[planned]` records a decision that
stands but has **no implementation in this repository today** — read it as
intent, and expect nothing in the code to honour it. Every statement *not* so
marked describes what the code does now.

```bash
grep -n '\[planned\]' DESIGN.md      # the full list, in document order
```

That grep is the contract: implementing one of these means deleting its marker
in the same change, and adding an unbuilt idea means marking it.

---

## 0. Why runcomposer exists

A private predecessor prototype proved the product idea:
a taxonomy-driven compose UI over a tagged test corpus, a lossless tag-filter
AST, preview-first selection, and a run orchestrator. It also proved, twice,
how such a tool becomes un-releasable: its "generic" core authored
deployment-specific vocabulary, its one real runner shipped inside the core
behind import guards, execution was a hidden side effect of a storage call,
and locale strings and environment literals were baked into code and UI.

runcomposer keeps what the prototype proved right (the selection model, the
taxonomy, the compose flow, the store contract, the UI interaction model) and
redraws the boundary around a single principle:

> **The interface is a document, not a protocol.**
> The core's product is a versioned, human-readable *run spec* that anything
> can read, write, and understand — plus the machinery to compose it, hand it
> to any executor, and take results back from any transport.

Foundational decisions:

- **Open-source first.** Any environment-specific setup is a private plugin +
  config on top of the public core — never content of the core.
- **Tag-based and framework-agnostic.** Tests are *items with tags* from any
  `TestSource`; Robot Framework is the reference plugin, not the substrate.
- **Reference connectors, not built-ins** — a local process pool for Robot
  Framework, a Jenkins trigger, and a generic command runner — plus a
  first-class **export mode**: the spec must let any third-party executor
  integrate with **zero core interface changes**. They are plugins on the
  same surface (`runcomposer.sdk` and its two toolkits, §6.6) any private
  connector uses.
- **Generic, i18n'd UI** (EN + DE shipped), all environment specifics served
  from config.
- Prototype code and its old YAML contract are input, **not** constraints.

Scope guard (cross-ref §13): no cron, no queues across instances, no
multi-instance coordination, no CI-system ambitions. Compose, dispatch,
ingest, browse — that is the whole product.

---

## 1. Product identity & naming

**What it is:** a self-hosted web app + API + CLI that lets a team *see* their
test corpus through a curated tag taxonomy, compose precise selections with a
real filter language, and turn a selection into a reproducible, portable **run
spec** that any executor can fulfill — with results flowing back into a run
history that itself feeds new selections ("rerun what failed").

**What it is not:** a CI system, a scheduler, or a test framework.

**Name — decided: `runcomposer`.** The exchange document is the **run spec**
(`runspec`). Availability verified 2026-07-06: PyPI free (JSON API + simple
index), npm free. Published to PyPI from 0.1.3 (2026-09-03), so the documented
install is `pip install "runcomposer[robot]"`. The release workflow's PyPI job
is gated behind the `PYPI_PUBLISH` repository variable and authenticates by
Trusted Publishing (OIDC) — no API token exists; publishing is a deliberate
owner action, triggered by pushing a `v*` tag. Runner naming
axis is *what-it-drives*: `robot-pool` (Robot Framework executed on this
machine), `jenkins` (drives a Jenkins job), `command` (runs a configured
command), `demo` (simulates).

**License — decided: MIT.**

---

## 2. Domain model (core vocabulary — the only words the core knows)

| Term | Meaning |
|---|---|
| **Item** | A runnable test: `id`, `name`, `tags: [str]`, optional `hierarchy`, optional `meta` (opaque). |
| **Item id (normative)** | An opaque, stable string **minted by the TestSource**. Invariant: `TestSource.items()[].id` and `TestSource.resolve(native_name)` define the *same id space* — every native name a ResultParser emits must resolve to exactly one Item id. The core never parses, splits, or normalizes ids or native names; it compares ids for equality only. (A Robot Framework source mints `id = longname`; a pytest source might mint `id = nodeid`.) |
| **TestSource** | A plugin that enumerates Items, produces a content-hashed **catalog snapshot**, and owns native-name→id resolution. |
| **Catalog** | Every configured TestSource as one: one id space, one snapshot, reloaded when the corpus changes (§6.1). |
| **Taxonomy** | A curated tree over tag patterns used for navigation and selection-building. Data, not code. Served twice: verbatim, and **resolved** against the catalog — each node expanded with the concrete tags its own pattern matches, nodes matching nothing dropped, unclaimed tags gathered under one synthetic node. Resolution is derived per request, never stored, and speaks the same node grammar, so the file format does not know about it. |
| **Selection** | The lossless filter: `tag_filter` AST + optional explicit `item_ids`. Compiled against a catalog snapshot into a **materialized item list**. |
| **Run spec** | The versioned document: identity + selection (incl. materialization) + source snapshot + results contract + one opaque runner section. |
| **Run** | The stored record for one spec. Its state is a *headline*: derived from its latest dispatch, never written on its own (§4). |
| **Dispatch** | One hand-off of a run to an executor, and the unit the lifecycle is tracked on. `mode` is a configured runner id, `export` (the document was handed out for somebody else to execute) or `ingest` (results arrived for a run nothing was dispatched for). One run may have several dispatches (re-runs). |
| **Shard** | A named part of one dispatch — a partition, a chunk, a CI job. A dispatch *declares* its shard set at hand-off; completion is computed against those names. Shards may be declared in **groups** (the chunks of one partition): a label for display and roll-up that decides nothing. |
| **Delivery** | One shard's results arriving, content-hashed. Idempotent per `(run, dispatch, shard)` (§5). |
| **Live status** | What is known about an item *while* its shard executes: `RUNNING`, or the status it ended with. A progress report, kept apart from verdicts and replaced by the shard's delivery (§4). |
| **Verdict** | Per-item result: a status, duration, message, the `shard` it was delivered under, and `attempt`. An executor reports `PASS`, `FAIL`, `SKIP` or `ERROR`. A fifth status, **`MISSING`**, is never reported and never stored: it is *derived* for a planned item that never came back once nothing more is expected (§4). The shard is assigned by the delivery, not the producer: a parser emits verdicts without one, the delivery labels the bundle, and every verdict read back carries it. `attempt` counts executions of an item within one shard; the highest attempt is the **effective** verdict — the one that decides the outcome — and `flaky` is derivable from the earlier ones. |
| **Completion** | A finished dispatch's outcome, aggregated over the effective verdicts including the derived `MISSING` ones: `ERROR` > `FAIL` > `INCOMPLETE` > `PASS`. `INCOMPLETE` means something planned was never reported; a run with a missing item cannot be `PASS`, and neither can a run nobody reported anything for. |
| **Runner** | Anything that fulfills a run spec. In-process runners are plugins handed a `DispatchContext` (§6.2); external runners just consume the document. |
| **Parameter** | A per-run option a runner *declares* — the only values a run spec can carry into a runner (§6.2). |
| **Event** | One entry of a run's event stream: every state change, delivery, refusal and runner log line, with a cursor (§4). |
| **Observer** | A plugin told about lifecycle events — the completion hook (§6.5). |

Framework-specific concepts (suites, Robot `longname`, partition fan-out,
`output.xml`) live entirely in the framework plugins' vocabulary.

---

## 3. The run spec (the centerpiece)

Versioned YAML — **JSON-isomorphic and accepted everywhere**; the CLI can emit
either (`runcomposer spec --format json|yaml`), so zero-dependency consumers can
read the spec with nothing but a JSON parser. The JSON Schema ships inside the
package (`runcomposer/schemas/runspec-1.0.json`) and is what `runcomposer
validate` checks against; publishing it at a resolvable URL is `[planned]`, so
treat its `$id` as an identifier rather than a location. Design rules:

- **Core sections are generic and closed** within a spec version (see
  versioning policy below).
- **Exactly one open section: `runner`.** Namespaced by runner id; the core
  never interprets its contents. An in-process runner reads from its own
  namespace only the per-run parameters it declared (§6.2) — the section is
  open for external executors, not a way to reconfigure this machine.
- **The materialized item list is inside the document.** A dispatched/exported
  spec is self-sufficient: the exact item set is embedded, the filter that
  produced it is kept for provenance, and the snapshot hash makes drift
  detectable.
- **Compile-time materialization with provenance.** Anything resolved at
  compose time (history-based selections, taxonomy refs) is recorded together
  with how it was derived.

**Versioning policy:** `runspec: "MAJOR.MINOR"`. A consumer MUST accept any
spec of the same MAJOR: known sections validate strictly, **unknown fields
inside known sections are ignored** (that is what a MINOR bump adds), unknown
top-level sections are ignored with a warning. A consumer MUST refuse a higher
MAJOR. `runcomposer validate` pins to the newest schema it knows.

### 3.1 Schema sketch

```yaml
runspec: "1.0"

run:
  id: "01JZEXAMPLE0000000000000AA" # ULID, minted at compose time; correlation key for the RUN
  title: "Payments regression without quarantined tests"
  created_at: "2026-07-06T09:14:03Z"
  labels:                           # free-form provenance; stored, never interpreted
    requested_by: "alex"
    origin: "ui"

selection:
  tag_filter:                       # lossless AST — node := "pattern" | {op: AND|OR, items:[...]} | {not: node}
    op: AND
    items:
      - Payments                    # bare string = literal tag (case-insensitive)
      - op: OR
        items: ["prefix:Checkout-", "regex:^Cart(V2)?$"]
      - not: "regex:^Quarantine-.*"
  item_ids: []                      # optional explicit picks; if both present: intersection (fixed AND)
  materialized:                     # REQUIRED for dispatch/export; optional in previews
    item_ids: ["Shop.Payments.Cards.T001", "..."]   # THE authoritative executed set
    at: "2026-07-06T09:14:02Z"
    count: 17
  derived_from:                     # optional provenance for materialized inputs
    - provider: "history"
      query: { run: "LATEST", verdicts: [FAIL] }
      resolved_run_id: "01JZ8ZZ..."

source:
  provider: "robotframework"
  root: "tests/"                    # OPTIONAL provenance the builder may pass; the compose path
                                    # does not, so consumers must not require it
  snapshot: "sha256:9f2c..."        # catalog hash at compose time — an INTEGRITY CHECK, not an alternate compile path

results:
  expect:
    - format: "robot-output-xml"    # registered ResultParser id
  shards: "runner-declared"         # or an integer when known at compose time (see §4).
                                    #   The schema accepts both; the composer emits 1,
                                    #   and a non-integer is read back as 1.
  deliver: "none"                   # api | file-drop | none — advisory, and nothing
                                    #   reads it; the composer always emits "none"
  callback: "https://runcomposer.shop.example/api/v1/runs/{run.id}/results"
                                    # [planned] — schema-accepted, but nothing writes or
                                    #   reads it. The jenkins runner builds its callback
                                    #   URL from its own `callback_base` option instead.
  token: "rct_..."                  # accepted so that 0.1 documents validate; 0.2 never
                                    #   writes it — the ingest token is not part of the
                                    #   document (§5)

runner:                             # ONE opaque namespaced section
  robot-pool:                       # per-run parameter values of the runner configured
    partitions: ["staging"]         #   under this id (§6.2), validated when submitted
    allow_drift: false
```

Filter grammar notes (deliberate, not inherited):

- `prefix:X` is a filter primitive defined as pure sugar for `regex:^X`, so
  selections and taxonomy leaves speak one grammar.
- **Case handling is asymmetric, and both halves are load-bearing.** A bare
  literal is compared case-insensitively (tags are written by people). A
  `regex:` expression is compiled with no flags and therefore matches
  case-**sensitively** — and so does `prefix:X`, being sugar for `regex:^X`.
  The opt-out is inline: `regex:(?i)^cart`. Stated here because getting it
  wrong raises no error — a pattern written in the wrong case is valid and
  simply selects nothing.
- There is no configurable operator between `tag_filter` and `item_ids`:
  explicit picks semantically *narrow* a filter result, so both present means
  intersection — a fixed rule, not an option.
- There is no "tolerate unknown ids" switch: an explicit id the catalog does
  not know is a compose-time error, and drift between compose and execution
  is governed by the executor's drift policy (§3.3).
- **One text form.** The same filter has a one-line text form for people —
  `Payments AND (Smoke OR Regression) AND NOT prefix:Quarantine-` — read by
  `core.filtertext` and written by `format_filter`, so that writing a filter
  and reading the text back gives the same AST. `NOT` binds tighter than
  `AND`, `AND` tighter than `OR`, and parentheses group; the operators are
  the upper-case words `AND`, `OR`, `NOT` (`and` is a tag). A pattern that
  contains whitespace, a parenthesis, a double quote or a backslash, or that
  is an operator word, is written in double quotes, inside which a backslash
  takes the next character literally; a bare `regex:`/`prefix:` pattern that
  runs into `(` or `"` must be quoted. `AND (x)` / `OR (x)` is a group of one
  item — the one AST shape infix text cannot write otherwise. `*` is every
  item. Wherever a filter is given as one string — a selection's
  `tag_filter` over the API, the CLI's filter argument, the `filter` of
  `ui.quick_filters`, `ui.presets` and `ui.default_filter`, a taxonomy
  leaf's `filter` (which must read as one pattern), the kit's
  `FakeDispatchContext.from_source(selection=…)` — it is this text; a spec
  stores the AST, never the text. Text that cannot be read is a `400`
  `filter-syntax` naming the `problem` and its `position` (in characters),
  or CLI exit `2` with the column marked. A string that is not readable as
  text and contains neither an operator word nor a double quote is the
  single pattern it spells, with a warning that shows the quoted spelling —
  so `regex:^Cart(V2)?$` keeps selecting what it always did. The web UI and
  the core are held to the same cases, `tests/fixtures/filter-text.json`
  (tests/test_filter_text.py, tests/test_filter_text_entry_points.py).
- **"Every item" has no node of its own.** `runspec/1.0`'s filter grammar is
  closed: `filterNode` in the shipped schema is a string, `{op, items}` or
  `{not}`, with no other fields, so a new node shape would be refused by
  every 1.0 validator, and a string `"*"` would mean the tag `*` to a 1.0
  consumer and select nothing. "Every item" is therefore the one expression
  true of every item, `{"not": "regex:^$"}` — NOT of a pattern no tag can
  match. That is what a spec stores and the API returns; the text form, on
  the server and in the UI, writes that node as `*` and reads `*` as it, and
  `"*"` quoted is the tag spelled so.

### 3.2 What deliberately stays OUT of the core schema

Anything framework-, CI- or deployment-specific. All
of it lives either in the `runner.*` namespace (the plugin's own vocabulary)
or in runner/store policy — never in core sections. An exchange format that
grows one deployment-specific field at a time ends up almost entirely
deployment-specific; a closed core with a single open section is the rule
that prevents it.

### 3.3 Executor contract

A conforming executor MUST:

1. **Execute exactly `selection.materialized.item_ids`.** The embedded list is
   authoritative. `tag_filter` is provenance; `snapshot` is an integrity
   check. Executors do not re-compile selections. What is planned and never
   reported is not silently forgiven: it becomes `MISSING` (§4).
2. **Check drift before executing:** if the live corpus snapshot differs from
   `source.snapshot`, refuse by default. With an explicit override, execute
   the intersection of `materialized.item_ids` with the live corpus and report
   the difference as `SKIP` verdicts with reason `drift`. The override belongs
   to the executor, not to the core: `robot-pool` and `command` take it as
   the `allow_drift` option (for `robot-pool` also a per-run parameter), and
   the remote-agent example takes it as `RC_ALLOW_DRIFT=1`. There is deliberately no core CLI flag — the core never
   executes anything, so it has nothing to overrule.
3. **Deliver results that reference `run.id`** — and, when the execution is
   split, a `shard` label per bundle (§4/§5).

It MAY ignore everything else, including the whole `runner` section. That is
the contract: three obligations, one document.

---

## 4. Run lifecycle & identity

```
                    dispatch (runner)                declare(shards)
   COMPOSED ───────────────────────────► DISPATCHED ────────────────► RUNNING
      ▲                                      │  refused / raised          │
      └──────────────────────────────────────┘  before the hand-off:     │
            (no dispatch remains;              the reason is an event     │
             the headline is what it was)                                 │
                                                                          │
   export, or the runner returned                 all declared shards     │
   with shards outstanding                        delivered               │
      └──────────────► AWAITING_RESULTS ──────────────────────────► COMPLETE
                             │                                    (PASS | FAIL |
                             │                                     ERROR | INCOMPLETE)
   from DISPATCHED / RUNNING / AWAITING_RESULTS:
      → FAILED     the runner raised after the hand-off, or (AWAITING_RESULTS)
                   results were awaited longer than core.dispatch.awaiting_timeout_s;
                   delivered results are kept
      → CANCELLED  cancellation was requested and honoured
      → STALE      the process that was executing the dispatch is gone
```

**The lifecycle is tracked per dispatch.** A dispatch is in exactly one of
these states, and one function in the core (`lifecycle.advance`) decides
which moves are legal; nothing else changes a state. The run's own
`state` / `completion` / `completed_at` are a **headline**: derived from the
run's *latest* dispatch and cached on the run so that runs can be listed and
filtered by it. Three consequences, each deliberate:

- Re-dispatching a finished run cannot erase what the previous dispatch
  established. If the new dispatch is refused it leaves no dispatch behind,
  and the headline is the earlier result again.
- A late bundle for an *older* dispatch updates that dispatch and nothing
  else; the headline does not move.
- Handing a finished run out again (an export) makes it `AWAITING_RESULTS`
  until that hand-out is answered, finalized or cancelled — it is, at that
  moment, a run waiting for results. "Latest *completed* run" (§7) therefore
  skips it until then.

**Dispatch is asynchronous.** `POST /api/v1/runs` with a dispatch request
records the dispatch (`DISPATCHED`) and returns; the runner executes on the
**dispatch manager's** bounded pool (`core.dispatch.max_concurrent`, default
2). Progress is read from the run, its event stream and live verdicts. The
request does wait briefly — at most `core.dispatch.handoff_wait_s`, default
2 s — for the hand-off, so that a refusal that happens straight away (drift,
a failing readiness hook) is the answer to the request rather than an event
to go looking for. The CLI runs the same manager in the foreground: `run`,
`dispatch` and `rerun` return when the runner's `dispatch()` returned.

**The hand-off is `declare`.** A runner prepares (drift check, hooks,
planning) and then calls `ctx.declare(shards)` exactly once: the shard set is
recorded and the dispatch moves to `RUNNING`. Everything before that call may
refuse; a refusal or an exception before it leaves **no dispatch**, and the
reason is recorded as a `dispatch.refused` event. Everything after it is an
execution attempt that stays visible: an exception becomes `FAILED` with the
error text, and whatever was delivered until then is kept.

- **Identity is layered:** `run.id` (the spec) → `dispatch_id` (each hand-off;
  minted by runcomposer) → `shard` (declared by name at hand-off).
- **Completion is computed, never guessed.** A dispatch is complete when every
  shard it *declared by name* has delivered — a delivery under some other
  label is recorded but stands in for nothing. Its completion aggregates the
  **effective** verdicts (highest attempt per shard and item) plus one derived
  `MISSING` verdict for every planned item that never reported: per shard when
  the runner declared which items each shard carries, per dispatch otherwise.
  The planned set is what is owed either way — a runner that declares shards
  covering fewer items than it was given does not shrink the run; the rest is
  `MISSING`.
  So "1 of 46 delivered" is `INCOMPLETE`, a bundle whose only verdict could
  not be resolved is `INCOMPLETE`, and an empty result is never `PASS`.
  Dispatching a run whose materialized selection is empty is refused.
- **When the runner returns:** all declared shards in → `COMPLETE`; shards
  outstanding → `AWAITING_RESULTS`, and their results arrive by ingestion
  (§5); no shards declared at all → settled at once, `INCOMPLETE`, since
  nothing can arrive. An export dispatch is born `AWAITING_RESULTS` with shards `1…N`
  (`results.shards`, default 1). `POST /runs/{id}/finalize` closes the latest
  dispatch as it stands — outstanding shards are given up, unreported items
  count as `MISSING` — and is refused while a runner is still executing it
  or after it was cancelled.
- **Late results.** `FAILED` and `STALE` dispatches keep their partial
  results and can still be completed by deliveries that arrive afterwards: an
  interrupted process does not un-run the tests that had finished, and an
  expired wait (below) does not refuse the results that end it.
  `CANCELLED` is final — results for a cancelled dispatch are refused, also
  a delivery that was read against the dispatch before another process
  recorded the cancel (tests/test_two_processes.py).
- **Expiry of a wait for results.** A dispatch handed to an external
  executor waits in `AWAITING_RESULTS`; if the executor died, nothing ever
  arrives. With `core.dispatch.awaiting_timeout_s` set (default `null`:
  wait until someone finalizes or cancels), a dispatch that has waited
  longer than that becomes `FAILED` with a `run.failed` event whose data
  says so — `expired: true`, `waited_s`, `awaiting_timeout_s` and the
  `outstanding_shards` that never delivered — and whose error says that
  results arriving later are still taken in. The clock starts when the
  dispatch began to wait and restarts with every delivery, so a job that
  reports shard after shard is not expired between two of them. A dispatch
  still executing in a runner is not what expires. `serve` applies the rule
  in a background loop (every quarter of the timeout, but no more often than
  once a second and at least once a minute); `runcomposer gc` applies it once, for
  a deployment without a server (`expired_dispatches` in its report).
  Failing rather than finalizing is deliberate: an expired dispatch is an
  execution that broke, not a result, and `FAILED` keeps it open for the
  results that may still come (tests/test_runner_contract.py,
  `TestAwaitingExpiry`).
- **Cancellation** (`POST /runs/{id}/cancel`, `runcomposer cancel`). For a
  dispatch executing in this process the context's `cancelled` flag is set and
  the runner's `cancel(ctx)` is called; the dispatch becomes `CANCELLED` when
  the runner comes back. For one executing in *another* process the request is
  written to the store and honoured there within half a second — which is how
  the CLI cancels a run a server is executing. Whoever asks first — a cancel
  made in this process, the dispatch manager's watch on the store for one
  made elsewhere, or a dispatch that ends cancelled for any other reason —
  the flag is raised and the runner's `cancel(ctx)` called exactly once, and
  a dispatch is not closed as `CANCELLED` before that call has returned: a
  runner that notices `ctx.cancelled` and comes back early does not leave
  its executor running (tests/test_cancel_paths.py). For a dispatch that only waits for
  results, the runner is asked to stop its executor through a context rebuilt
  from the stored record, and the dispatch is `CANCELLED` at once. A dispatch
  still queued is cancelled without the runner ever starting.
- **Restart recovery.** While a runner executes, the dispatch names its owner
  (host, process id, a per-process token). At startup — `serve`, the
  dispatching CLI commands, and `doctor` — every `DISPATCHED`/`RUNNING`
  dispatch whose owner is gone is marked `STALE` with a `run.stale` event
  saying why. `serve` repeats this every `core.dispatch.recovery_interval_s`
  seconds (default 15; `null`: at startup only), so a dispatch of another
  process on the same store — a foreground `runcomposer run` — whose
  process dies while the server is up becomes `STALE` without a restart; a
  dispatch whose owner is alive, or is the server itself, is never touched,
  and the store decides again inside its transaction, so a dispatch that
  moved on in the meantime is left alone (tests/test_recovery_periodic.py).
  An owner is gone when its process id is dead (on Linux a
  process that ended but was not reaped — a zombie under a container's
  process 1 without an init — counts as dead); when it is this
  very process id under another token (an earlier life of it — process id 1
  of a restarted container); or when it ran on another host — one instance
  owns a store (§13), so that is a container that was replaced. What cannot
  be told apart is a process id that was reused by an unrelated program: the
  dispatch then looks owned. `cancel --force` (`?force=true`) is the way out
  — it closes a dispatch as `CANCELLED` whoever owns it. (A dispatch that
  merely *waits* for results has no owner; its way out is the expiry above,
  `finalize` or `cancel`.) Two processes recovering one store mark such a
  dispatch once. A reader does not recover: `runcomposer runs`, `show` and
  `watch` flag a `DISPATCHED`/`RUNNING` dispatch whose owner is gone as
  `RUNNING (owner gone)`, with a line naming the way out (`doctor` or
  starting `serve` marks it `STALE`, and so does a running `serve` within
  its recovery interval; `cancel --force` closes it), and their
  JSON carries `owner_gone` on the run and on each dispatch; they change
  nothing (tests/test_cli_owner_gone.py).
- **Events.** Every state change emits an event — `run.composed`,
  `run.dispatched`, `run.running`, `run.awaiting_results`, `run.completed`,
  `run.failed`, `run.cancelled`, `run.stale` — as do `dispatch.refused`,
  `delivery.recorded`, `delivery.attached` (a quarantined bundle bound to a
  run by a person), `run.cancel_requested`, and `log` / `warning` lines — a
  runner's, its worker processes' (through the live channel), and an
  external executor's (pushed, marked `source: push`, §5). Events are appended to the store with an increasing `seq`, read back
  after a cursor (`GET /runs/{id}/events?after=`), and handed to observers
  (§6.5).
- **Live status** is an optional runner capability: per-item status streamed
  while a shard is still executing — `RUNNING` when an item starts, then the
  status it ended with (`LIVE_STATUSES`). It is its own record
  (`LiveStatus`), not a verdict: `RUNNING` can be streamed but never
  delivered, stored as a verdict or scored, and a delivery or push that
  claims it is refused. Live status is kept apart from delivered verdicts,
  replaced per (shard, item) by the next update, flagged `live` where it is
  shown, and dropped when the shard delivers; an item the run did not plan
  or a status outside `LIVE_STATUSES` is ignored. It comes from a runner's
  `ctx.live` / live channel (§6.2) or, for an executor runcomposer did not
  start, from the progress endpoints (§5). The run detail counts what is
  streaming per shard (`live_summary`). Without it a run shows planned →
  final (tests/test_runner_contract.py, `TestRunningAndWorkerLogs`). Once a
  dispatch is over (`COMPLETE`, `FAILED`, `CANCELLED`, `STALE`) nothing
  executes: an item last seen `RUNNING` is no longer listed as a live row
  or counted in `live_summary` — `status=RUNNING` selects what is executing
  now — and what was seen stays on its `MISSING` row as `last_live`
  (tests/test_live_rows_after_dispatch.py).
- **What a `MISSING` verdict last saw.** `MISSING` says "nothing was
  delivered for this item" — which is also true of an item that was seen
  passing live in a shard that was cancelled or never delivered. So every
  `MISSING` row of a run detail carries `last_live`: what was last streamed
  for that item (`status`, `duration_ms`, `message`, `shard`), or `null`. It
  stays `MISSING` — a live report is not a result — but nobody has to match
  rows up to tell "never started" from "ran, result not delivered"
  (`TestRunDetailSemantics`).
- **Unsolicited results** (unknown or missing `run.id`) never create run
  records by themselves. From the file drop they land in a visible
  **quarantine** for a person to attach or promote (`runcomposer quarantine`,
  or `ingest --allow-unsolicited`); over HTTP a push to an unknown run is
  refused outright (§5). Rationale: on shared result transports, "the newest
  bundle is somebody else's run" is a routine event — auto-accept would
  silently swallow exactly those.

---

## 5. Result ingestion

Dispatch and result-return are **decoupled**. One pipeline, pluggable
parsers, four ways in:

1. a runner's own `ctx.deliver` (§6.2);
2. API push — `POST /api/v1/runs/{id}/results` (multipart: artifacts +
   optional `shard`, `dispatch`, `format`);
3. the **file-drop inbox** — a watched directory for git-transported or
   air-gapped bundles;
4. manual — `runcomposer ingest <path> [--run <id>]`.

Upload from the UI is `[planned]`.

**The pipeline** is the same for all of them: find the run and the dispatch →
verify the marker → parse with the ResultParser for the run's expected format
→ correlate every native name through `TestSource.resolve` → record the
delivery → let the lifecycle recompute (§4). A bundle is parsed *before*
anything is written, so one that cannot be read leaves no trace on the run.
A bundle that names no dispatch goes to the dispatch still waiting for
results (the newest such), else the latest; results for a run nothing was
dispatched for open an `ingest` dispatch declaring the shard they arrived
under — for results that will arrive in several shards, export the run first
(§4: the export declares them).

**Correlation & the bundle marker.** For transports that cannot set HTTP
fields, the bundle carries a **`runcomposer_run.json` marker**
`{run_id, dispatch_id?, shard?, spec_sha256?}`, written next to the native
artifacts by whatever executed the run (`runcomposer-exec` writes it — §6.2c).
Ingestion matches on `run_id` and verifies `spec_sha256`, when present,
against the hash of the exact document bytes handed out for that dispatch. A
mismatched or markerless bundle is refused — into quarantine when it came
over a transport — never silently into a watched run.

**Idempotency (normative):** every delivery is content-hashed, and the key is
`(run, dispatch, shard)`. The same content again for that shard is a
**no-op** (file-drop pollers, git re-pulls and webhook retries all redeliver
in practice). *Different* content for a shard that already delivered
**replaces** that shard's verdicts — last-writer-wins, so a correction can
flip FAIL→PASS — and completion is recomputed. The same content under
*another* dispatch is a new delivery: re-running a spec that yields identical
results completes again. The store makes the check-and-write atomic, so
concurrent deliveries to one shard leave exactly one.

**Security floor (not deferred).**

- *The ingest token.* Pushing results over HTTP needs a token. One is minted
  whenever a run is handed out — an export, or a runner handing work to an
  external executor (`ctx.ingest_token()`) — and it is **bound to that
  dispatch**: it delivers to that dispatch and to no other. It is returned
  once, in the answer to the hand-out (`ingest_token`; `spec --export` prints
  it or writes `--token-file`), and only its hash is stored. It is **not part
  of the spec document**: the document can be read back, re-downloaded and
  passed around without leaking a credential. Lost tokens are rotated
  (`POST /runs/{id}/dispatches/{dispatch_id}/token`), which invalidates the
  previous one. The schema still accepts `results.token` so that documents
  written by 0.1 validate; importing one keeps the token's hash for the run
  and drops the plaintext. `core.ingestion.tokens: disabled` turns the check
  off for closed networks. The CLI's `ingest` and the file drop need no
  token — whoever can run them already has the store.
- *Auth precedes any read and any write.* The run and the token come from
  the path and the headers: an unknown run is a plain `404` and nothing is
  kept; a missing token is `401`, a wrong one `403`, a token used against
  another dispatch `403` — answered before a byte of the body is read, the
  connection then closed. (The whole answer goes out first, and what the
  client still sends is discarded for at most two seconds before the close:
  a client that is in the middle of sending reads the refusal, not a reset.)
  The body of an authenticated push is read as a
  stream into a temporary bundle directory, each file under its base name (a
  name that is not a file name — `..`, `.`, empty, a control character — is
  a `400`). Only an authenticated bundle that is then refused for what it
  contains is quarantined, named by its files' names — never by where it lay
  on the server (tests/test_api_streamed_push.py,
  tests/test_api_hostile_names.py).
- *Bounded.* Upload size (`max_upload_mb`) and the quarantine
  (`quarantine_max`, enforced **on write** — a new entry beyond the bound
  evicts the oldest) are config. The upload size is counted while the body
  arrives — `413` once it is passed, whatever length was declared, a chunked
  upload without one included — and bounds the progress pushes too (§5,
  below). Identical bundles are quarantined once.
- *Explicit, logged attach.* Binding a quarantined bundle to a run is a
  person's decision (`quarantine attach`); it is recorded as a
  `delivery.attached` event on the run. An attach that would record nothing
  of the run — not one of the bundle's results is for an item the run
  planned — is refused (`409`, `nothing-planned`): binding it would only
  close the run as `INCOMPLETE`. The run and the quarantine entry stay as
  they were (tests/test_runner_contract.py, `TestRunDetailSemantics`).
- *Defused XML.* A document carrying a DTD is refused by the parser itself —
  wherever the declaration sits, in whatever encoding — through one shared
  helper every XML-reading parser uses (`forbid_dtd` / `parse_xml`, §6.6).
- *A spec cannot make this process run code.* A result format is a **name**,
  resolved through the `parsers:` config section and the entry points — never
  an import path taken from a document — and a spec's `runner.<id>` section
  reaches a runner only through that runner's declared parameters (§6.2).

Full authN/authZ stays out of scope (§13); the write path is not open by
default.

**Progress from an executor elsewhere.** Results are not the only thing an
external executor has to say: while it works, which item is executing and
how items ended is worth showing, and so are its log lines. Two endpoints
take them, authorized exactly like the results push — the run first, then
the dispatch's ingest token (`X-Runcomposer-Token` or `Bearer`), before
anything is read, and their bodies bounded by `max_upload_mb` (plus 64 KiB
for the envelope):

- `POST /api/v1/runs/{id}/live` — `{dispatch?, updates: [{shard, item_id,
  status, duration_ms?, message?}]}` → `{dispatch_id, accepted, ignored,
  reason}`. The same rules as a runner's `ctx.live`, said in the answer
  instead of silently: a status outside `LIVE_STATUSES` is a `400`; an item
  the run did not plan, a shard that has delivered, and everything once the
  dispatch is over are ignored and counted.
- `POST /api/v1/runs/{id}/log` — `{dispatch?, lines: [{message, level?}]}`
  → `{dispatch_id, accepted}`: `log` events (`warning` for the levels
  warning and error) marked `source: push`.

Both name the dispatch their token is bound to; a token used for another
dispatch is a `403`. Neither is a result: live status is dropped when the
shard delivers, and only the results push completes anything. The client
side is `RemoteLiveChannel` in the SDK (a `LiveChannel` that POSTs),
`runcomposer.sdk.robot.RemoteLiveStatusListener` for a Robot Framework agent
(configured from `RUNCOMPOSER_CALLBACK_URL`, `RUNCOMPOSER_INGEST_TOKEN`,
`RUNCOMPOSER_SHARD`, `RUNCOMPOSER_DISPATCH_ID`), and `runcomposer-exec
--callback` (§6.2c) (tests/test_runner_contract.py, `TestPushedProgress`,
`TestTheRemoteChannelAndTheConsumer`).

Over HTTPS an executor verifies the server's certificate against the
system's trust store, or against a CA bundle — a file of the certificates
to trust, for a server behind a private certificate authority:
`ca_bundle=` on `RemoteLiveChannel` and `push_results`, `--ca-bundle` /
`RUNCOMPOSER_CA_BUNDLE` for `runcomposer-exec`, and the same variable for
`RemoteLiveStatusListener` (or its `ca_bundle` argument). `verify_tls=False`
/ `--no-verify-tls` / `RUNCOMPOSER_VERIFY_TLS=false` skips the check, for a
lab server whose certificate nobody signed. A bundle that does not exist,
or holds no certificate, is refused where it is given — by the channel's
constructor, by `push_results` before anything is sent, and by the
consumer before anything executes. A certificate
that cannot be verified is a counted progress failure, and a refused
results push whose message names the TLS reason and the way out. The
channel stays picklable: the TLS context is built in the process that
pushes (tests/test_remote_tls.py, tests/test_remote_listener_tls.py).

**The file drop.** Each immediate subdirectory of the inbox
(`core.ingestion.inbox`) is one bundle, taken in when it is *complete*:
either it contains the marker or an empty `.done` file — the sender's own
signal, and why `runcomposer-exec` writes the marker last — or nothing in it
(no file added, removed, resized or touched) changed for
`core.ingestion.settle_s` (default: two poll intervals). A processed bundle
moves to `processed/` whatever happened to it, so a bundle that cannot be
ingested is quarantined and can never block the ones after it.

**ResultParser plugins** turn native artifacts into verdicts. Shipped:
`runcomposer-verdicts` (the reference JSON format), `robot-output-xml`,
`junit-xml`. Bad input is one error type — `ParseError` — whichever parser
raises it; anything else a parser raises is treated the same way: the bundle
is refused as `bad-bundle`. `robot-output-xml` reads an `output.xml` with
the standard library's streaming reader, after the defused check: Robot's
own reading restated — longname, status, message, elapsed time, a failed or
skipped suite teardown applied to the tests below it — and compared field
by field with Robot's result model (tests/test_robot_output_reader.py). So
a server that parses results needs no Robot Framework, loads nothing of
it, and never holds a large output's keyword tree in memory. **The anti-leak litmus test is about identity,
not parser count:** the core never string-compares native names — every
native name goes through `TestSource.resolve(native_name) → id`, so all
name-normalization quirks live inside the source plugin that owns them. A
guard test asserts the core contains no native-name normalization. Names the
catalog does not know are reported as warnings, never invented as ids — and
the items they were meant for end up `MISSING`.

**Refusals over HTTP** carry the reason as the error `code` and a status that
says what kind of refusal it was: `422` the bundle cannot be read
(`bad-bundle`, `unsolicited`), `409` it contradicts the run (`sha-mismatch`,
`marker-conflict`, `dispatch-cancelled`, `nothing-planned`), `404` `unknown-run` /
`unknown-dispatch`, `400` a declared format that is not the run's, a body
that is not multipart, a file name that is not one, `413` too large.

`runcomposer export --format ctrf` emits a normalized cross-tool result
document of a run's latest dispatch — effective verdicts, `MISSING` reported
as `other` — fitting for a project whose thesis is document-shaped
interfaces.

---

## 6. Ports (in-process plugin protocols)

Five roles — `TestSource`, `Runner`, `ResultParser`, `RunStore`,
`RunObserver` — defined as protocols in `runcomposer.core.ports`, and one
object going the other way: the `DispatchContext` runcomposer hands a runner.

**Plugin loading.** Every plugin section of the config file (§8) is a mapping
of *id → options*. The plugin class comes from `module:
"mypkg.runners:MyRunner"` inside the entry (an import path, always
`package.module:ClassName` — the hack-it-in-an-afternoon path), or from
`plugin: <entry-point name>` (the packaged-distribution path, entry-point
groups `runcomposer.sources`, `.runners`, `.stores`, `.parsers`,
`.observers`), and defaults to the entry's key as an entry-point name. Both
are first-class; there is no env-var module loading.

**Two import rules, each held by a guard test.** `runcomposer.core` imports
nothing above it — not the application layer, not a plugin — and names no
framework. The reference plugins in `runcomposer.plugins` import from this
package only `runcomposer.sdk` and its toolkits (§6.6): the same surface a
connector outside this repository has.

### 6.1 `TestSource`

```python
provider_id: str
def items() -> list[Item]
def snapshot() -> str                            # content hash
def resolve(native_name: str) -> str | None      # parser-name -> id (None = unknown)
# defaults in the SourceBase helper class:
def fingerprint() -> str | None                  # cheap "did the corpus change" token; None = cannot tell
duplicate_ids: Sequence[str]                     # ids more than one item carries
```

Reference impls:
- **`manifest`** — a JSON/YAML catalog; requires **only `id` + `tags`**
  (`name`, `hierarchy`, `meta`, `aliases` optional). This is the
  zero-dependency adopter path. Its snapshot hashes ids and tags — what a
  selection can depend on — so renaming a test or editing `meta` is not drift.
  Docs ship **two** examples: a Robot Framework catalog and a pytest catalog
  (`id: "tests/test_cart.py::test_checkout[visa]"`) — the framework-agnostic
  claim is demonstrated, not asserted.
- **`robotframework`** — walks `.robot` files via `robot.api` (a dependency of
  the plugin, not the core); mints `id = longname`; `resolve` owns any
  name-normalization quirks. Takes `root` or `roots`: a repository often holds
  several sibling suite trees that belong in one catalog, and parsing each as
  its own root (rather than pointing Robot at their common parent) is what
  keeps the ids stable — Robot names the root suite after the directory it is
  given, so a common parent prepends a segment to every longname and the
  catalog's ids stop matching the ids that come back in results.
- **`demo-corpus`** — the bundled web-shop catalog (§12), as a named source.
  It is what "no `sources:` configured" means, spelled as configuration.

**The catalog.** `sources:` may hold several entries; together they are one
catalog (`app.catalog`): one item list, one snapshot (a single source's own,
verbatim; a hash over all of them otherwise), `resolve` asking each source in
turn. An id carried by more than one item — across sources, or within a source
that tolerates it (the Robot source does; a manifest refuses a duplicate id in
its own file) — is reported (`duplicate_ids`; `runcomposer catalog`,
`doctor`), not raised: a corpus is not unusable because two of its tests are
named alike.
A selection plans such an id once, with a warning.

**Staying current.** A source is loaded once and kept. One that offers
`fingerprint()` — the bundled ones do: file sizes and modification times, no
parsing — is reloaded when the token changes, checked at most once a second.
One whose `fingerprint()` returns `None` is reloaded on request
(`POST /api/v1/catalog/reload`) and before every dispatch, where the drift check needs the corpus as it is *now*.
Composing a run also refreshes first. Editing the corpus never requires a
restart, and a corpus that did not change is never parsed twice in one
process. (Each CLI invocation is its own process and parses once; a
cross-invocation cache is not built.)

### 6.2 `Runner`

```python
def describe() -> RunnerInfo
def dispatch(ctx: DispatchContext) -> None
# optional — defaults in the RunnerBase helper class:
def plan(ctx: PlanContext) -> PlanPreview    # declared by the "plan" capability
def cancel(ctx: DispatchContext) -> None     # declared by the "cancel" capability
def health() -> Health                       # status ok | degraded | down | unknown;
                                             #   data: runner-defined, shown verbatim
```

One instance is built per configured runner id and may be dispatching
several runs at once: everything that belongs to one dispatch lives on its
context, never on the runner. Construction must be free of side effects —
every configured plugin is built once at startup (§8).

**`RunnerInfo`**: `id` (the plugin's name — the id a deployment dispatches to
is the config key, so one plugin can be configured several times), `title`,
`description`, `capabilities` (flags; `live_status`, `partitions`, `cancel`,
`plan`, `external_delivery`, `simulated` have meaning to clients),
`result_format`, and **`parameters`**: the per-run options the runner
accepts from whoever starts a run. A `Parameter` has `name`, `kind`,
`label`, `help`, `default`, `choices`, `required`, an optional `pattern` and,
for a `mapping`, an optional `key_pattern`. The seven kinds:

| kind | value |
|---|---|
| `string` | one line of text |
| `text` | several lines — a form renders a text area |
| `integer`, `boolean` | as named |
| `choice` / `multi_choice` | one value / a list drawn from `choices` |
| `mapping` | names to strings — extra variables of a test framework, say |

`pattern` is a regular expression the whole value must match
(`re.fullmatch`) — of a `string`, of the whole `text`, of every *value* of a
`mapping` — checked when the run is requested, not inside the runner, with
an error naming the parameter. The API exposes the declarations, `pattern`
included (`GET /runners`, `/meta`), validates submitted values against them
— an unknown, missing, ill-typed or non-matching value is a `400` naming the
parameter — and writes the accepted values into `runner.<id>` of the spec
document (tests/test_parameters.py). `key_pattern` is the same for every
*key* of a `mapping` (`[A-Z][A-Z0-9_]*` for variable names, say); it is
served with the declaration (`"key_pattern": null` when not declared), so a
form checks names as it checks values (tests/test_parameter_refusals.py).

What a declaration cannot express, the runner checks itself: a runner that
cannot carry out a per-run value raises `InvalidParameter("<name>", "<what
is wrong>")`. From `plan`, or from `dispatch` before `declare`, it is the
request's `400 invalid-parameter` naming the parameter, and no dispatch
remains (the CLI says it like a value the declaration refuses, exit `1`);
raised after the hand-off it fails the dispatch with its message
(tests/test_parameter_refusals.py).

**A document cannot reconfigure the machine that executes it.** Of a spec's
`runner.<id>` section, an in-process runner sees exactly the keys it declared
as parameters, validated. Everything else in that section is ignored with a
warning event — it may be meant for an external executor — and cannot
override a configured option. Readiness hooks, listeners, paths and
credentials come from the config file only.

**Options and parameters stay apart.** `ctx.options` holds the options
written in the runner's config entry, as its constructor received them
(`${env:…}` resolved, numbers and booleans from the environment converted,
§8), and nothing else; `ctx.parameters` holds the
per-run values after validation, with the declared defaults filled in. Nothing is overlaid: a per-run
value never appears in `options`, so a parameter cannot shadow the option of
the same name, and a runner that wants a per-run value reads it from
`parameters` (tests/test_runner_contract.py, `TestOptionsAndParameters`).

**`DispatchContext`** — everything a runner gets, and everything it may do:

| | |
|---|---|
| `run_id`, `dispatch_id`, `runner_id` | identity; the dispatch id is minted by runcomposer |
| `spec`, `item_ids` | the document (read-only) and `selection.materialized.item_ids` — the authoritative executed set |
| `options`, `parameters` | the options of the runner's config entry and nothing else; the validated per-run parameters, defaults applied — apart, never overlaid (above) |
| `workdir` | a directory for this dispatch under the artifact root, already created; a file written here is served once it is recorded with `add_artifact` (§6.4) |
| `private_dir` | a directory for this dispatch that is **never** served, whatever is recorded — for files that must not be downloadable (credentials a hook writes, intermediate state). Under `core.private_dir`, created on first use, removed with the run and swept by `gc` |
| `source` | the catalog as it is now — drift check, name resolution |
| `connector_settings(name)` | the deployment's `connectors.<name>` mapping (`${env:…}` resolved), read-only, empty when there is none — how a callable named in a runner's configuration reads the settings of the connector package it belongs to |
| `history.durations(labels=, last_n=, statuses=)` | mean duration per item id from earlier dispatches; `statuses` restricts it to executions that ended so (§7) |
| `refuse(reason)` | decline; legal before `declare`, leaves no dispatch |
| `declare(shards, *, plan="", links=None, spec_sha256=None, groups=None)` | THE hand-off (§4): shard names, or names mapped to the item ids each shard will report; exactly once. `groups` labels shards that belong together (`{shard: label}` — the partition each chunk runs on): results get a roll-up of their own, next to the one per shard. `spec_sha256` records the hash of document bytes the runner serialized itself (`export_spec()` records it for the bytes it returns) |
| `set_link(name, url)` | record or replace one named link for what is only known after the hand-off — the URL of the build a queued job became. Before `declare` it joins the hand-off's links; after it, it is stored (and logged), so a context rebuilt for `cancel` carries it |
| `live(shard, item_id, status, …)` / `live_channel()` | streamed per-item status — `RUNNING`, then how the item ended (§4); the channel is a **picklable** object with `live` and `log` for worker processes |
| `delivered_shards()`, `outstanding_shards()` | this dispatch's shards that have delivered — by this runner or by a push from outside — and those still owed, in declaration order. A runner whose executor pushes results itself may find the dispatch complete before it is done with it |
| `parse(bundle, *, format=None)` | native artifacts → correlated verdicts, with the configured parser — for runners that relabel (attempts) before delivering |
| `deliver(shard, *, verdicts=… \| bundle=…, format=None)` | one shard's results; idempotent by content hash; completion is recomputed after every delivery |
| `add_artifact(name, path_or_url, *, media_type=None, shard=None, group=None)` | record a file, a **directory** or an `http(s)` link — of the dispatch, of one shard, or of a group of shards. Recording is what makes a local file downloadable (§6.4) |
| `log(message, level="info")` | a line in the run's event stream |
| `export_spec()` | the document as the exact JSON bytes to hand to an external executor; their hash is recorded for marker verification (§5) |
| `ingest_token()` | mint the token an external executor pushes results — and progress — with (§5) |
| `links` | what was given to `declare` and set with `set_link` — how `cancel` finds its executor |
| `cancelled`, `raise_if_cancelled()` | cooperative cancellation |

It is a protocol, so a runner can be tested against a fake. The live channel
works with **any** RunStore: it carries no store, only a file in `workdir`
that worker processes append to (one file per process) and the dispatching
process tails — live status and log lines alike, so a worker's progress
lands in the run's event stream (tests/test_runner_contract.py,
`TestOwnDispatch`, `TestShardGroups`, `TestRunningAndWorkerLogs`). What a
worker wrote before its shard is delivered is in the stream before that
delivery: a delivery reads the files once more before it records, and waits
for a line the tailing thread has read but not yet emitted
(tests/test_live_lines_before_delivery.py).

Three rules about using it, each held by a test:

- **Several threads at once.** `declare`, `set_link`, `deliver`, `log`,
  `add_artifact`, `live` and `live_channel` may be called from several
  threads at once, on the real context and on the kit's fake: nothing is
  lost or recorded twice, and a link set while another thread declares is
  part of the hand-off or stored after it. What stays the runner's to
  order: one shard is delivered by one thread (two deliveries of a shard
  are two deliveries, the later replacing the earlier), and `declare` comes
  before the deliveries it announces (tests/test_context_threads.py).
- **Record, then deliver.** The delivery of the last outstanding shard
  completes the dispatch, and `run.completed` is written then: what a
  runner records after it — an artifact, a log line — arrives after the run
  completed, and an observer reacting to `run.completed` does not see it.
  Record a shard's artifacts before delivering it; the Robot toolkit's
  `deliver_shard` does (tests/test_robot_pool_options.py,
  `TestRecordBeforeDelivering`).
- **A refusal leaves nothing.** A dispatch that leaves no record — refused,
  an `InvalidParameter`, raised or returned before `declare` — leaves no
  files either: the `workdir` and `private_dir` that attempt created are
  removed again. A cancel before the hand-off keeps them, since a
  `CANCELLED` record remains (tests/test_dispatch_leftovers.py).

**Previewing a dispatch.** `plan(ctx: PlanContext) -> PlanPreview` answers
"what would this dispatch do?" — every item with the shard it would land in
— and does nothing else: no execution, no record, no trigger. `PlanContext` is a strict
subset of `DispatchContext`: `run_id`, `runner_id`, `spec`, `item_ids`,
`options`, `parameters`, `source`, `history`, `connector_settings()`,
`refuse()` and `export_spec()` (whose bytes are recorded nowhere). There is no `declare`,
no `deliver`, no artifact, no token and no work directory, so a preview has
nothing to act with, and one planning function serves `plan` and
`dispatch` — the preview shows what the dispatch does. `PlanPreview` carries
`text` (the plan as people read it, the same text a dispatch records),
`shards` (each shard the dispatch would declare, mapped to its item ids — an
empty list where the executor distributes on its own), `groups`,
`estimates_ms` (per shard, where history allows), `notes` (what a preview
cannot check, a cold start) and runner-defined `data`. A preview may refuse
for the reasons a dispatch would; the refusal is the answer, not an error
(an `InvalidParameter` it raises is the request's `400`, as above).
`RunnerBase.plan` says that the runner offers no preview; a runner that can
tell declares the `plan` capability, and every reference runner does. It is
reached through `POST /api/v1/plan`, `POST /runs/{id}/plan` and
`runcomposer plan` (§9) (tests/test_runner_contract.py, `TestPlanApi`,
`TestPlanCommand`).

**a) `robot-pool`** — Robot Framework executed on this machine in worker
processes (started with the `spawn` method, one per shard, at most
`max_workers` at a time, supervised by `supervise_workers`, §6.6). It is built on the Robot Framework toolkit
(`runcomposer.sdk.robot`, §6.6) and imports nothing else of runcomposer.

- *Plan.* How the selection meets the `partitions` (configured; chosen per
  run through the `partitions` multi-choice parameter) is
  `partition_mode`: `each` (default) runs the selection once *per* chosen
  partition — the same tests against every environment — each partition cut
  into duration-balanced chunks; `split` runs every test *once*, the chosen
  partitions being parallel lanes the selection is distributed over,
  balanced by duration. With `partition_mode_per_run: true` the mode is
  also a per-run parameter, `partition_mode` (a choice of `split` / `each`,
  default the configured mode), honoured alike by preview and dispatch.
  `max_chunks_per_partition: N` bounds how many chunks of one partition are
  cut, and so run at once — `1`: each partition runs one chunk at a time,
  so a shared environment is never used twice at once. History comes from the store over the last
  `history_depth` dispatches of runs matching `history_selector.labels`,
  restricted to `history_statuses` when set (`[PASS]`: a test that fails usually
  stops short of its normal length and would skew the balance), seeded durations filling the
  gaps (§7); round-robin on a cold start, and the plan says so. An item
  without a known duration weighs the mean of the known ones; the preview
  says how many were known and what the others weigh, gives the estimate
  per partition, and names an item that alone takes longer than an even
  share — the partition holding it is the longest whatever the split, so an
  uneven count of items per partition is the balance working. The Robot
  parser reports a test that passed or failed in under a millisecond as
  1 ms, since 0 means "not known" (tests/test_planning_weights.py). One chunk is
  one shard, declared with its item ids and grouped by partition. The
  planner is `plan_chunks` in the SDK, for any runner to reuse.
- *Pieces.* `max_tests_per_chunk` bounds a single Robot execution: a chunk
  is executed in pieces, one after another. Merged into one result per
  chunk by default; with `chunk_shards: true` each piece is a declared shard
  of its own (`<lane>-01`, `<lane>-02`, …), delivered as soon as it is done.
- *Exact selection.* Tests are chosen by exact id, not by name pattern: an id
  containing `[` or `*` is that one test. `suite_root` or `suite_roots`: with
  several roots each item is executed from the root it came from. Every
  item is checked against the roots, a single root included: an item belongs
  when the first segment of its id is the name Robot gives the root, and its
  recorded source lies under the root (an item the Robot source collected from that very root
  belongs whatever the suite calls itself). A mismatch — a catalog built
  from a parent directory, another copy of the suites — refuses preview and
  dispatch before the hand-off, naming the item and the root, instead of
  ending in `MISSING` items; `runcomposer doctor` runs the same check over
  the whole catalog (tests/test_robot_pool_options.py,
  `TestASingleRootIsChecked`). `robot_options: {name: …}` renames the
  top-level suite, so the ids begin with that name: the suite-root check,
  the exact selection — which renames the top suite before it matches,
  since Robot applies the name only after its pre-run modifiers — and
  `doctor` read the ids so (tests/test_robot_pool_suite_name_and_health.py,
  `TestARenamedTopSuite`). `rpa`
  (`true` / `false` / `null`, default `null`: Robot decides) sets the
  execution mode: a root that mixes tests and tasks needs it, since Robot
  refuses a selection keeping both — and that dispatch fails with Robot's own
  message and a hint naming the option.
- *Execution.* `variables` for all, `partition_variables` per partition, the
  partition itself under `partition_variable` (default `PARTITION`).
  `run_parameters` declares further per-run parameters (any of the seven
  kinds) and `parameter_variables` turns them into Robot variables — a value
  into the named variable, a `mapping` parameter (written `"*"`) into one
  variable per entry. `protected_variables` names Robot variables a per-run
  mapping may not set — matched as Robot matches names (case, spaces,
  underscores and `${…}` ignored) — refused as an invalid parameter by
  preview and dispatch. `executed_tag` (a template over `{partition}` /
  `{shard}`) is stamped on every executed test. `worker_setup` — configured
  callables — run inside each worker before Robot and may contribute
  variables; `pythonpath` and `listener` are passed on, `robot_options` are
  further keyword options for Robot's own `run`. `worker_cwd` (a path,
  relative to the config file) is the workers' working directory and
  `worker_env` is set in their environment — text, `${env:…}` like any
  option (tests/test_robot_pool_worker_surroundings.py). `robot_options`
  such as `removekeywords` and `flattenkeywords` shape the `log.html` and
  `report.html` Robot writes, never its `output.xml`; `clean_output:
  {remove_keywords: […], flatten_keywords: […]}` — Robot's own selectors,
  checked at startup — shapes the `output.xml` itself, and with it
  everything that is parsed, merged, recorded and pushed: every execution's
  output, re-executions included, is rewritten in the worker through
  Robot's result model, atomically, right after the execution, every
  test's id, status, message, tags and duration unchanged; the same
  selectors shape that execution's log and report, so they show what the
  output holds (tests/test_robot_post_execution.py, `TestCleanOutput`,
  `TestRobotOptionsShapeTheLogOnly`). `post_execution` names configured
  callables called in the worker after every execution, after the
  cleaning, as `step(output, info)`: the path of that execution's
  `output.xml`, and what `worker_setup` gets — the final `variables`
  included — plus `execution` (its 1-based index; `None` for a
  re-execution of failures), `attempt`, `rerun`, `root` and `item_ids`. A
  step that raises, or a cleaning that fails, fails that execution: its
  tests are `ERROR` with a message naming the step, its output is neither
  parsed, re-executed nor merged, and the shard goes on with its next
  execution (`TestPostExecution`). `rerun_failed: N`
  re-executes failures up to N times, each attempt delivered with its
  attempt number. Everything a shard executed is merged into one
  `output.xml` / `log.html` / `report.html` (`log`, `report`, default on) —
  without the "added from merged output" note a merge of disjoint executions
  would stamp on every test — and recorded as artifacts, together with the
  shard's directory itself (`files (<shard>)`, a recorded directory), so the
  files a log refers to are servable (§6.4) — recorded before the shard is
  delivered. Relative links in the HTML messages of a merged result — a
  file a library wrote and linked — are rewritten for where the merged log
  lies, so they reach the shard's files where they are served; an image a
  served log shows inline renders only when it is embedded (`data:`),
  since the artifact sandbox loads images from nowhere else (§6.4;
  tests/test_robot_merge_process.py, `TestLinksInMergedLogs`).
  `merge_groups: true` adds one merged `output.xml` /
  `log.html` / `report.html` per group and one for the run, each produced
  in a spawned process as soon as its shards are in and recorded as a group
  or run artifact (`log.html (group eu)`, `log.html (run)`) before the last
  shard delivers; a failed merge is a warning line, a cancelled dispatch
  merges nothing more, and no verdict depends on a merge
  (tests/test_robot_pool_options.py, `TestMergeGroups`).
- *Results* are parsed with the configured ResultParser (`format`, else the
  run's expected format, else `robot-output-xml`) — so a parser configured
  under `parsers:` applies to what this runner produces as well.
- *Refusals before the hand-off:* corpus drift (§3.3; `allow_drift`, also a
  per-run parameter, executes the intersection and reports the rest as
  `SKIP` / `drift`), a failing or overrunning `pre_run_hooks` command
  (`hook_timeout_s`), a missing suite root, an item no suite root holds, a
  protected variable, a `worker_cwd` that is not a directory, `worker_data`
  that cannot be sent to a worker process, and whatever a `plan_checks` or `pre_dispatch` step
  refuses. `plan_checks` — configured callables, free of side effects — run
  for a preview and for a dispatch alike, after the plan stands and before
  any readiness step, as `check(ctx, info)` with `info` holding
  `partitions`, `partition_mode`, `parameters`, `shards`, `groups`,
  `item_ids` and `plan`; a check may raise `InvalidParameter` or refuse, and
  a preview whose checks passed says so. `pre_dispatch` names configured
  callables called in
  the dispatching process before the hand-off as `step(ctx, info)`, where
  `info` holds the plan — `partitions`, `parameters`, `shards`, `groups`,
  `plan`, `item_ids`, `variables`, `workdir`. A step may `ctx.refuse`, log,
  keep files in `ctx.private_dir`, and return what goes into every shard of
  the dispatch: a mapping of Robot variables, or `{variables: {…}, env:
  {…}, worker_data: {…}}` — a mapping whose keys are only those three, each
  a mapping — whose `env` is added to `worker_env` and whose `worker_data`
  reaches `worker_setup` as `info["data"]`; later steps win, and `None` or
  an answer that is not a mapping adds nothing
  (tests/test_robot_pool_worker_surroundings.py,
  `TestWhatAPreDispatchAnswerMeans`): preparation that needs to know what
  is about to run.
- *Stopping.* Cancellation, and `shard_timeout_s`, stop worker processes;
  outstanding shards are not started. Each worker runs in a session of its
  own, so a stop reaches what a suite started, too; a worker says when its
  shard starts and ends in the run's event stream. Delivered shards are kept. A
  `pre_run_hooks` command still running is stopped by a cancellation too
  (with what it started), and the dispatch ends `CANCELLED`.
- *Live status* comes from a listener streaming through the live channel
  (`live_status`, default on): `RUNNING` when a test starts, its status when
  it ends. Workers log their progress into the run's event stream through
  the same channel.
- *Preview and health.* `plan` shows the shards, groups and estimates a
  dispatch would declare, and notes that readiness steps run only at
  dispatch. `health` data: `robot_version`, `max_workers`, `suite_roots`,
  `partitions`, `partition_mode` and `checks` — "Robot Framework
  installed", one row per suite root, then the rows of `health_checks`:
  configured callables called as `check(info)` with `{options,
  robot_version, suite_roots, partitions, partition_mode, max_workers}`,
  each answering one `{check, ok, detail}` row or a list of them; a failing
  or raising check makes the health `degraded` (tests/test_robot_pool_options.py,
  `TestHealthChecks`). The checks run side by side, and each answers within
  `health_check_timeout_s` (default 5) or is a failed row saying so — a
  check that overruns keeps running in its thread, but `health` returns
  (tests/test_robot_pool_suite_name_and_health.py, `TestHealthCheckTimeout`).
  With `worker_cwd` a row says whether it is a directory; with
  `robot_options: {name: …}` the data carry `suite_name`.
- *Configured callables.* Every option that names a callable —
  `worker_setup`, `pre_dispatch`, `post_execution`, `plan_checks`,
  `health_checks` — takes
  `"pkg.mod:callable"` or `{call: "pkg.mod:callable", with: {…}}`; the
  `with:` mapping reaches the callable as the keyword `settings`, in a
  spawned worker too (§8).

`examples/robot-pool/config.yaml` configures the plugin three times:
`robot-pool` (`each`, two partitions, reruns), `robot-lanes` (`split`,
two-test pieces as shards, a `mapping` and a `choice` parameter turned into
Robot variables, balanced on passing durations) — each run end to end by
tests/test_four_ways.py (`test_1_…`, `test_1b_…`) — and `robot-regions`
(the mode chosen per run, one chunk of a region at a time, protected
`SHOP_URL` / `CURRENCY`, merged results per region and for the run, outputs
cleaned of the keywords of passing tests), run by
tests/test_robot_pool_options.py (`TestTheShippedExample`); the switches are
tested in tests/test_robot_toolkit.py, tests/test_robot_pool.py and
tests/test_robot_pool_options.py.

**b) `jenkins`** — triggers an existing parameterized Jenkins job. It is
built on the Jenkins toolkit (`runcomposer.sdk.jenkins`, §6.6) and imports
nothing else of runcomposer. Options: `base_url`, `job` (folders as
`folder/job`), `parameters` — a mapping of *your job's* build-parameter
names to templates (below) — `completion: callback | poll`,
`callback_base`, `shards`, `artifact_paths`, `result_urls`,
`field_providers`, `postprocess`, `format`, `run_parameters`, `user` /
`api_token` (give them as `${env:…}`), `verify_tls` / `ca_bundle`,
`request_timeout_s`, `retries` with backoff on transient HTTP errors,
`poll_interval_s`, `timeout_s`, `max_parameter_bytes`, `max_request_bytes`,
`plan_provider` / `plan_fields`, `check_parameters`, `outage_tolerance_s`.
Every option that names a callable — `field_providers` values,
`postprocess`, `plan_provider` — takes `"pkg.mod:callable"` or `{call: …,
with: {…}}`, the `with:` mapping arriving as `settings=` (§8).

*What travels to the job* is configured, not fixed. A template may use the
document — `{spec_b64}` (the exact bytes, gzip + base64: one line of ASCII
that survives any form or shell, decoded with `base64 -d | gunzip`),
`{spec_url}` (where the job downloads the exact bytes; needs
`callback_base`) or `{spec_json}` (the document as text, for small specs
only: a build parameter is one form field and one environment variable of
the build) — identity (`{run_id}`, `{dispatch_id}`, `{title}`,
`{labels[key]}`), the way back (`{callback_url}`, `{token}` — bind it to a
password parameter), the work (`{item_ids}` one per line, `{shards}`
comma-separated, `{params[name]}` for the per-run parameters declared under
`run_parameters`: a list comma-joined, a `mapping` as `name=value` lines),
and every name under `field_providers` — a configured callable called as
`callable(ctx, fields) -> str` that may `ctx.refuse` a run the job has no
way to carry out (it is called for a preview too, so it must be free of side
effects) — and every name under `plan_fields` (below). Every template is
checked at startup against these names. A
trigger whose parameters exceed `max_parameter_bytes` (one value, default
128 000) or `max_request_bytes` (all of them, default 1 000 000; `null`
lifts either) is refused before anything is sent, with a message naming the
way out (`{spec_b64}` or `{spec_url}` instead of `{spec_json}`).

*The document is a visible build parameter, deliberately.* It carries no
credential: the ingest token is never part of it and no per-run parameter
kind is a secret, so everything in it — title, labels, selection, item
ids, per-run values — is what runcomposer itself serves for the run
(`GET /api/v1/runs/{id}/spec`), and the build page says exactly which
document was run. The token travels only as `{token}`, bound to a Jenkins
password parameter, which the REST API names without a value and neither
the parameters page nor the console shows — unless a shell step traces
its commands (`set -x`, or a step without a shebang line, which Jenkins
runs with `sh -xe`), since Jenkins does not mask it by itself. The cost is
size: every build parameter reaches the build step as an environment
variable, which on Linux must stay under 128 KiB or the step cannot start;
`max_parameter_bytes` (128 000) refuses such a trigger before it is sent,
and `{spec_url}` keeps the document out of the parameters altogether.
`ci/jenkins/e2e.py` checks each of these against a real Jenkins
(`what_jenkins_shows_of_the_parameters`).

*Shards.* `shards` names what one build reports separately — a list, or a
template rendering to a comma- or newline-separated list
(`"{params[regions]}"`); default `["1"]`. With `callback` the job pushes
each with `shard=<name>`. With `poll`, `{shard}` in `artifact_paths` /
`result_urls` tells their files apart (or a `postprocess` splits what was
downloaded); several shards that could not be told apart are refused before
the trigger.

*A plan of its own.* For a job that cannot say which item goes where,
`plan_provider` is a configured callable called by the preview and the
dispatch alike as `(ctx, fields)` — free of side effects, it may
`ctx.refuse` — returning any of `"shards": {name: [item ids]}` (they replace
what `shards` says, are declared with their items and shown in the
preview), `"groups": {shard: label}` and `"fields": {name: text}`, the
template fields listed under `plan_fields` so the templates can be checked
at startup. What it returns is checked before anything is sent; a wrong
shape or an item that is not planned fails the dispatch, or the preview,
naming the provider (tests/test_jenkins_followup.py, `TestPlanProvider`).

*How results come back.* With `callback` the job POSTs its bundle to
`{callback_url}` using `{token}` — and may report progress through the same
token (§5); `dispatch` returns once the build is queued and the run waits in
`AWAITING_RESULTS`. With `poll`, runcomposer follows the queue item the
trigger returned to *its* build — never "the latest build of the job" —
records the build URL as the dispatch's `build` link and as an artifact,
waits for it, downloads what matches `artifact_paths` (glob patterns over
the archived files; default `results/*` when no `result_urls` are given)
and the `result_urls` — paths below the build's URL that a publisher serves
without archiving, such as `consoleText` — runs `postprocess`
(`callable(ctx, shard, directory) -> Path | None`; the path it returns is
what is delivered) and delivers each shard. A shard the job already pushed
is not downloaded again (`ctx.outstanding_shards()`). Following a build
rides out a Jenkins that stops answering for up to `outage_tolerance_s`
seconds since it last answered (a restart) — without it, a request that
still fails after the bounded retries fails the dispatch — and the timeout
is not extended by it (tests/test_jenkins_followup.py,
`TestFollowingThroughAnOutage`).

*Stopping.* `cancel` stops the build through its recorded `build` link when
there is one; otherwise it asks the queue item what it became — a running
build, also when Jenkins has already forgotten the queue item (it does so a
few minutes after the build started; the build is then found by the queue
id it records), or a queued item, which is cancelled. A cancel that arrives
while the trigger is under way is handled by the runner itself: it takes the
build back and the dispatch ends `CANCELLED`. A rejected trigger is a
refusal; a build that never finishes is a `FAILED` dispatch, and so, with
`poll`, is a queue item somebody cancelled in Jenkins before it started.

*Preview and health.* `plan` shows the build parameters that would be sent —
long values as `‹N bytes›`, the token and the dispatch id as `‹at
dispatch›` — and the shards, and triggers nothing; it cannot say which item
goes to which shard, since the job distributes them — unless a
`plan_provider` says. `health` data:
`version`, `job_url`, `completion` and `checks` (`{check, ok, detail}` for
"server reachable" and "job readable"). Jenkins ignores a build parameter
its job does not declare — silently: the build runs, the value never
arrives — so `check_parameters: true` adds a row comparing the names under
`parameters` with the job's declared parameters; a name the job would drop,
or a job that declares none, makes the runner `degraded`
(tests/test_jenkins_followup.py, `TestDeclaredParameters`).

The CI side needs a thin consumer — a job step that runs `runcomposer-exec`
(§6.2c) and, for `callback`, a POST of the bundle (or `runcomposer-exec
--push`). `examples/jenkins/config.yaml` configures four entries:
`jenkins` (callback) and `jenkins-poll` (poll) for a job that runs the
whole selection, and `jenkins-fanout` (poll; shards from a per-run
parameter, the document as `{spec_b64}`, a field computed from the
selection, results cut out of the console log by a `postprocess`) and
`jenkins-fanout-push` (callback; the job pushes each region with progress)
for a job that runs it once per region — their two callables in
`examples/jenkins/shop_job.py`. A reproducible Jenkins-in-docker setup with
both jobs is in `ci/jenkins/`. What is verified how: the test suite runs the
runner against an in-process HTTP stand-in that speaks the Jenkins endpoints
it uses (crumb + session cookie, queue item → build, a queue item that
expired, artifact listing and download, URLs below the build, stop, cancel)
— tests/test_jenkins.py, tests/test_jenkins_toolkit.py,
tests/test_jenkins_followup.py, and the fan-out
entries in tests/test_four_ways.py (`test_2b_…`); `ci/jenkins/e2e.py` is
the manual end-to-end check against a real Jenkins.

**c) Export / remote round-trip** — for executors runcomposer never talks to
(a remote checkout that receives code via git and returns result bundles the
same way). Exporting a run hands out the document — exact bytes, kept and
re-downloadable (`GET /runs/{id}/dispatches/{dispatch_id}/spec`), their hash
recorded — and mints the ingest token (§5). A deliberately tiny consumer,
**`runcomposer-exec`**, reads the spec → writes `materialized.item_ids` one
per line into an ids file → runs a caller-supplied command template
(`--command`, placeholders `{ids_file}`, `{run_id}`, `{out_dir}`, replaced
literally) → writes the `runcomposer_run.json` marker next to the outputs,
**last and only if the command exited with an accepted code**
(`--ok-exit-codes`, default `0`), so a failed execution never looks like a
complete bundle. It stays framework-agnostic on purpose: rendering a
*runner-native* invocation is the job of the command it calls —
`examples/remote-agent/` does exactly that. Passing `runner.*` values through
to that command is `[planned]`; nothing on the exec path reads the `runner`
section today.

Talking back is optional and needs nothing but the stdlib either. With
`--callback URL` (the run's results URL, as a runner hands it out) and the
dispatch's ingest token (`--token`, or `RUNCOMPOSER_INGEST_TOKEN` in the
environment) the consumer reports while it works through the progress
endpoints (§5): a log line when it starts and, after a `--command`, one
with the command's exit code; with `--simulate` also the live status of
every item. Progress is best-effort —
a report that cannot be delivered is said once and dropped, and the
execution goes on. `--push` uploads the finished bundle to that URL as one
multipart POST, so no other transport is needed; a push that is refused or
cannot be delivered is an error of the consumer (exit `4`), with the bundle
and its marker left on disk (tests/test_runner_contract.py,
`TestTheRemoteChannelAndTheConsumer`). Behind a private certificate
authority, `--ca-bundle PATH` (default `RUNCOMPOSER_CA_BUNDLE`) names the
certificates to trust; `--no-verify-tls` (default `RUNCOMPOSER_VERIFY_TLS`,
true or false) skips the check (§5; tests/test_remote_tls.py,
`TestTheConsumerOverHttps`).

**`runcomposer-exec` distribution — decided: both, by construction.** It is a
single self-contained **stdlib-only** Python file (reads the spec as JSON;
YAML only if a parser happens to be importable), distributed both as part of
the package and as that same file published per release for vendoring.

**d) `command`** — the afternoon-sized connector: runs a configured command
per shard on this machine and delivers what it wrote through the configured
parser. `command` (a string for the shell, or an argv list) with the
placeholders `{ids_file}` `{out_dir}` `{spec_file}` `{run_id}`
`{dispatch_id}` `{shard}` — replaced literally, nothing else in braces is
touched — plus `format`, `shards`, `max_parallel`, `cwd`, `env`, `timeout_s`,
`ok_exit_codes`, `allow_drift`, and declared per-run `parameters` (handed to
the command as `RUNCOMPOSER_PARAM_<NAME>`; a `mapping` as `name=value`
lines). Any framework that starts from a command line and writes a result
file is connected by configuration alone. Its `plan` names the shards and
the items each would run. Each shard's command is started by a supervised
worker process (`supervise_workers`, §6.6) in a process group of its own:
a cancel, or `timeout_s` (counted from the worker's start), stops the
command and whatever it started, and a cancel that lands before the
command starts runs nothing (tests/test_command_runner.py).

**e) `demo`** — **simulated**. No test is run: verdicts derive from a seed
(a per-run parameter), so the compose → dispatch → results loop can be shown
without a test framework. It says so itself (`simulated` capability, every
failure message). With `delay_s` (configured only) each item takes that
long, streaming `RUNNING` and then its status, so live progress and
cancellation can be watched. Only dispatchable where it is configured.

### 6.3 `RunStore`

Persists runs, specs, dispatches (state, declared shards, their items and
their groups, plan, links, error, parameters, outcome summary, owner, cancel
request), the exact bytes handed out per dispatch, ingest-token hashes,
deliveries, verdicts, live status (as `LiveStatus` records, `RUNNING`
included — `record_live_verdict` / `live_verdicts`), artifact references
(with their shard or group), imported durations and the event stream. **The
store records; it decides nothing** — lifecycle state is computed by
`core.lifecycle` and written by the application's ledger, the one place that
does.

The history-query surface is explicit because §7 depends on it:

- `latest_completed_run(*, completed_before=None, labels=None)` — "latest"
  means latest *completed*, and unscoped it ranges over every completed run
  in the store (§7);
- `verdicts_for(run_id, dispatch_id=None, *, shard=None)`, every verdict
  carrying its `shard` and `attempt`;
- `list_runs(state, labels, since, until, q, limit, offset)` → a page plus
  the total, each run carrying its dispatches — what a run list needs without
  a query per row. Times are compared as stored, so a run's `created_at` is
  stored normalized to UTC whatever spelling its document uses;
- `duration_aggregates(labels, last_n, statuses=None)` — the mean over the
  last `last_n` dispatches that delivered, of verdicts with a duration above
  0 (0 means "not known") and, with `statuses`, one of those statuses; an
  item nothing was measured for falls back to its seeded duration;
- `seed_durations(durations, labels=None)` — keep imported durations as
  that fallback (§7), scoped by labels the way a run's labels scope its
  history; importing an item again under the same labels replaces its seed;
- `events(run_id, after, limit)`, and `latest_events(run_id, limit)` — a
  bounded read of the tail, which is what a run detail shows.

A dispatch's groups are written with `update_dispatch(shard_groups=…)` and
read back as `DispatchRecord.shard_groups`; an artifact's group goes in with
`add_artifact_ref(…, group=…)` and comes back as `ArtifactRef.group`. The
conformance kit checks all of it (`check_store`: `store.shard-groups`,
`store.live-running`, `store.duration-statuses`, `store.duration-seeds`,
`store.atomic`).

**`atomic()`** is a block of store calls taken as one unit: every call the
thread makes inside it is atomic together — no other writer, another thread
or another process sharing the store, writes between its reads and its
writes; an exception undoes what the block wrote; nested blocks join the
outermost. The application's ledger takes every lifecycle decision inside
one — read the delivered shards and verdicts, evaluate, write the
completion and the headline — and emits the decision's events after the
commit, so a delivery, a cancel or a recovery in another process lands
before or after a decision, never in between, and a reader woken by an
event finds the state it announces (tests/test_two_processes.py,
tests/test_store_atomic.py). `StoreBase.atomic` guarantees nothing beyond
what each call does: a store shared between processes provides its own.

`add_dispatch(..., exclusive=True)` refuses (`RunActive`) when the run's
latest dispatch is in flight, checked inside the transaction that inserts:
that is what keeps two processes — a server and a CLI, two CLIs — from both
dispatching one run; the dispatch manager's own lock only covers its
process.

Reference impl: `sqlite` (default, zero-setup), safe to share between the
threads of a server and between processes on one file: WAL journal and a busy
timeout, one connection per thread, every write one short
`BEGIN IMMEDIATE` transaction — so `record_delivery`'s check-then-write
cannot interleave with another — and `atomic()` one `BEGIN IMMEDIATE`
transaction around the whole block, each call inside it a savepoint of its
own. Labels are matched as data (`json_each`), never through a JSON path a
key could break. It needs SQLite 3.24.0 or newer (the upsert) with the JSON
functions, and refuses to open otherwise, naming the version found and the
one required (tests/test_store_sqlite_version.py). `path: ":memory:"` is supported for tests and
throwaway use.

**Schema version and migration.** The sqlite schema is versioned in
`PRAGMA user_version` — version 3 today — and migrated in place: from
version 2 by adding the dispatches' `shard_groups`, the artifact
references' `shard_group` and the `duration_seeds` table. A database written
by 0.1.x opens as it is: deliveries without a dispatch get an `ingest` dispatch, where
0.1 left several deliveries for one shard the newest wins, a plaintext token
in a stored document is replaced by its hash, a dispatch that was executing
is `STALE` — and completion is **recomputed** under §4's rules, so a run 0.1
called `PASS` although planned items never reported is `INCOMPLETE`. A
database from a newer schema is refused.

**Port evolution.** 0.2 is a breaking revision of every port. From here the
ports grow by *addition* within a minor line: new members, and keyword-only
parameters with defaults on existing ones. `RunStore` is the wide one — 33
members, `close`, `atomic` and `store_id` among them — and may gain members
in a minor release, with a default in `StoreBase` wherever a default can be
right; a store of your own that lacks a new member is refused at startup
naming it, and `check_store` in its tests says so first (§6.6).

A second store over `postgres` is `[planned]`; until it exists, an adopter
who must live in an existing database writes their own against this port.
Stated plainly: **history features are dark on a fresh store** — failed-rerun
and duration-balancing activate as runs accrue. The one store that starts
warm is the demo's (§12); duration balancing can be warmed by importing
known durations (`runcomposer history import`, §7).

### 6.4 Artifacts

Not a port: the store records artifact references
`(name, media_type, url_or_path, shard, group)` and reads them back. They
surface in `GET /api/v1/runs/{id}` under `artifacts`, each resolved to
something a reader can follow and carrying its `shard` and `group`.

**`url_or_path` is one field with two meanings**, and the resolution is
explicit about which:

- an absolute **`http`/`https` URL** is a remote artifact (a CI build link).
  It passes through as the href verbatim; runcomposer never fetches, proxies,
  or validates it. Any other scheme — `file:`, `data:`, `javascript:` — is
  deliberately *not* treated as a URL: handing one out as a link is a
  link-injection vector.
- anything else is a **local path**, servable only when it really lies inside
  the recording run's directory under `core.artifact_dir`. A path outside
  stays visible in the payload with no href (kind `external-path`), rather
  than offering a link that cannot work. A recorded **directory** is listed
  with kind `directory` and no href: it is not a document to open, but every
  file below it is servable — a report together with the images and
  screenshots it refers to. Hidden files and directories below it (a tool's
  own bookkeeping, such as a worker's hand-back file) are not part of that:
  they are served only when recorded by themselves.

So every listed artifact carries a `kind`: `url` (a link; `href` is the
URL), `local` (a recorded file; `href` is the path it is served under),
`directory` (a recorded directory; no `href`, its files are servable) or
`external-path` (outside the run's artifact directory; listed, never
served). **Over HTTP, `url_or_path` never says where files lie on the
server's machine:** it is the URL for a link, the path *relative to the
artifact directory* for a file or directory inside it, and the bare file
name for one outside. The CLI, which runs on that machine, shows the
recorded path as it is (tests/test_api.py).

A built-in route serves `/artifacts/{run_id}/{dispatch_id}/…` — the layout of
`ctx.workdir` — and **only what was recorded**: a file a runner recorded with
`add_artifact`, or a file under a directory it recorded, by the run whose
directory the file lies in. Anything else in the artifact directory answers
`404`, exactly like a file that does not exist. A file is downloadable
because a runner said so, not because it happens to be in a served tree —
so an intermediate file, a token a hook wrote or another run's file is never
handed out by accident (tests/test_api.py,
`test_only_recorded_artifacts_are_served`,
`test_a_recorded_directory_makes_its_tree_servable_and_nothing_beside_it`,
`test_a_file_recorded_by_another_run_is_not_served_from_this_runs_directory`).
What must never be downloadable, whatever is recorded, belongs in
`ctx.private_dir`: a directory per dispatch under `core.private_dir`
(default `private/` next to the config file), which must lie outside
`core.artifact_dir` — a configuration that puts it inside is refused at
startup.

Two safety properties of the route, both deliberate:

1. **Containment, checked after resolution.** Artifact paths are
   attacker-influenced. The request path is resolved fully and refused unless
   it lands strictly inside the configured directory, so `..` segments,
   absolute paths, and symlinks pointing out of the tree are one rule rather
   than three patches. A refusal is a `404`, identical to a miss, and so is
   a path no file system can hold (a NUL byte, an over-long name)
   (tests/test_api_hostile_names.py).
2. **Sandboxed delivery.** Artifact bytes are untrusted content served from
   the same origin as the API and the UI. They go out with
   `Content-Security-Policy: default-src 'none'; sandbox` and
   `X-Content-Type-Options: nosniff`, so an artifact cannot script against
   this origin. A `text/html` artifact — a framework's log or report is an
   HTML *application* and is a blank page without its own script — gets
   `sandbox allow-scripts` with inline script and style and embedded images
   allowed and everything else still denied: without `allow-same-origin` it
   has an origin of its own and cannot read or call this server as the user,
   `default-src 'none'` lets it load and connect to nothing, and
   `form-action 'none'` lets it post nothing.

**Retention is config, not an afterthought:** `core.retention.max_age_days`
(`null` = no age limit) and `max_runs`, applied by `runcomposer gc`.
Artifacts live and die with their run: pruning or deleting a run removes its
directory — and its private directory — and a directory that belongs to no
stored run is swept from both roots.

### 6.5 `RunObserver`

```python
def notify(event: RunEvent) -> None
```

Configured under `observers:` — several are allowed — and told about the
lifecycle events of §4 (per entry, `events:` narrows or widens that with a
list of kinds, `*` wildcards allowed). Observers are called on a thread of
their own, in event order: a slow one delays other notifications, never a
dispatch. **An observer that raises is recorded as a warning event and
affects nothing else.** This is one callback per state transition, not a
scheduler (§13).

Shipped: **`webhook`** (POST the event as JSON; with a `secret` — give it as
`${env:…}` — the body is signed, `X-Runcomposer-Signature: sha256=<HMAC>`),
**`jsonl`** (append one line per event to a file), **`command`** (run a
command with the event in its environment — `RUNCOMPOSER_EVENT`,
`RUNCOMPOSER_RUN_ID`, `RUNCOMPOSER_STATE`, `RUNCOMPOSER_COMPLETION`, … — and
as JSON on stdin; never on the command line).

### 6.6 The SDK

`runcomposer.sdk` is the connector surface: the protocols (the five roles,
`DispatchContext`, `PlanContext`, `LiveChannel`, `History`), their base
classes (`SourceBase`, `RunnerBase`, `ParserBase`, `StoreBase`,
`ObserverBase`, all derived from `PluginBase`), the records (`LiveStatus`
and `PlanPreview` among them, `LIVE_STATUSES`), the error taxonomy, and the
helpers a connector needs — the chunk planner, content hashes, the marker
reader, the defused XML reader, lifecycle functions, parameter validation,
`load_import_path` and `ConfiguredCall` / `configured_calls` (for a
plugin's own options that name a callable, in either spelling, §8), the
filter's text form (`parse_filter_text`, `read_filter_string`,
`quote_pattern`, `format_filter`, `FilterSyntaxError`, §3.1),
`RemoteLiveChannel` and `push_results` (an executor's progress and its
results bundle, pushed to the API — over HTTPS with `ca_bundle` /
`verify_tls`; a refused push raises
`ResultsPushRefused` with the status and the server's answer, §5) — plus
`load_config` and `Service` for driving
runcomposer from Python (compose → dispatch → wait → read, in one process).

Beside them, the SDK carries the supervisor of worker processes for any
framework — `supervise_workers` with `WorkerPayload` and `WorkerOutcome`:
one spawned process per shard, shards of one lane in order and lanes side
by side, at most `max_workers` at once, a per-shard timeout, the result the
worker's function returns handed back, a line in the log when a shard
starts and when it ends, and one stop protocol — ask (`stop()` becomes true,
`.stop-requested` appears in the shard's directory, `SIGTERM` to the
worker's process group, so what the worker started is asked too), after
`grace_s` terminate, after another `grace_s` kill, then kill what is left
of the group before the worker is reaped. A payload or a function that
cannot be sent to a spawned process is refused before anything starts. The
Robot toolkit's `supervise` and `spawn_merge` (one payload, the merge as
its target) and the `command` runner are built on it — one implementation
of starting, stopping and reaping a worker
(tests/test_worker_supervisor.py, tests/test_robot_merge_process.py).
`WorkerPayload` has no hook after an execution: the target is the author's
own function, and what follows an execution is its next line.

It is a package with **two toolkits** — the parts `robot-pool` and `jenkins`
are assembled from:

- `runcomposer.sdk.robot` — exact-id selection (`ExactSelection`), the
  live listeners (`LiveStatusListener`, and `RemoteLiveStatusListener` for
  an agent that executes elsewhere), what one worker is told and its entry
  point (`ShardPayload` — with a working directory, an environment and data
  for the worker's setup, the cleaning of every output and the steps after
  every execution — and `run_shard`; a `ShardOutput` a step failed on
  carries `failed` and the `item_ids` reported `ERROR` for it),
  `clean_output` (one `output.xml` rewritten without the keyword data
  Robot's selectors name), the supervisor of Robot
  workers (`supervise`, on `supervise_workers`: a bound on parallelism,
  lanes that must not overlap, a per-shard timeout, the stop protocol),
  delivery of what a worker produced, its artifacts recorded first
  (`deliver_shard`, `collect_verdicts`), merging (`merge_outputs` with
  `rebot_options`, and `MergePayload` / `run_merge` / `spawn_merge` for a
  merge in a spawned process, so the dispatching process never loads
  Robot's result model; `NameTopSuites`), the suite-root check
  (`attribute_roots`, `root_mismatches`, both with `name=` for a renamed
  top suite), `robot_version` (read from the installed distribution's
  metadata);
- `runcomposer.sdk.jenkins` — `JenkinsClient` (session and crumb, bounded
  retry, TLS, trigger → queue item → build, lookup by queue id once the
  queue item is gone, stop and cancel, archived artifacts and any URL below
  a build, the parameters a job declares (`job_parameters`), and `follow`: a triggered build from its queue item to its end,
  taken back when the dispatch was cancelled before a link existed to stop
  it by, riding out an outage of up to `outage_tolerance_s`), `JenkinsError` / `JenkinsTimeout` /
  `JenkinsQueueCancelled` (a queue item cancelled in Jenkins), and `encode_spec` /
  `decode_spec` for the compressed spec.

Why they are exported rather than kept inside the runners: a connector whose
needs differ from what the reference runners' switches offer should compose
the same parts — the Robot worker and its stop protocol, the Jenkins queue
semantics — instead of restating them, and restating them is where such
details go wrong. `robot-pool` and `jenkins` are built on the toolkits and
import nothing else of runcomposer, which is the evidence that the toolkits
suffice; three composed example runners in the test suite, written against
the SDK and at most one toolkit, run through the kit and a real deployment
(`LaneRunner` in tests/test_robot_toolkit.py, `NightlyRunner` in
tests/test_jenkins_toolkit.py, and `CheckRunner` in
tests/shop_check_runner.py — plain Python checks in supervised workers, no
toolkit at all).

**What is stable for the 0.2.x series:** the names in
`runcomposer.sdk.__all__`, in the two toolkits' `__all__` and in the kit's
(`runcomposer.testing`, `runcomposer.testing.jenkins`), with their
documented meaning; the run-spec document (`runspec/1.0`); the error codes;
and the reference runners' documented options. A removal or a change of
meaning is a breaking change listed in the changelog. **What is
provisional:** `Service` and `Config` are exported whole, but only the
members the connector guide names are stable; the `RunStore` protocol may
gain members in a minor release (§6.3); and responses may gain fields — a
client ignores the ones it does not know. Guard tests
hold the shape: frozen lists of the names of the SDK and of each toolkit,
importing the SDK loads neither toolkit nor Robot Framework — nor the YAML
reader or the document validator, so an executing machine needs neither
(tests/test_minimal_imports.py) — the Robot
toolkit and `robot-pool` import without Robot Framework installed (it is
imported only inside the functions that execute), the toolkits import only
`runcomposer.sdk` from this package, the SDK has exactly these two
toolkits, and the reference runners are built on them (tests/test_sdk.py,
tests/test_self_containment.py).

**What a server process loads of Robot Framework.** Importing any part of
the `robot` package loads all of it, its process-global logger included.
So `robot_version()` reads the distribution's metadata, the suite-name rule
is restated (and compared with Robot's own), the parser reads the file
itself (§5), and merges run in a spawned process: with a catalog that is
not read through Robot, a server that answers `health`, previews and
dispatches with `merge_groups` has no `robot` module loaded at any point.
The `robotframework` catalog source does load Robot when it builds the
catalog — that is its nature — and the runner adds nothing to what the
source loaded (tests/test_robot_server_imports.py).

**The contract is checked, not discovered.** Every member the application
calls on a plugin is a member of its protocol; the optional ones have
defaults in the base classes. When a configured plugin is built, its class
is checked for the `Configurable` members (`OPTIONS`, `CONNECTOR`,
`resolve_config_paths`) and the built object for every member of its role's
protocol (`contract_members(protocol)` is that list); a missing one is a
`ConfigError` naming it. Afterwards members are called directly — a guard
test keeps `getattr`/`hasattr` off plugin objects in the layers above the
plugins.

**The hand-off order** — refuse only before `declare`, `declare` exactly
once, `deliver` only after it — and the shape checks on what is declared
and delivered are one piece of core code (`core.handoff`) used by the real
`DispatchContext` and by the kit's fake one, so the two refuse the same
misuse with the same message.

**The conformance kit** — `runcomposer.testing`, a sibling surface to the
SDK (plugins import the SDK at run time, tests import the kit), stable on
the same terms: `check_source`, `check_runner`, `check_parser`,
`check_store`, `check_observer` run a port's invariants against a caller's
instance and fail naming the violated rule — for a runner with the `plan`
capability also `runner.plan`, `runner.plan-is-read-only` and
`runner.plan-matches-dispatch`, for a store `store.atomic`, and for an
observer that passes on only some kinds `check_observer(kinds=…)`;
`FakeDispatchContext` records what a runner
does — the whole live stream in order (`live_updates`, `RUNNING` included)
next to what a store would hold (`live_now`), groups, links, worker log
lines, `private_dir`, `delivered_shards()` / `outstanding_shards()` — and
previews a runner (`preview`), with options and parameters apart as in a
real dispatch; `ConnectorHarness` builds a real `Service` in a temporary
directory and drives compose → dispatch → settled with a timeout, starts a
dispatch to follow and cancel (`start` → a handle with `wait_until`,
`cancel`, `read`), previews (`plan`), and with `serve=True` serves the API
on a free port for an executor that pushes (`RUNCOMPOSER_HARNESS_URL` and
`RUNCOMPOSER_HARNESS_PORT` name it before the plugins are built;
`advertise_host` is the name an executor in a container reaches it by).
`runcomposer.testing.jenkins`
holds the Jenkins protocol stand-in the reference runner is tested with
(`FakeJenkins` — jobs that declare their parameters and drop the others as
Jenkins does, and a server that goes `down()` and comes back `up()`), so a connector's own Jenkins runner is tested against the
same thing; `plugin(...)` builds an installed plugin by its config name. Every reference plugin is tested
with it — the evidence that it suffices (tests/test_kit_additions.py,
tests/test_testing_kit.py).
`examples/connector-template/` is an installable connector package for the
example world, tested with the kit; [docs/connectors.md](docs/connectors.md)
is the author's guide.

---

## 7. History-based selection ("rerun what failed")

A **selection provider** resolved at compose time — the same
resolve-then-materialize pattern taxonomy refs use:

```
compose-time: RunStore query → item ids materialized into selection
            → spec records derived_from provenance (§3.1)
```

In the UI, "rerun what failed" is an action of a run — the run view's rerun,
which composes `failed+error+missing@run:<id>` like `rerun --scope failed`.
A free picker over selectors and scopes — `Select items by history:
[failed] in [latest completed run | run <id> | last run before <date>]
(filter: labels)` — is `[planned]` in the UI. Every selector and scope below
is reachable from the CLI and the API.

The executed spec stays static and reproducible; provenance keeps it
auditable. Locale date words are UI sugar, never spec content. The
run-completion vs. item-verdict distinction (select the run by *completion
status*, filter items by *verdict*) is carried by §6.3's two-level history
contract.

**The label scope is part of the query, not decoration.** Without one,
`latest` ranges over every completed run in the store, whoever composed it
and for whatever purpose. On an instance with more than one stream of runs —
a nightly regression next to people's one-off selections — the most recent
completed run is then often not the one a rerun was meant to follow, and a
rerun derived from the wrong run is a perfectly valid, much smaller run that
gives no hint of the mix-up. So the machine-readable query syntax carries the
scope:

```
<verdict>[+<verdict>…]@<selector>[?key=value&key2=value2]
                                         # verdict: failed | passed | skipped | error | missing

failed@latest                            # every completed run is a candidate
failed+error+missing@run:01JZ8ZZ…        # everything that did not pass — what
                                         #   `rerun --scope failed` composes from
failed@latest?suite=nightly              # only runs labelled suite=nightly
failed@before:2026-07-02T00:00:00Z?suite=nightly&env=staging
failed@run:01JZ8ZZ…                      # explicit run; a scope here is REFUSED,
                                         #   not ignored — it could only mislead
```

`?` and `&` occur in neither ISO-8601 timestamps nor run ids, so the suffix is
unambiguous; percent-escapes are decoded, so a label value may contain either.
The same scope is available as `runcomposer runs --failed-in latest --label
suite=nightly`, in the API as `selection.history` — the string above, or
`{query, labels}` — and as the `labels=` argument of
`Service.resolve_history`; a key supplied twice with different values is
refused rather than silently resolved. Items are selected by their
**effective** verdict in the resolved run's latest dispatch, `MISSING`
included; the preview (`POST /selection/compile`) reports which run a history
selection resolved to before anything is composed.

Provenance records both halves of "which run, and why that one" — the query
(selector **and** scope) plus the run it resolved to, its `completed_at`, and
the labels it actually carried:

```yaml
derived_from:
  - provider: history
    query: { run: LATEST, verdicts: [FAIL], labels: { suite: nightly } }
    resolved_run_id: "01JZ8ZZ..."
    resolved_run_completed_at: "2026-07-02T03:14:00Z"
    resolved_run_labels: { suite: nightly, env: staging }
```

The idiom for a recurring stream is to scope the lookup and label the new run
the same way, so the rerun joins the stream it reran:
`runcomposer spec --from-history 'failed@latest?suite=nightly' --label
suite=nightly`.

A rerun of failures (`rerun --scope failed`, `POST /runs/{id}/rerun`) keeps
the source run's title unless it is given one (`--title`, `title`): that it
is a rerun, and of what, is data — origin `rerun`, `derived_from`, and
`rerun_of: {run_id, scope}` in the answer and in the new run's detail — not
prose in a title (tests/test_runner_contract.py, `TestRunDetailSemantics`).

**Duration history.** The other half of history is how long items take —
what duration-balanced planning reads (`ctx.history.durations`, §6.2). It is
the mean over the last N dispatches that delivered, of runs carrying the
given labels, optionally restricted to executions that ended with given
statuses (`statuses=("PASS",)`, a runner's `history_statuses`): a test that
fails usually stops short of its normal length, and its chunk would look short. A duration of 0
ms means "not known" to the store and to the planner alike, never "instant".
A fresh store knows nothing, so a deployment that already knows how long its
tests take can **seed** it: `runcomposer history import FILE [--label k=v]`
reads a CSV (a header naming an `item` column and `duration_ms` or
`duration_s`) or JSON (`{"durations": {item: ms}}`, or a list of `{item,
duration_ms}`), resolves every name through the catalog — an item id or any
native name the source resolves — and stores the durations under the
given labels; names the catalog does not know are reported, not stored. A
seed answers only for an item nothing was measured for: the first real
execution takes over. `runcomposer history durations [--label] [--last N]
[--status S]` prints the means as a planner sees them.
`examples/history/durations.csv` is a sample in the example world
(tests/test_runner_contract.py, `TestDurationHistory`).

---

## 8. Configuration

```yaml
# config.yaml — one file, layered sections
core:
  api: { host: 127.0.0.1, port: 8100, cors: [...],
         docs: false,                  # true: also serve the /docs and /redoc viewers (§9)
         hosts: null }                 # the host names it answers to; null: the loopback
                                       #   names on a loopback bind, else every name (below)
  taxonomy_file: taxonomy.yaml        # curated tree over tag patterns; format: docs/taxonomy.md
  artifact_dir: artifacts/
  private_dir: private/               # ctx.private_dir — never served; outside artifact_dir
  retention: { max_age_days: 90, max_runs: null }      # null = no limit
  dispatch: { max_concurrent: 2, handoff_wait_s: 2.0,
              awaiting_timeout_s: null,                # null = wait for results until
                                                       #   finalized or cancelled (§4)
              recovery_interval_s: 15 }                # serve's recovery pass; null = at startup only
  ingestion: { tokens: required, inbox: results_inbox/, max_upload_mb: 200,
               quarantine_dir: quarantine/, quarantine_max: 100,
               poll_interval_s: 2.0, settle_s: null }
  locale_default: en
  text_language: en                    # BCP 47 tag of the free text the server and its runners
                                       #   write (error details, health reasons, plan text)
store:                                 # exactly one entry
  sqlite: { path: runcomposer.db }
sources:                               # one or more → one catalog (§6.1)
  shop-ui:  { plugin: robotframework, roots: [ui/tests, api/tests] }
  shop-api: { plugin: manifest, path: api-catalog.json }
runners:                               # the key IS the runner id
  robot-pool: { suite_roots: [ui/tests, api/tests], max_workers: 8,
                partitions: [staging, preview] }
  nightly-ci: { plugin: jenkins, base_url: "${env-public:SHOP_CI_URL}", job: shop/nightly,
                user: runcomposer, api_token: "${env:SHOP_CI_TOKEN}",    # a secret
                callback_base: "https://runcomposer.shop.example" }
  smoke:      { plugin: command, command: "./run-smoke.sh {ids_file} {out_dir}",
                format: junit-xml }
parsers:                               # options per result format id
  junit-xml: { pattern: "TEST-*.xml" }
observers:
  team-chat: { plugin: webhook, url: "https://chat.shop.example/hooks/tests",
               secret: "${env:SHOP_HOOK_SECRET}", events: [run.completed, run.failed] }
ui:                                    # served at /api/v1/ui-config, /meta and /presets
  quick_filters:                       # filter text (Smoke, "Auth OR Cart") or a filter AST
    - { label: "Cards", filter: Payments-Cards }
    - { label: "Checkout, not quarantined",
        filter: { op: AND, items: [Checkout, { not: "prefix:Quarantine-" }] } }
  default_filter: Regression           # what a new selection starts from
  presets:                             # named, complete starting points
    - { name: nightly-smoke, label: "Nightly smoke", filter: Smoke,
        runner: robot-pool, parameters: { partitions: [staging] },
        title: "Nightly smoke", labels: { suite: nightly } }
connectors:                            # free-form: a connector package's own settings
  shop-connector: { ... }
```

Rules:

- **Plugin sections share one spelling.** The key of an entry is its id; the
  plugin class comes from `module:` or `plugin:` inside it and defaults to the
  key as an entry-point name (§6). Every other key of the entry is an option
  handed to the plugin's constructor. One plugin may be configured several
  times under different keys.
- **Only what is configured under `runners:` can be dispatched to.** The API
  lists exactly those, under those ids. A config file without `runners:` has
  no dispatchable runner — composing, exporting and ingesting is all it does.
- **Options are validated at startup.** Each entry's keys are checked against
  the plugin's constructor (or an explicit `OPTIONS` declaration): an unknown
  or missing option is a `ConfigError` naming the entry and the accepted
  keys. The plugin is checked against its role's contract (§6.6) the same
  way. Then every configured plugin is constructed once — construction must
  be free of side effects — and the taxonomy is read, so `serve` and `doctor`
  refuse a deployment that could not have worked. Unknown plugin ids fail the
  same way.
- **Values from the environment.** A string option of the form
  `${env:NAME}` or `${env:NAME:-default}` — at any depth of an option value,
  under `connectors:` too — is replaced from the environment when the plugin
  is built; a missing variable is a `ConfigError` naming it. Such a value is
  a **secret** unless it is written `${env-public:NAME}` (the same
  substitution, for an address, a region, a port), used as a path (an option
  the plugin names in `PATH_OPTIONS`), or converted to a number or a boolean
  (below); a `:-default` is written in the file and is no secret.
  Configured options never travel into a run spec, an API response or a log
  line; where a plugin quotes one anyway, secrets are blanked out of free
  text — messages, refusal reasons, errors, plan text and notes, health
  details, error documents, log lines — and out of the string values of
  runner-defined data (health and plan `data`), never out of identifiers:
  shard, group and runner names, item, run and dispatch ids, states, event
  kinds, parameter names, mapping keys and links are recorded as they are
  (tests/test_env_values.py). A secret shorter than four characters cannot
  be blanked without mangling ordinary text and is refused at startup,
  naming the option and the variable — write `${env-public:…}` if it is no
  secret. The links a runner records are stored as given, because a later
  `cancel` finds its executor through them — a runner must not put a secret
  into a link. An environment value is text; an option the plugin's
  constructor annotates as `int`, `float` or `bool` (optionally `| None`)
  receives it converted — `true`/`yes`/`on`/`1` and `false`/`no`/`off`/`0`,
  an empty value as `null` where the annotation allows it — and a value that
  does not convert is a `ConfigError` naming the option and the variable,
  never quoting the value. Plugin *selection* is never read from the environment, and the file
  itself is chosen with `--config` only.
- **Callables named in configuration.** Wherever a plugin option names a
  callable — a hook, a provider, a check — it takes `"pkg.mod:callable"` or
  `{call: "pkg.mod:callable", with: {…}}`; the `with:` mapping reaches the
  callable as the keyword argument `settings` (nothing is added without
  it), so one function serves several entries and nothing it needs travels
  through the environment. `ConfiguredCall` and `configured_calls` in the
  SDK parse both spellings for a connector's own options; a callable reads
  its connector package's settings with `ctx.connector_settings(name)`
  (tests/test_robot_pool_options.py, `TestConfiguredCallablesWithSettings`;
  tests/test_jenkins_followup.py, `TestConfiguredCallables`).
- **`connectors:`** is a free-form mapping the core never interprets — the
  place for a connector package's own settings. A plugin class that declares
  `CONNECTOR = "<name>"` is built with `connectors.<name>` as the keyword
  argument `connector_settings` (`${env:…}` resolved, its secrets redacted
  like any option's); embedding code reads it with
  `config.connector_settings("<name>")`. Any other unknown top-level key is an
  error that says so and names `connectors:`.
- **`parsers:`** configures result parsers by format id, with the same
  `module:` / `plugin:` selection. A run spec names a format id only (§5).
- **`ui:`** is served to clients (`GET /api/v1/ui-config`, `/meta`) with
  every key as written — except the three that carry selections, which are
  validated against the filter grammar at startup and served in one
  normalized shape. `quick_filters` are `{label, filter}` entries whose
  `filter` is filter text (§3.1) or a filter AST (a bare string entry is
  short for that filter labelled with itself — a single pattern with the
  pattern); a string that needs the
  single-pattern fallback works and is named in one warning when the
  configuration is loaded, and text that cannot be read refuses the start,
  naming the entry; `default_filter` is the
  expression a new selection starts from; `presets` are named, complete
  starting points — `name` and `filter`, optionally `label`, `description`,
  `title`, `labels`, and a `runner` with its per-run `parameters`. Each
  selection is served with `tag_filter` (the AST, in the grammar
  `selection.tag_filter` takes), `text` (its canonical filter text, which
  reads back to the same `tag_filter`) and `kind` (`pattern` or
  `expression`, as the filter reads); `GET /api/v1/presets` serves the three
  together. A preset that names an unconfigured runner, or parameters that
  runner would refuse, refuses the start — otherwise a client would offer a
  start the API then refuses (tests/test_deployment_options.py).
- **Zero-config.** With no config file at all runcomposer runs on built-in
  defaults *spelled as configuration*: the sqlite store in
  `./runcomposer.db`, the `demo-corpus` source, the bundled taxonomy, and the
  `demo` runner. A config file that leaves out `store:` or `sources:` gets
  those two defaults; runners are never implied.
- **`core.api.hosts`** lists the host names the server answers to — a
  name, an IPv4 address or a bracketed IPv6 address, optionally with
  `:port` (without one, any port); `*.shop.example` for every name below
  `shop.example` but not that name itself; `*` for every name. A request
  for any other name, or without a `Host`, is a `400` `unknown-host`
  before anything else sees it, whatever its method (§9). The list
  replaces the default rather than adding to it. Unset, the address the
  server is bound to decides: on a loopback address (`serve` on its
  default `127.0.0.1`) only `localhost`, `127.0.0.1`, `[::1]` and the bound
  address itself are answered, since nothing but this machine can reach it
  by any other name — which closes DNS rebinding for the default local
  setup; on any other address (`0.0.0.0` in the image) every name is
  answered until the list is set. An application built without its bind
  address (embedded in another server, the kit's harness) answers to every
  name unless the list is set. An empty list or an entry that is not a host
  refuses the start, naming `core.api.hosts[i]` (tests/test_api_hosts.py;
  SECURITY.md §6).

**One path base.** A relative path anywhere in the config file resolves against the
**config file's own directory** — the core's keys (`taxonomy_file`,
`artifact_dir`, `private_dir`, `ingestion.inbox`, `ingestion.quarantine_dir`)
and the plugin sections alike. A config directory is therefore portable, and
one `--config` behaves identically from every working directory. Absolute
paths are left as written. A path may come from the environment, and then
`${env:…}` is substituted **before** the path is anchored — so `root:
${env:SHOP_CHECKOUT}/tests` with an absolute value stays where the variable
points, and a relative value is anchored like any other; this holds for the
core's keys and for every option a plugin names in `PATH_OPTIONS`
(tests/test_deployment_options.py). The config file's own path is made
absolute when it is loaded — also when `--config` named it by a relative
path, made absolute rather than resolved, so a file reached through a
symbolic link keeps the directory it was named in — and without a file the
working directory at load time is the base; so every anchored path is
absolute, and a later change of directory, or a worker process started
elsewhere, finds the same files (tests/test_config_paths_absolute.py).

The core must not decide what a plugin's options *mean*, and "which of these
strings are paths" is exactly that kind of decision — a listener spec, a
shell command and a URL are not paths. So the plugin says so: a class
attribute `PATH_OPTIONS = ("suite_root", …)` names the options (single values
or lists) that are anchored to the config file — the default
`PluginBase.resolve_config_paths` resolves exactly those — and a plugin whose
rule has exceptions overrides `resolve_config_paths(options, resolve)` and
resolves them itself — the sqlite store does, because `:memory:` names no
file at all. The scope is the *config file*: a path inside a **run spec** is
the document's own value and is never rewritten.

**The taxonomy file is validated on the same terms** — it is the other thing a
deployment writes by hand. Top-level `taxonomy` must be a list; each node
needs a non-empty `label`, a `filter` (when present) that is a string whose
filter text names a single pattern the §3.1 grammar accepts — a tag spelled
like an expression is quoted (`'"Auth OR Cart"'`), and text that reads as an
expression is refused, saying how to quote it; a string that needs the
single-pattern fallback is listed under `warnings` of the resolved
taxonomy — and a `children` (when present) that is a
list; sibling labels must be distinct; a node with neither `filter` nor
`children` is refused, because that is exactly what a misspelled key
produces. Unknown *extra* node keys are ignored, matching §3's stance on
unknown fields inside a known section. Checked at startup, and on every
request — `GET /api/v1/taxonomy` re-reads the file — with the same validator
and the same message: a `500` naming the offending node, never a `200` with
an empty tree.

---

## 9. API & CLI surface

```
GET    /api/v1/health                     # is this process serving
GET    /api/v1/meta                       # boot info in one call: version, locale, text_language,
                                          #   runners + parameters, catalog size, taxonomy, what
                                          #   is configured (loaded) and what is installed,
                                          #   api.docs (whether /docs and /redoc are served)
GET    /api/v1/ui-config                  # the ui: section, selections normalized (§8); text_language
GET    /api/v1/presets                    # {presets, default_filter, quick_filters}
GET    /api/v1/runners[?refresh=true]     # configured runners: id, capabilities, parameters
                                          #   (with pattern, key_pattern), health + data (cached 30 s)
GET    /api/v1/catalog/tags               # every tag with its item count
POST   /api/v1/catalog/reload             # re-read the corpus now
GET    /api/v1/taxonomy[?resolve=true]    # the validated file; or resolved against the catalog

POST   /api/v1/selection/compile          # preview: matched items, total, warnings, derived_from
POST   /api/v1/selection/spec-preview     # the would-be runspec — nothing is stored
POST   /api/v1/plan                       # what a runner would do with a selection — nothing is
                                          #   stored, executed or triggered (§6.2)

POST   /api/v1/runs                       # compose (+ dispatch: {runner, parameters} | {mode: export})
GET    /api/v1/runs                       # list: state, label, q, since, until, limit, offset → total
GET    /api/v1/runs/{id}                  # detail (below)
DELETE /api/v1/runs/{id}
GET    /api/v1/runs/{id}/items            # the materialized selection
GET    /api/v1/runs/{id}/spec             # the stored document (never a token)
GET    /api/v1/runs/{id}/export?format=ctrf  # the results as a CTRF download (what `export` writes)
GET    /api/v1/runs/{id}/events?after=    # the event stream after a cursor; wait= long-polls
GET    /api/v1/runs/{id}/verdicts         # dispatch, shard, status filters (status=RUNNING:
                                          #   what is executing now); limit/offset
POST   /api/v1/runs/{id}/plan             # what a dispatch of this run would do
POST   /api/v1/runs/{id}/dispatch         # dispatch or export a stored run
POST   /api/v1/runs/{id}/cancel           # ?force=true closes it whoever owns it (§4)
POST   /api/v1/runs/{id}/rerun            # scope: all | failed; title — answers rerun_of
POST   /api/v1/runs/{id}/finalize
GET    /api/v1/runs/{id}/dispatches/{dispatch_id}/spec    # the exact bytes handed out
POST   /api/v1/runs/{id}/dispatches/{dispatch_id}/token   # rotate the ingest token
POST   /api/v1/runs/{id}/results          # ingestion push (multipart; token)
POST   /api/v1/runs/{id}/live             # live status from an external executor (token, §5)
POST   /api/v1/runs/{id}/log              # log lines from an external executor (token, §5)

GET    /api/v1/quarantine
POST   /api/v1/quarantine/{id}/attach     # bind to an existing run (logged)
POST   /api/v1/quarantine/{id}/promote    # its own `origin: ingested` run
DELETE /api/v1/quarantine/{id}

GET    /artifacts/{run_id}/{dispatch_id}/…   # local artifact files (§6.4)

GET    /openapi.json                      # the API's description — always served
GET    /docs, /redoc                      # its viewers — only with core.api.docs: true
```

The two viewers load their script and style from a public CDN into the
browser that opens them — third-party script on this origin, and a blank
page in a closed network — so they are off unless `core.api.docs: true`
says otherwise; `/openapi.json`, from this origin and nothing but the
schema, is always there, and `/api/v1/meta` says which (`api.docs`)
(tests/test_api_browser_safety.py, `TestApiDocumentation`).

**Nothing waits for an execution.** `POST /runs` with a dispatch, `POST
/runs/{id}/dispatch` and `/rerun` answer as soon as the dispatch is recorded
(§4); the run comes back `DISPATCHED` or `RUNNING`. A dispatch refused within
the hand-off wait is the error of that request (`409`, `dispatch-refused`;
`400`, `invalid-parameter`, when the runner raised `InvalidParameter`, §6.2)
— and when it was composed by the same request, the run is removed again
unless the request said `keep_run`; the error body says which (`run_kept`).
Everything checkable up front — the runner id, its parameters — is checked
before a run exists. The answer to an accepted dispatch says `handoff: done`
(declared, or already over) or `handoff: pending` (still preparing). The rule
is one rule: **a run an answer has named is never removed implicitly** — so a
refusal that arrives after a `pending` answer leaves the run `COMPOSED` with a
`dispatch.refused` event, whatever `keep_run` said: by then the run is the
record of the refusal.

**A run row is complete for a list** — verdict counts by status, planned
count, duration, the latest dispatch's mode, labels, origin — and `total` is
the size of the whole filtered set. Like every other headline field, the
run's `duration_s` describes its **latest dispatch** (a run dispatched again
a week later did not take a week); each entry of `dispatches` carries its
own `duration_s` and `shard_groups`. A row carries no deliveries — those
are the detail's. **A run detail** adds deliveries,
verdicts (shard, attempt, duration, `live`; a `MISSING` row also
`last_live`, §4), a per-shard roll-up (`shards`: counts and computed
completion per shard — "green on one partition, red on the other" is read,
not aggregated), the shard groups (`shard_groups`: shard → label) and their
own roll-up (`groups`: `{group, shards, declared_shards, delivered_shards,
planned, count, summary, live, completion}`, the completion `null` until
every shard of the group delivered), what is streaming now (`live_summary`:
`{shard: {status: count}}`), `rerun_of` for a run a failed-rerun composed,
artifacts with hrefs, their shard and group, kind (`local`, `url`,
`directory`, `external-path`) and a `url_or_path` that never names a server
path (§6.4), recent events, and the `selection` the run
was composed from (filter AST, picked ids, history provenance), so a client
can load it back into a builder. The verdict list is flat and labelled
rather than nested by shard: a per-item view wants it flat.

**Plans are answers, not errors.** `POST /plan` (`{selection, runner,
parameters?, title?, labels?}`) and `POST /runs/{id}/plan` (`{runner,
parameters?}`; without `parameters`, the ones the run's document carries)
answer `{runner, run_id, item_count, parameters, refused, refusal, plan,
warnings}`, where `plan` is `{text, shards: [{shard, group, count, item_ids,
estimate_ms}], groups: [{group, shards, count, estimate_ms}], notes, data}`
— or `null` with `refused: true` and the `refusal` a dispatch would meet,
answered `200`. Parameters are validated as for a dispatch (a bad one is a
`400` naming it, and so is one the runner's `plan` refuses with
`InvalidParameter`), and a runner without the `plan` capability is a `400`.
`selection` is what `POST /runs` takes: `tag_filter` — filter text or an
AST (§3.1) — `item_ids`, and `history` (a query string or `{query,
labels}`, §7), each optional.
Nothing is stored, executed or triggered, and a stored run is left alone in
whatever state it is.

**Exports are byte-exact.** An export answers with `spec_url`, `spec_sha256`
and — once — `ingest_token`. The URL serves exactly the bytes the hash was
computed over, any number of times, never containing the token; a client that
needs the document downloads it rather than re-serializing JSON it was shown.

**Errors are uniform.** Every error body is `{"detail": <string>, "code":
<stable id>}` — a domain error, a failed request validation (`400`,
`invalid-request`, plus a `fields` list), an unknown route. Status codes
follow from the *type* of the error, mapped in one place: not found `404`,
invalid request `400` (`invalid-request`, `unknown-host`, …), state conflict `409` (`run-active`,
`dispatch-refused`, `spec-conflict`, …), token `401`/`403`, unreadable bundle
`422`, too large `413`; a deployment that broke under a running server (the
taxonomy file) is a `500` that says what is wrong. Request bodies are
validated before anything acts on them. Some errors carry fields of their
own: `invalid-parameter` the `parameter`; `filter-syntax` — filter text
that cannot be read — the `problem` (`empty`, `unexpected-end`,
`unexpected-operator`, `missing-operator`, `unbalanced-open`,
`unbalanced-close`, `unterminated-quote`, `needs-quotes`), its `position`
(0-based, in characters) and, where reading stopped at a word, the `token`.

**Cross-site writes are refused.** A state-changing request — any method
but `GET`, `HEAD` and `OPTIONS`, on every path — that a browser sends for
another site's page is refused `403`, `cross-site-request`, before any route
sees it: an `Origin` that is neither this server's own (the request's
`Host`, the scheme's default port where one leaves it out) nor listed in
`core.api.cors`, or `Sec-Fetch-Site: cross-site` without an `Origin`.
Clients that send neither header — curl, the CLI, an executor pushing with
its token — are unaffected (tests/test_api_browser_safety.py; SECURITY.md
§6).

**Only the server's own names are answered.** Comparing `Origin` with
`Host` cannot see DNS rebinding — a page on a name its author controls,
resolved to this server's address, is one origin with that name to the
browser. So a request for a host name the server does not answer to
(`core.api.hosts`, §8) is refused `400`, `unknown-host`, before any route —
or the cross-site check — sees it, whatever its method: `GET` included,
the health route, the UI and the artifacts too (tests/test_api_hosts.py).

`?resolve=true` on the taxonomy is a parameter rather than a second endpoint:
the unresolved body is the published shape and a client that asks for the
file must keep getting the file. Static files are served with
`Cache-Control: no-cache` for `/` and the locale files, and as immutable for
the hashed `/assets/`.

Runner-specific admin actions are declined: a runner
offers `dispatch`, `plan`, `cancel`, `health` and declared per-run parameters, and an
endpoint whose meaning only one runner knows would be an unchecked surface of
the API.

**CLI** — the flag-level reference is [docs/cli.md](docs/cli.md):

| | |
|---|---|
| look | `catalog` · `taxonomy-check` · `compile` · `validate` · `doctor` (`-v`: every runner's health data) |
| compose | `spec` (`--dry-run`, `--export`, `--runner` / `--param`) · `export-spec` · `plan` (a selection or `--run`; `--items`) |
| run | `run` (compose → dispatch → wait → summary) · `dispatch` (a spec file or `--run`) · `rerun` (`--scope all\|failed`, `--title`) · `cancel` (`--force`) · `watch` |
| read | `runs` (`--failed-in`) · `show` · `export --format ctrf` |
| results | `ingest` · `quarantine list\|attach\|promote\|discard` · `finalize` |
| history | `history import` (seed durations from a file) · `history durations` (`--label`, `--last`, `--status`) |
| operate | `serve` · `gc` · `delete` · `demo` |

`run`, `dispatch` and `rerun` execute the runner in the CLI's own process and
return when it did; `--no-wait` only concerns a run left `AWAITING_RESULTS`.
A spec *file* handed to `dispatch` whose run is already stored must be that
run's document: an edited selection, source or results contract is refused.
Its `runner` section is the exception — the one open section states the
per-run parameters for this dispatch, and is validated like `--param`.
`--json` prints one JSON document — the same shapes the API returns, except
that an artifact's `url_or_path` is the recorded path, since the CLI runs on
the machine the files are on, and that `runs` and `show` add `owner_gone`
(§4). A `--param` value is read as YAML, except that the values of a
`mapping` are text as typed (`--param 'variables={ORDER_LIMIT: 1300}'`
passes the text `1300`). Exit
codes are the same everywhere: `0` done, or the run is `PASS`; `1` the answer
is no (the run is not `PASS`, a spec is invalid, a bundle refused, a run
unknown, a plan that would be refused); `2` the command line or the
configuration is wrong; `3` the run ended without a result (refused, failed,
cancelled, stale, timed out). Every
command that reads a deployment takes `--config` — the only way to select a
config file. `python -m runcomposer` is the same command line, exit codes
included (tests/test_python_m.py). Plus the
vendorable **`runcomposer-exec`** consumer (§6.2c), shipped as its own tiny
artifact.

Logging uses the `logging` module under `runcomposer.*`; `serve` installs a
default handler (`--log-level`), the other commands print warnings and, with
`-v`, what they do (`doctor -v` also prints every runner's health data).

---

## 10. UI

A React app is the product's face (the prototype's interaction model is kept:
taxonomy tree + filter-builder + auto-compiled preview table + fine
selection). The tree reaches the user through a **searchable, keyboard-driven
picker dialog** rather than a permanent panel, and applies a whole selection as
ONE filter group — the resolved tree (§2) has a node for every catalog tag
and grows with the catalog, which an always-visible list with no search
cannot carry. The dialog is the non-technical layer over the same
`selection.tag_filter`; the filter-builder underneath is the raw one, and
neither is a degraded version of the other:

1. **i18n** — every literal in locale files; `en` + `de` shipped; locale date
   sugar lives here. The page starts in the language the viewer chose
   before, else in the first language their browser asks for that there are
   texts in, else in `core.locale_default`; `<html lang>` always says which.
   The UI words whatever reaches it as a code, an enum or a field — states,
   results, error codes, inbox reasons, a value that does not have its
   declared `pattern` — and the reference runners' own titles, descriptions,
   parameter labels and healthy-state summaries, by plugin id, with the
   server's text as the fallback. **What it cannot word it does not
   pretend to:** a sentence a runner, a tool or the server wrote at run time
   (an error's detail, a health reason, a plan's notes and text, a refusal)
   is shown as written, carries the language the deployment says such text
   is in — `core.text_language` (§8), served as `text_language` by
   `/ui-config` and `/meta`; `en` by default, since the server and the
   reference plugins write English — and, while the page is in another
   language (the primary subtags differ: `de-CH` text on a German page is
   not marked), a small mark in front, the same everywhere. Translating it would mean
   guessing at text the UI has never seen; leaving it unmarked would let it
   pass for a gap in the translation. Names and data are not marked: tags,
   item names, test messages, and what a deployment declares itself — a
   parameter's label and help, a preset, a quick filter — are its own
   wording, shown as written.
2. **Config-driven environment** — whatever the deployment puts under `ui:` is
   served from `/api/v1/ui-config`, never hardcoded (§8). The UI reads
   `quick_filters` — always at hand next to the presets, not only on an
   empty builder (one pattern is one more rule; a whole expression becomes
   the filter, or one more group of the filter that is there) — starts a new selection
   from `default_filter` — only when the URL carries no selection and the
   session has no draft to come back to — and offers `presets` (read from
   `GET /api/v1/presets`), each of which fills selection, runner, the
   runner's options, title and labels. Each is taken from its
   `tag_filter`; its `text` is the canonical filter text, shown as it is. Next to them, under a heading of
   their own, are the **selections a person saved** ("Save selection…":
   filter, unticked items, runner, options, title, labels under a name; use,
   rename, delete). Those live in the browser's `localStorage` — per browser
   and origin, never on the server, not shared and not exported; a browser
   that stores nothing says so and loses nothing else. What everybody should
   find is a preset in `ui.presets`. `examples` is `[planned]`, so a key
   the UI does not yet read is served and silently ignored. Partitions are
   not a UI setting: they are the `partitions` parameter a runner declares,
   chosen in the runner form.
3. **Runner-aware run bar** — what would run, with which runner, and the
   actions Plan / Export / Run, in a bar that stays in reach at any scroll
   position and width. The actions are for the filter in effect and nothing
   else: while the expression is being typed as text and differs from it,
   they are off, the filter in effect stays in view, and the bar says why —
   as it says that the server cannot be reached when that is the reason.
   While the server is out of reach the preview asks again by itself, at
   growing intervals, as long as the page is looked at; and a count is
   shown only next to the filter it is the count of — a preview that could
   not be updated shows none, in its head or on the Run button.
   Runs list shows dispatch mode + lifecycle state incl.
   `AWAITING_RESULTS`; an inbox view for unmatched (quarantined) deliveries.

**What the UI covers of the 0.2 API.** Four hash routes — `#/compose`,
`#/runs`, `#/runs/<id>`, `#/inbox` — so a deep link survives a reload
without the server knowing a single UI route, and the UI works unchanged
behind a reverse proxy that mounts it under a path prefix. Compose: the tag
picker, the filter editor, the auto-compiled preview, a runner form rendered
from the runner's declared parameters — every kind, `text` and `mapping`
included, a `pattern` and a mapping's `key_pattern` shown as the form a
value or a name must have and checked before sending, and the server's
refusal put on the field (in the UI's own words where the parameter
declares such a form: only the server's sentence knows any other reason,
and is then shown as the server's) — with the runner's health status and, where it reports them, its
checks one by one (`health.data.checks`); a plan preview (`POST /plan`,
§6.2) for a runner with the `plan` capability, showing shards, groups,
estimates and notes or the refusal a dispatch would meet; and export with
the document downloaded byte-exact from `spec_url` (§9). The preview of the
selection and the plan are each the answer to what is on screen or are not
shown: a request is keyed by what it asks, a newer question aborts it, and a
late answer is dropped. Runs: paging, search, state and label filters. A
run: the lifecycle states and statuses 0.2 added (`FAILED`, `CANCELLED`,
`STALE`; `MISSING`, `INCOMPLETE`), the event stream followed live, the
`RUNNING` live status per item (listed and counted as running, never as a
result), the roll-up per shard and — where the runner declared groups — per
group, with artifact links (a recorded directory is listed, not linked),
`last_live` on a `MISSING` row after a cancel — what needs a look first:
groups and shards with a failure, then those still running, the rest folded
into one line once there are many (also the cards of failed shards beyond
the first few), one step from the top to the failed results, and each
result row linking to the log and report of its own shard — "N of M
reported" meaning the same for the run, a group and a shard: items with a
reported result, never a `MISSING` one (the roll-ups' `count`) — an item
shown by the name the catalog has for it now, with its id, and found by
either; a run the server stopped under worded as that, not as an error of
the runner (the dispatch is `STALE`; what the server noted is a detail
line) — cancel, rerun (all, or what
did not pass, optionally with a title; the new run links back through
`rerun_of`), a plan for the stored run (`POST /runs/{id}/plan`) and delete.
Inbox: attach, promote, discard. The footer links the API documentation
viewer when the server serves it (`meta.api.docs`), else the OpenAPI
description (`/openapi.json`), which it always serves.

**How a filter travels.** The UI sends a selection's `tag_filter` as the
AST, except a filter that is one pattern: a lone string is filter text to
the server (§9), so a single pattern is sent as its quoted text
(`quotePattern`) — a tag spelled `Auth OR Cart`, a tag named `AND` or `*`,
`"regex:^Cart(V2)?$"` — which the server reads back to that one pattern; a
run spec stores the pattern, never the quotes. Server text it receives
(`text` of a configured filter) is canonical text, and what it acts on is
the entry's `tag_filter`. The field that adds a rule reads what is typed
with the same grammar: one pattern is one more rule, an expression the
rules or the group it says, text that is no expression is refused in place
with its position — so `Payments AND Smoke` means the same there, in the
text editor and on the command line; a tag with a space or an operator
word in it is typed in quotes (or chosen from the catalog's list). A rule
whose pattern matches no tag of the catalog is flagged on its chip, with
the nearest tags offered for a plain tag: the editor's early word on a
typing mistake, by the server's matching rules (§3.1), claimed only where
the browser can read the expression. The text editor checks text locally before
applying it; a `filter-syntax` refusal (`problem`, `position`, `token`) is
worded by the UI where it is shown. Positions are counted in code points,
as the server and the shared cases count them; the UI converts to UTF-16
offsets in one place (`codePointPosition` / `utf16Offset` in
`ui/src/lib/filter.ts`) to put the caret there. An artifact is linked by
its served `href`, never by `url_or_path`; a recorded directory is listed
by name, and a file outside the run's artifact directory by name and the
file name the server gives — never a path of the server's machine.

Not in the UI: uploading results (§5), and the label-scoped history picker
of §7 — a rerun is an action of one run.

**Controls say what they do, in a word.** Every button of the UI carries a
visible word; a sign alone is a guess for whoever has not learnt it. One
exception, decided and kept to one: a rule's remove control is `×`. A rule
is a chip, a chip is closed with `×` wherever chips exist, and the word
cost a third of a chip's width — two rules to a row of the filter column
instead of three. It has an accessible name and a tooltip that say which
rule it removes; NOT keeps its word, because a box alone says nothing.

**Stack — decided:** TypeScript and React, and nothing else at run time —
React and React DOM are the only dependencies. What earlier versions took
from widget libraries the UI does itself, because each piece is small next to
what depends on getting it exactly right: an expression editor over the run
spec's own filter grammar (a rule is a chip, a group a box with its
operator, NOT a switch on either, and the same expression can be typed as
text — the grammar of §3.1 rather than a widget's model translated through
an adapter — one grammar, read alike by the server and the UI and held to
the same cases, `tests/fixtures/filter-text.json`, on both sides; `*` for
"every item" is part of it, and on the wire it is the negation of a
pattern nothing matches, `{"not": "regex:^$"}`, which is what is sent and
what the UI never shows); dialogs on the native `<dialog>` element (focus trap,
`Escape`, return of focus); and its own windowing for long lists, so the
resolved tag tree — which grows with the catalog — the selection preview and
the run's results keep only the rows in view in the DOM. Vite builds it
into the wheel; its development server proxies `/api` and `/artifacts` to
a local `runcomposer serve` with `Host` passed through, so the server's
refusal of cross-site writes (§9) takes the page as same-origin. Unit tests run with vitest; browser end-to-end tests with
Playwright (`npm run e2e`, which builds first); `npm run lint` is eslint plus
the TypeScript check. Per-item *live* state requires a runner that streams
it (§6.2a) or an executor that pushes it (§5); without it the grid shows
planned → final, honestly labeled.

---

## 11. Repo strategy & code salvage

**Fresh public repository, no imported history.** The predecessor prototype's
git history contains private material and stays private; runcomposer's public
history starts clean. Code is salvaged from the prototype **file by file,
after cleaning** — the proven pieces worth carrying over:

- the tag-filter AST evaluator and its models (proven grammar, §3.1);
- the taxonomy tree/rule engine (with locale literals moved to config);
- the selection→materialization compile flow;
- the UI interaction model (filter builder + taxonomy popup + preview);
- the test-suite patterns — especially two guards: a **self-containment
  guard** (core imports no plugin, contains no native-name normalization) and
  a **selection matrix test** (a large synthetic corpus proving AST→item-set
  semantics exactly), re-pointed at the core `Selection.compile(snapshot)`
  API.

Everything execution-, CI-, and deployment-specific lives inside the
appropriate plugin (`robot-pool`, `jenkins`, `command`) rather than in the core.
Private adopters keep their execution details in their own private
plugin and config packages that depend on this repo — nothing of theirs
appears here, in code, history, examples, or docs.

---

## 12. Demo world

`runcomposer demo` boots a neutral corpus — a fictional web-shop suite (~60
items: `Payments`, `Checkout`, `Catalog`, `Auth`; sprint/ticket/quarantine
tag patterns) via the `demo-corpus` source, a matching taxonomy, a `demo` runner
**simulating** executions (no test is run, and it says so), and **history at
both levels** (completed runs *and*
per-item verdicts/durations) so failed-rerun selection and duration-balanced
planning are demonstrable immediately: the command composes, runs, then
re-composes from what those runs returned, and prints the whole cycle.

That history is **persisted**, because the last thing the demo shows —
"rerun what failed" — is exactly the thing a fresh store cannot answer, and a
demo that narrates history it did not write leaves the reader's very next
command contradicting it. So the demo runs through the real machinery
(`Service.compose_run` → `Service.dispatch` → the store) rather than an
in-process imitation of it, and the summaries it prints are read back out of
the store.

**Where it writes is part of the promise.** `demo` seeds one directory,
`./runcomposer-demo` (or `--workspace DIR`), holding a generated `config.yaml`
— which configures the `demo` runner explicitly, since only configured
runners are dispatchable (§8) — and the sqlite file that config points at. Three properties, each chosen
against an alternative:

- *Not* a bare `runcomposer.db` in the working directory. That is the
  zero-config default store (§8), so a demo writing there would leave fake
  runs in the store an adopter's first real run then shares — `--failed-in
  latest` answering with `Shop.Payments.Cards.T003`.
- *Not* a temp directory that a reader cannot find again, and *not* a
  configured store of yours: the demo has no `--config` and must never touch
  a real deployment's data.
- Refused rather than forced: a directory holding a `config.yaml` the demo
  did not write, or any other non-empty directory, is somebody's deployment,
  and re-seeding it would delete their store. The demo says so and exits `2`.
  A working directory that is not writable at all (a container) falls back to
  a temp directory and prints which.

Every command the demo prints as a next step carries that workspace's
`--config` and works when pasted, from any directory. `rm -rf` on the
workspace is the complete uninstall; re-running re-seeds it from scratch, so
the output stays deterministic. The demo corpus doubles as the E2E fixture. A
pytest-flavored manifest example ships alongside (§6.1).

---

## 13. Explicitly out of scope for v1

- Full authN/authZ (the per-run ingest token of §5 **is** in scope — it's the
  seam that lets real auth arrive later without redesign; everything else:
  reverse proxy).
- Scheduling/cron, cross-instance queues, HA. One instance — one host — owns
  a store and the dispatches it executes; restart recovery (§4) relies on
  that when it takes an owner from another host to be gone.
- Result analytics/dashboards beyond run detail.
- Taxonomy editing UI.
- Hot-reload of the config file. (The corpus *is* reloaded when it changes —
  §6.1 — and the taxonomy file is re-read per request.)

---

## 14. Phasing

- **P0 — skeleton:** scaffold `core/` + `plugins/`; runspec 1.0 schema +
  `runcomposer validate`; neutral demo corpus; CI. **Acceptance criterion:
  one-command quickstart** — `pipx run runcomposer demo` *and*
  `docker run ... runcomposer demo`, UI pre-bundled (no Node toolchain for
  evaluators). The image runs runcomposer under an init (`tini`) as process
  1, which forwards signals and reaps the processes a stopped worker leaves
  behind; `docker run --init` is not needed.
- **P1 — compose & export:** SpecBuilder emits runspec; compile/preview API;
  sqlite store; UI port (i18n + ui-config + stack refresh); **export-mode
  end-to-end**: compose → `runcomposer spec` → `runcomposer-exec` → marker bundle →
  `runcomposer ingest` → COMPLETE.
- **P2 — execution:** `robot-pool` runner (incl. listener-based live verdicts,
  duration history + documented cold start); ingestion API + file-drop inbox +
  quarantine; `robot-output-xml` parser; lifecycle in UI.
- **P3 — reach:** `ci-trigger` (since 0.2: `jenkins`) + the thin CI-side consumer stage (named
  deliverable, §6.2b) against a real CI instance; `junit-xml` parser + pytest
  manifest example (the agnosticism proof); history-based selection provider;
  `runcomposer export --format ctrf`.
- **P4 — first private adopter:** a closed-source plugin/config package
  consuming the public core — the reference pattern for any company-internal
  adoption. Lives outside this repo by definition.

Each phase ends runnable + demoable.

**0.2** reworked the backbone the phases above produced as a proof of concept
— the runner contract, the lifecycle, the store, configuration, the API and
the CLI — into the contracts this document describes. The phase list is kept
as history; `CHANGELOG.md` says what changed.

---

## 15. Decision log

| # | Question | Decision (2026-07-06) |
|---|---|---|
| 1 | Name | **`runcomposer`** — verified free on PyPI + npm 2026-07-06, still unclaimed at 0.1.0. Placeholder-package reservation was **dropped** (2026-08-28): a squat advertises the gap it fills, and every documented path installs from a clone anyway. Publishing for real is the owner's call, gated behind the `PYPI_PUBLISH` repo variable. |
| 2 | Warm history at adopters | **Start fresh everywhere**; no migration machinery in core; private adopters may seed their own stores. |
| 3 | UI stack (superseded by #23) | **Refresh to current React/Vite at P1; keep SVAR** behind the adapter; localization spike noted. |
| 4 | License | **MIT.** |
| 5 | `runcomposer-exec` distribution | **Both by construction**: stdlib-only single file, also pipx-installable; JSON spec input so zero deps. |
| 6 | Who mints the dispatch id (2026-08-28; the mechanism is superseded by #11/#12) | **runcomposer does**, offered to the runner as a `DispatchReservation` via the optional `bind_dispatch` hook (§6.2); the runner decides *when* the hand-off is recorded, and the returned handle refines the declaration. Rejected: passing the id into `dispatch()` (breaks the published signature), and letting bound runners write their own dispatch rows (duplicates the ledger, unavailable to unbound runners, and lets a runner's own id drift from its configured `mode`). A refused dispatch — refusal always precedes the recording — leaves **no** row. |
| 7 | Shape of the shard in run detail (2026-08-28) | **Label the flat verdict list, add a sibling roll-up** (§9). Rejected: nesting verdicts by shard — it breaks every existing reader for a view a per-item table does not want; and leaving the aggregation to clients — the fan-out question is the reason shards exist, so answering it is the payload's job. |
| 8 | Serving local artifacts (2026-08-28) | **Containment + sandbox** (§6.4): resolve the path fully and refuse anything not strictly inside `core.artifact_dir` (one rule covering `..`, absolute paths and symlinks), refuse as `404` so probing is uninformative, and serve with `sandbox`/`default-src 'none'`/`nosniff` because the bytes are attacker-influenced and share an origin with the UI. A `text/html` artifact may run its own inline script inside that sandbox (`allow-scripts` without `allow-same-origin`, nothing loadable or reachable) — a framework's log is an application and was a blank page without it. Rejected: a bare static mount (no containment story, scripts live), and forcing `Content-Disposition: attachment` on everything (kills the one-click log the feature exists for). |
| 10 | Where the taxonomy resolves (2026-09-03) | **Server-side, opt-in per request** (`?resolve=true`, §2/§9). Server-side because the tag universe is the catalog and the matcher is `core.filter`: resolving in the UI would mean shipping the whole catalog to the browser and reimplementing the §3.1 grammar — its case rules included — in JavaScript, where it would drift from the one the selection actually compiles with. Opt-in because the unresolved body is published API. Rejected: resolving unconditionally (changes every response and charges every caller for it), a second endpoint (duplicates the §8 error contract for the same document in the same grammar), and extending the *file* format so a leaf could carry several tags (the limitation is deliberate — the tree hands the builder one pattern, docs/taxonomy.md — and it would put the corpus into the file, which is exactly what drifts). |
| 9 | History scope syntax (2026-08-28) | **A query-string suffix on the selector**, `<verdict>@<selector>?key=value&…` (§7), because `?`/`&` cannot occur in an ISO-8601 time or a run id, it survives a shell, an HTTP body and a spec field unchanged, and it composes with every existing selector. A scope on `run:<id>` is **refused, not ignored** — it can only mislead. Rejected: a second CLI-only flag (invisible in the API and in `derived_from`), and bracket/semicolon forms (quoting hazards, no established reading). |
| 11 | Who owns the lifecycle (0.2) | **The core, per dispatch.** One transition function; a run's headline is derived from its latest dispatch (§4). Rejected: a run state any caller may set (0.1) — a refused re-dispatch erased a finished run, and a late bundle for an old dispatch rewrote the headline. Supersedes #6: the dispatch id is still runcomposer's, but the hand-off is now `ctx.declare`, not a reservation object and a returned handle. |
| 12 | Runner contract (0.2) | **`dispatch(ctx)` with a context supplied by runcomposer** (§6.2). Rejected: optional hooks discovered by attribute (`bind`, `bind_dispatch`, `deliveries`, `last_plan`) — undocumented by construction — and handing runners the store, which made live status depend on one store implementation's private attribute. |
| 13 | Synchronous or asynchronous dispatch (0.2) | **Asynchronous, with a bounded wait for the hand-off** (§4, §9). The request never waits for an execution; it waits up to `handoff_wait_s` so an immediate refusal is the answer to the request. Rejected: holding the request for the run (0.1; unusable beyond seconds), and returning before anything is known (every refusal becomes an event somebody has to go and find, and a composed-and-refused run an orphan). |
| 14 | Planned but never reported (0.2) | **A derived `MISSING` status and an `INCOMPLETE` completion** (§2, §4). Rejected: counting an absent result as nothing (0.1: "1 of 46 delivered" was `PASS`), and storing synthetic `ERROR` verdicts — a later corrected delivery would have to un-write them, and "the executor reported an error" and "the executor reported nothing" are different facts. |
| 15 | Where the ingest token lives (0.2) | **Outside the document, bound to a dispatch, stored hashed, returned once** (§5). Rejected: a plaintext token in the stored spec (0.1; served back by the read API), and a token inside the exported bytes — a document that carries a credential can be neither re-downloaded nor passed around, and both are what the document is for. |
| 16 | What a spec's `runner` section may do (0.2) | **Carry declared per-run parameters; nothing else reaches an in-process runner** (§6.2). Rejected: overlaying the whole section on the configured options (0.1) — a document could then set readiness hooks and listeners, i.e. execute code on the machine that runs it. Same reasoning for result formats: names resolved through configuration, never import paths from a document. |
| 17 | Config: what names a plugin instance (0.2) | **The entry's key is the id; `plugin:` / `module:` select the class** (§8). The API lists and dispatches by that id, so one plugin can be configured several times. Rejected: the key as the plugin id with a single instance each (0.1), and reporting the plugin's own `describe().id`, which is not the name anyone can dispatch to. |
| 18 | What the manifest snapshot hashes (0.2) | **Ids and tags** — what a selection depends on, and what the Robot source already hashed. Rejected: the whole item (0.1), which made a renamed test or an edited `meta` drift. Consequence: a spec composed by 0.1 against a manifest reads as drifted under 0.2. |
| 19 | Options and per-run parameters on the context (0.2) | **Apart, never overlaid** (§6.2): `ctx.options` is the config entry, `ctx.parameters` what the run chose, validated. Rejected: options overlaid by parameters — a parameter could then shadow an option of the same name, and a runner could not tell what the deployment configured from what a person chose. |
| 20 | What the artifact route serves (0.2) | **Only what a runner recorded** — a file, or the tree under a recorded directory — and a never-served `private_dir` for the rest (§6.4). Rejected: serving whatever lies in a dispatch's work directory — every intermediate file, and any credential a hook wrote there, was one guessed URL away. |
| 21 | A dispatch whose results never come (0.2) | **Opt-in expiry to `FAILED`** (`core.dispatch.awaiting_timeout_s`, §4), the clock restarted by every delivery, late results still taken in. Rejected: finalizing on expiry (an executor that died is a broken execution, not a result), and an expiry on by default (how long a remote job may take is the deployment's knowledge, not the core's). |
| 22 | The reference runners' parts (0.2) | **Exported as SDK toolkits** (`runcomposer.sdk.robot`, `.jenkins`, §6.6), the reference runners built on them. Rejected: keeping the parts private to the runners — a connector with different needs would restate the worker stop protocol or the Jenkins queue semantics, which is where such details go wrong. |
| 23 | UI stack (0.2; supersedes #3) | **TypeScript and React only** (§10): the expression editor, dialogs and list windowing are the UI's own. Rejected: keeping the filter widget behind an adapter — the filter is the run spec's grammar, and translating a widget's model into it was a standing source of silent mistranslations. |
| 24 | A filter given as one string (0.2) | **Filter text, read by the core** (§3.1), with the single-pattern fallback and a warning for a string that is not text. The spec stores the AST; "every item" is `{"not": "regex:^$"}`, spelled `*` only in the text. Rejected: a string as always one pattern (`compile 'Auth OR Cart'` selected nothing), and a new node for "every item" (refused by every shipped `runspec/1.0` validator). |
| 25 | Which values from the environment are secret (0.2) | **`${env:…}` stays a secret; `${env-public:…}`, paths and converted numbers are none** (§8), and secrets are blanked out of free text only, never out of identifiers. Rejected: every environment value a secret (a region named like a shard, or a port, was blanked out of identifiers and addresses), and a secret too short to blank safely kept silently unblanked — it is refused instead. |
| 26 | The host names the server answers to (0.2) | **`core.api.hosts`, defaulted by the address actually bound** (§8, §9): unset, a loopback bind answers to the loopback names only — DNS rebinding is closed for the default local setup with nothing to configure — and any other bind to every name until the list is set. Keyed to the bind, not to `core.api.host`, because `serve --host` overrides the file (the image binds `0.0.0.0` over any config). The refusal is an ordinary error document (`400`, `unknown-host`); an entry without a port matches any port, one with a port that port only. Rejected: restricting every deployment to the loopback names by default (every server reached by a real name would refuse its first request), and leaving `Host` unchecked with a reverse proxy as the only answer (the default local setup was open to any page its user visits). |
| 27 | The run spec as a Jenkins build parameter (0.2) | **Visible, on purpose** (§6.2b): the document carries no credential — the token travels only as a password parameter — and it is what runcomposer itself serves for the run, so the build page says which document was run. Its cost is size (one environment variable of the build step); `max_parameter_bytes` refuses before that limit and `{spec_url}` keeps the document out of the parameters. Rejected: hiding the document as a password parameter, which protects nothing and takes from the build page the one thing worth showing. |
