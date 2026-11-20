# runcomposer — design

An open-source, tag-based test run composer & orchestrator.

Status: rev 4 — describes runcomposer 0.2 as built. Decisions final unless
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
  same surface (`runcomposer.sdk`) any private connector uses.
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
| **Shard** | A named part of one dispatch — a partition, a chunk, a CI job. A dispatch *declares* its shard set at hand-off; completion is computed against those names. |
| **Delivery** | One shard's results arriving, content-hashed. Idempotent per `(run, dispatch, shard)` (§5). |
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
  id: "01JZ9GQ2W8KJ3F6M4P5R7T9V"   # ULID, minted at compose time; correlation key for the RUN
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

### 3.2 What deliberately stays OUT of the core schema

Anything framework-, CI-, or deployment-specific: framework variables, code
generation flags, stage/partition vocabulary, CI job names and integer build
numbers, host-derived identity, artifact file naming, result-file paths. All
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
      → FAILED     the runner raised after the hand-off; delivered results are kept
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
  interrupted process does not un-run the tests that had finished.
  `CANCELLED` is final — results for a cancelled dispatch are refused.
- **Cancellation** (`POST /runs/{id}/cancel`, `runcomposer cancel`). For a
  dispatch executing in this process the context's `cancelled` flag is set and
  the runner's `cancel(ctx)` is called; the dispatch becomes `CANCELLED` when
  the runner comes back. For one executing in *another* process the request is
  written to the store and honoured there within half a second — which is how
  the CLI cancels a run a server is executing. (A runner that notices
  `ctx.cancelled` and returns on its own before that is `CANCELLED` without
  its `cancel` hook having been called.) For a dispatch that only waits for
  results, the runner is asked to stop its executor through a context rebuilt
  from the stored record, and the dispatch is `CANCELLED` at once. A dispatch
  still queued is cancelled without the runner ever starting.
- **Restart recovery.** While a runner executes, the dispatch names its owner
  (host, process id, a per-process token). At startup — `serve`, the
  dispatching CLI commands, and `doctor` — every `DISPATCHED`/`RUNNING`
  dispatch whose owner is gone is marked `STALE` with a `run.stale` event
  saying why. An owner is gone when its process id is dead; when it is this
  very process id under another token (an earlier life of it — process id 1
  of a restarted container); or when it ran on another host — one instance
  owns a store (§13), so that is a container that was replaced. What cannot
  be told apart is a process id that was reused by an unrelated program: the
  dispatch then looks owned. `cancel --force` (`?force=true`) is the way out
  — it closes a dispatch as `CANCELLED` whoever owns it. Expiring a dispatch that merely
  waits for results too long (`AWAITING_RESULTS` > N days) is `[planned]`;
  such a run waits until someone finalizes or cancels it, or retention prunes
  it.
- **Events.** Every state change emits an event — `run.composed`,
  `run.dispatched`, `run.running`, `run.awaiting_results`, `run.completed`,
  `run.failed`, `run.cancelled`, `run.stale` — as do `dispatch.refused`,
  `delivery.recorded`, `delivery.attached` (a quarantined bundle bound to a
  run by a person), `run.cancel_requested`, and a runner's `log` / `warning`
  lines. Events are appended to the store with an increasing `seq`, read back
  after a cursor (`GET /runs/{id}/events?after=`), and handed to observers
  (§6.5).
- **Live status** is an optional runner capability: per-item status streamed
  while a shard is still executing. It is kept apart from delivered verdicts,
  flagged `live` where it is shown, and replaced by the shard's delivery.
  Without it a run shows planned → final.
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
- *Auth precedes any write.* An unknown run is a plain `404` and nothing is
  kept; a missing token is `401`, a wrong one `403`, a token used against
  another dispatch `403`. Only an authenticated bundle that is then refused
  for what it contains is quarantined.
- *Bounded.* Upload size (`max_upload_mb`) and the quarantine
  (`quarantine_max`, enforced **on write** — a new entry beyond the bound
  evicts the oldest) are config. Identical bundles are quarantined once.
- *Explicit, logged attach.* Binding a quarantined bundle to a run is a
  person's decision (`quarantine attach`); it is recorded as a
  `delivery.attached` event on the run.
- *Defused XML.* A document carrying a DTD is refused by the parser itself —
  wherever the declaration sits, in whatever encoding — through one shared
  helper every XML-reading parser uses (`forbid_dtd` / `parse_xml`, §6.6).
- *A spec cannot make this process run code.* A result format is a **name**,
  resolved through the `parsers:` config section and the entry points — never
  an import path taken from a document — and a spec's `runner.<id>` section
  reaches a runner only through that runner's declared parameters (§6.2).

Full authN/authZ stays out of scope (§13); the write path is not open by
default.

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
is refused as `bad-bundle`. **The anti-leak litmus test is about identity,
not parser count:** the core never string-compares native names — every
native name goes through `TestSource.resolve(native_name) → id`, so all
name-normalization quirks live inside the source plugin that owns them. A
guard test asserts the core contains no native-name normalization. Names the
catalog does not know are reported as warnings, never invented as ids — and
the items they were meant for end up `MISSING`.

**Refusals over HTTP** carry the reason as the error `code` and a status that
says what kind of refusal it was: `422` the bundle cannot be read
(`bad-bundle`, `unsolicited`), `409` it contradicts the run (`sha-mismatch`,
`marker-conflict`, `dispatch-cancelled`), `404` `unknown-run` /
`unknown-dispatch`, `400` a declared format that is not the run's, `413` too
large.

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
package only `runcomposer.sdk` (§6.6): the same surface a connector outside
this repository has.

### 6.1 `TestSource`

```python
provider_id: str
def items() -> list[Item]
def snapshot() -> str                            # content hash
def resolve(native_name: str) -> str | None      # parser-name -> id (None = unknown)
# optional:
def fingerprint() -> str                         # cheap "did the corpus change" token
duplicate_ids: list[str]                         # ids more than one item carries
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
One that does not is reloaded on request (`POST /api/v1/catalog/reload`) and
before every dispatch, where the drift check needs the corpus as it is *now*.
Composing a run also refreshes first. Editing the corpus never requires a
restart, and a corpus that did not change is never parsed twice in one
process. (Each CLI invocation is its own process and parses once; a
cross-invocation cache is not built.)

### 6.2 `Runner`

```python
def describe() -> RunnerInfo
def dispatch(ctx: DispatchContext) -> None
# optional — defaults in the RunnerBase helper class:
def cancel(ctx: DispatchContext) -> None     # declared by the "cancel" capability
def health() -> Health                       # status ok | degraded | down | unknown
```

One instance is built per configured runner id and may be dispatching
several runs at once: everything that belongs to one dispatch lives on its
context, never on the runner. Construction must be free of side effects —
every configured plugin is built once at startup (§8).

**`RunnerInfo`**: `id` (the plugin's name — the id a deployment dispatches to
is the config key, so one plugin can be configured several times), `title`,
`description`, `capabilities` (flags; `live_status`, `partitions`, `cancel`,
`external_delivery`, `simulated` have meaning to clients), `result_format`,
and **`parameters`**: the per-run options the runner accepts from whoever
starts a run. A `Parameter` has `name`, `kind` (`string | integer | boolean |
choice | multi_choice`), `label`, `help`, `default`, `choices`, `required`.
The API exposes them (`GET /runners`, `/meta`), validates submitted values
against them — an unknown, missing or ill-typed value is a `400` naming the
parameter — and writes the accepted values into `runner.<id>` of the spec
document.

**A document cannot reconfigure the machine that executes it.** Of a spec's
`runner.<id>` section, an in-process runner sees exactly the keys it declared
as parameters, validated. Everything else in that section is ignored with a
warning event — it may be meant for an external executor — and cannot
override a configured option. Readiness hooks, listeners, paths and
credentials come from the config file only.

**`DispatchContext`** — everything a runner gets, and everything it may do:

| | |
|---|---|
| `run_id`, `dispatch_id`, `runner_id` | identity; the dispatch id is minted by runcomposer |
| `spec`, `item_ids` | the document (read-only) and `selection.materialized.item_ids` — the authoritative executed set |
| `options`, `parameters` | configured options overlaid by the validated per-run parameters; and the parameters on their own |
| `workdir` | a directory for this dispatch under the artifact root, already created; files written here are servable (§6.4) |
| `source` | the catalog as it is now — drift check, name resolution |
| `history.durations(labels=, last_n=)` | mean duration per item id from earlier dispatches |
| `refuse(reason)` | decline; legal before `declare`, leaves no dispatch |
| `declare(shards, *, plan="", links=None, spec_sha256=None)` | THE hand-off (§4): shard names, or names mapped to the item ids each shard will report; exactly once. `spec_sha256` records the hash of document bytes the runner serialized itself (`export_spec()` records it for the bytes it returns) |
| `live(shard, item_id, status, …)` / `live_channel()` | streamed per-item status; the channel is a **picklable** object with the same method for worker processes |
| `parse(bundle, *, format=None)` | native artifacts → correlated verdicts, with the configured parser — for runners that relabel (attempts) before delivering |
| `deliver(shard, *, verdicts=… \| bundle=…, format=None)` | one shard's results; idempotent by content hash; completion is recomputed after every delivery |
| `add_artifact(name, path_or_url, *, media_type=None, shard=None)` | |
| `log(message, level="info")` | a line in the run's event stream |
| `export_spec()` | the document as the exact JSON bytes to hand to an external executor; their hash is recorded for marker verification (§5) |
| `ingest_token()` | mint the token an external executor pushes results with (§5) |
| `links` | what was given to `declare` — how `cancel` finds its executor |
| `cancelled`, `raise_if_cancelled()` | cooperative cancellation |

It is a protocol, so a runner can be tested against a fake. The live channel
works with **any** RunStore: it carries no store, only a file in `workdir`
that worker processes append to (one file per process) and the dispatching
process tails.

**a) `robot-pool`** — Robot Framework executed on this machine in worker
processes (started with the `spawn` method, one per shard, at most
`max_workers` at a time).

- *Plan.* The selection × `partitions` (configured; chosen per run through
  the `partitions` multi-choice parameter), each partition cut into
  duration-balanced chunks — history from the store over the last
  `history_depth` dispatches of runs matching `history_selector.labels`;
  round-robin on a cold start, and the plan text says so. One
  (partition, chunk) is one shard, declared with its item ids. The planner is
  `plan_chunks` in the SDK, for any runner to reuse.
- *Exact selection.* Tests are chosen by exact id, not by name pattern: an id
  containing `[` or `*` is that one test. `suite_root` or `suite_roots`: with
  several roots each item is executed from the root it came from.
- *Execution.* `variables` for all, `partition_variables` per partition, the
  partition itself under `partition_variable` (default `PARTITION`).
  `executed_tag` (a template over `{partition}` / `{shard}`) is stamped on
  every executed test. `worker_setup` — import-path callables — run inside
  each worker before Robot and may contribute variables; `pythonpath` and
  `listener` are passed on. `max_tests_per_chunk` bounds a single Robot
  execution; `rerun_failed: N` re-executes failures up to N times, each
  attempt delivered with its attempt number. Everything a shard executed is
  merged into one `output.xml` / `log.html` / `report.html` (`log`, `report`,
  default on), recorded as artifacts.
- *Results* are parsed with the configured ResultParser (`format`, else the
  run's expected format, else `robot-output-xml`) — so a parser configured
  under `parsers:` applies to what this runner produces as well.
- *Refusals before the hand-off:* corpus drift (§3.3; `allow_drift`, also a
  per-run parameter, executes the intersection and reports the rest as
  `SKIP` / `drift`), a failing or overrunning `pre_run_hooks` command
  (`hook_timeout_s`), a missing suite root.
- *Stopping.* Cancellation, and `shard_timeout_s`, stop worker processes;
  outstanding shards are not started. Delivered shards are kept. (A hook
  already running is ended by its timeout, not by cancellation.)
- *Live status* comes from a listener streaming through the live channel
  (`live_status`, default on).

**b) `jenkins`** — triggers an existing parameterized Jenkins job. Options:
`base_url`, `job` (folders as `folder/job`), `parameters` — a mapping of *your
job's* build-parameter names to templates over `{spec_json}` `{run_id}`
`{dispatch_id}` `{callback_url}` `{token}` `{item_ids}` `{title}`
`{labels[key]}` `{params[name]}` (per-run parameters declared under
`run_parameters`) — `completion: callback | poll`, `callback_base`,
`artifact_paths` (glob patterns over the build's archived files),
`format`, `user` / `api_token` (give them as `${env:…}`), `verify_tls` /
`ca_bundle`, `request_timeout_s`, `retries` with backoff on transient HTTP
errors, `poll_interval_s`, `timeout_s`.

With `callback` the job POSTs its bundle to `{callback_url}` using
`{token}`; `dispatch` returns once the build is queued and the run waits in
`AWAITING_RESULTS`. With `poll`, runcomposer follows the queue item the
trigger returned to *its* build — never "the latest build of the job" — waits
for it, downloads what matches `artifact_paths` and delivers it; the build
URL is recorded as an artifact link. `cancel` stops the build (or cancels the
queue item); `health` probes the server and the job. A rejected trigger is a
refusal; a build that never finishes is a `FAILED` dispatch.

The CI side needs a thin consumer — a job step that runs `runcomposer-exec`
(§6.2c) and, for `callback`, a POST of the bundle. A reproducible
Jenkins-in-docker setup with that job is in `ci/jenkins/`. What is verified
how: the test suite runs the runner against an in-process HTTP stand-in that
speaks the Jenkins endpoints it uses (crumb + session cookie, queue item →
build, artifact listing and download, stop, cancel); the docker setup is the
manual end-to-end check against a real Jenkins.

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
the command as `RUNCOMPOSER_PARAM_<NAME>`). Any framework that starts from a
command line and writes a result file is connected by configuration alone.

**e) `demo`** — **simulated**. No test is run: verdicts derive from a seed
(a per-run parameter), so the compose → dispatch → results loop can be shown
without a test framework. It says so itself (`simulated` capability, every
failure message). Only dispatchable where it is configured.

### 6.3 `RunStore`

Persists runs, specs, dispatches (state, declared shards and their items,
plan, links, error, parameters, outcome summary, owner, cancel request),
the exact bytes handed out per dispatch, ingest-token hashes, deliveries,
verdicts, live status, artifact references and the event stream. **The store
records; it decides nothing** — lifecycle state is computed by
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
- `duration_aggregates(labels, last_n)`;
- `events(run_id, after, limit)`.

Reference impl: `sqlite` (default, zero-setup), safe to share between the
threads of a server and between processes on one file: WAL journal and a busy
timeout, one connection per thread, every write one short
`BEGIN IMMEDIATE` transaction — so `record_delivery`'s check-then-write
cannot interleave with another. `path: ":memory:"` is supported for tests and
throwaway use.

**Schema version and migration.** The sqlite schema is versioned in
`PRAGMA user_version` and migrated in place. A database written by 0.1.x
opens as it is: deliveries without a dispatch get an `ingest` dispatch, where
0.1 left several deliveries for one shard the newest wins, a plaintext token
in a stored document is replaced by its hash, a dispatch that was executing
is `STALE` — and completion is **recomputed** under §4's rules, so a run 0.1
called `PASS` although planned items never reported is `INCOMPLETE`. A
database from a newer schema is refused.

**Port evolution.** 0.2 is a breaking revision of every port. From here the
ports grow by *addition* within a minor line: new members, and keyword-only
parameters with defaults on existing ones.

A second store over `postgres` is `[planned]`; until it exists, an adopter
who must live in an existing database writes their own against this port.
Stated plainly: **history features are dark on a fresh store** — failed-rerun
and duration-balancing activate as runs accrue. The one store that starts
warm is the demo's (§12).

### 6.4 Artifacts

Not a port: the store records artifact references
`(name, media_type, url_or_path, shard)` and reads them back. They surface in
`GET /api/v1/runs/{id}` under `artifacts`, each resolved to something a
reader can follow.

**`url_or_path` is one field with two meanings**, and the resolution is
explicit about which:

- an absolute **`http`/`https` URL** is a remote artifact (a CI build link).
  It passes through as the href verbatim; runcomposer never fetches, proxies,
  or validates it. Any other scheme — `file:`, `data:`, `javascript:` — is
  deliberately *not* treated as a URL: handing one out as a link is a
  link-injection vector.
- anything else is a **local path**, servable only when it really lies inside
  `core.artifact_dir`. A path outside stays visible in the payload with no
  href, rather than offering a link that cannot work.

A built-in route serves `/artifacts/{run_id}/{dispatch_id}/…` — the layout of
`ctx.workdir`. Two safety properties, both deliberate:

1. **Containment, checked after resolution.** Artifact paths are
   attacker-influenced. The request path is resolved fully and refused unless
   it lands strictly inside the configured directory, so `..` segments,
   absolute paths, and symlinks pointing out of the tree are one rule rather
   than three patches. A refusal is a `404`, identical to a miss.
2. **Sandboxed delivery.** Artifact bytes are untrusted content served from
   the same origin as the API and the UI. They go out with
   `Content-Security-Policy: default-src 'none'; sandbox` and
   `X-Content-Type-Options: nosniff`, so an HTML artifact cannot script
   against this origin. A report that needs its own JavaScript does not
   render — host those where they belong and record the URL.

**Retention is config, not an afterthought:** `core.retention.max_age_days`
(`null` = no age limit) and `max_runs`, applied by `runcomposer gc`.
Artifacts live and die with their run: pruning or deleting a run removes its
directory, and a directory that belongs to no stored run is swept.

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

`runcomposer.sdk` is the connector surface: the protocols, the records, the
error taxonomy, and the helpers a connector needs — the chunk planner,
content hashes, the marker reader, the defused XML reader, lifecycle
functions, parameter validation — plus `load_config` and `Service` for
driving runcomposer from Python (compose → dispatch → wait → read, in one
process). Its names keep their meaning within a minor line; a removal is a
breaking change listed in the changelog. A frozen list of the names in the
test suite makes every change to it deliberate.

A conformance kit for connector authors (`runcomposer.testing`) and a
copy-to-start connector package under `examples/` are `[planned]`.

---

## 7. History-based selection ("rerun what failed")

A **selection provider** resolved at compose time — the same
resolve-then-materialize pattern taxonomy refs use:

```
compose-time: RunStore query → item ids materialized into selection
            → spec records derived_from provenance (§3.1)
```

The full picker — `Select items by history: [failed] in [latest completed run
| run <id> | last run before <date>] (filter: labels)` — is `[planned]` in the
UI, which today offers one fixed `failed@latest` button. Every selector and
scope below is already reachable from the CLI and the API.

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

---

## 8. Configuration

```yaml
# config.yaml — one file, layered sections
core:
  api: { host: 127.0.0.1, port: 8100, cors: [...] }
  taxonomy_file: taxonomy.yaml        # curated tree over tag patterns; format: docs/taxonomy.md
  artifact_dir: artifacts/
  retention: { max_age_days: 90, max_runs: null }      # null = no limit
  dispatch: { max_concurrent: 2, handoff_wait_s: 2.0 }
  ingestion: { tokens: required, inbox: results_inbox/, max_upload_mb: 200,
               quarantine_dir: quarantine/, quarantine_max: 100,
               poll_interval_s: 2.0, settle_s: null }
  locale_default: en
store:                                 # exactly one entry
  sqlite: { path: runcomposer.db }
sources:                               # one or more → one catalog (§6.1)
  shop-ui:  { plugin: robotframework, roots: [ui/tests, api/tests] }
  shop-api: { plugin: manifest, path: api-catalog.json }
runners:                               # the key IS the runner id
  robot-pool: { suite_roots: [ui/tests, api/tests], max_workers: 8,
                partitions: [staging, preview] }
  nightly-ci: { plugin: jenkins, base_url: "https://ci.shop.example", job: shop/nightly,
                user: runcomposer, api_token: "${env:SHOP_CI_TOKEN}",
                callback_base: "https://runcomposer.shop.example" }
  smoke:      { plugin: command, command: "./run-smoke.sh {ids_file} {out_dir}",
                format: junit-xml }
parsers:                               # options per result format id
  junit-xml: { pattern: "TEST-*.xml" }
observers:
  team-chat: { plugin: webhook, url: "https://chat.shop.example/hooks/tests",
               secret: "${env:SHOP_HOOK_SECRET}", events: [run.completed, run.failed] }
ui:                                    # served at /api/v1/ui-config and /meta
  quick_filters: [...]
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
  keys. Then every configured plugin is constructed once — construction must
  be free of side effects — and the taxonomy is read, so `serve` and `doctor`
  refuse a deployment that could not have worked. Unknown plugin ids fail the
  same way.
- **Secrets.** A string option of the form `${env:NAME}` or
  `${env:NAME:-default}` — at any depth of an option value — is replaced from
  the environment when the plugin is built; a missing variable is a
  `ConfigError` naming it. Configured options never travel into a run spec,
  an API response or a log line, so a value resolved this way does not
  either — and where a plugin quotes one of its options anyway (a startup
  refusal, a runner's refusal reason or error, its health, a log line), the
  values that came from the environment are blanked out before anything is
  answered, recorded or logged. (Values shorter than four characters are
  not: blanking those would mangle ordinary text. Do not use them as
  secrets.) Plugin *selection* is never read from the environment, and the file
  itself is chosen with `--config` only.
- **`connectors:`** is a free-form mapping the core never interprets — the
  place for a connector package's own settings, read with
  `config.connector_settings("<name>")`. Any other unknown top-level key is an
  error that says so and names `connectors:`.
- **`parsers:`** configures result parsers by format id, with the same
  `module:` / `plugin:` selection. A run spec names a format id only (§5).
- **Zero-config.** With no config file at all runcomposer runs on built-in
  defaults *spelled as configuration*: the sqlite store in
  `./runcomposer.db`, the `demo-corpus` source, the bundled taxonomy, and the
  `demo` runner. A config file that leaves out `store:` or `sources:` gets
  those two defaults; runners are never implied.

**One path base.** A relative path anywhere in the config file resolves against the
**config file's own directory** — the core's keys (`taxonomy_file`,
`artifact_dir`, `ingestion.inbox`, `ingestion.quarantine_dir`) and the plugin
sections alike. A config directory is therefore portable, and one `--config`
behaves identically from every working directory. Absolute paths are left as
written.

The core must not decide what a plugin's options *mean*, and "which of these
strings are paths" is exactly that kind of decision — a listener spec, a
shell command and a URL are not paths. So the plugin says so: a class
attribute `PATH_OPTIONS = ("suite_root", …)` names the options (single values
or lists) that are anchored to the config file, and a plugin whose rule has
exceptions implements `resolve_config_paths(options, resolve)` instead and
resolves them itself — the sqlite store does, because `:memory:` names no
file at all. The scope is the *config file*: a path inside a **run spec** is
the document's own value and is never rewritten.

**The taxonomy file is validated on the same terms** — it is the other thing a
deployment writes by hand. Top-level `taxonomy` must be a list; each node
needs a non-empty `label`, a `filter` (when present) that is a single pattern
string the §3.1 grammar accepts, and a `children` (when present) that is a
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
GET    /api/v1/meta                       # boot info in one call: version, locale, runners +
                                          #   parameters, catalog size, taxonomy, what is
                                          #   configured (loaded) and what is installed
GET    /api/v1/ui-config
GET    /api/v1/runners[?refresh=true]     # configured runners: id, capabilities, parameters,
                                          #   health (cached 30 s)
GET    /api/v1/catalog/tags               # every tag with its item count
POST   /api/v1/catalog/reload             # re-read the corpus now
GET    /api/v1/taxonomy[?resolve=true]    # the validated file; or resolved against the catalog

POST   /api/v1/selection/compile          # preview: matched items, total, warnings, derived_from
POST   /api/v1/selection/spec-preview     # the would-be runspec — nothing is stored

POST   /api/v1/runs                       # compose (+ dispatch: {runner, parameters} | {mode: export})
GET    /api/v1/runs                       # list: state, label, q, since, until, limit, offset → total
GET    /api/v1/runs/{id}                  # detail (below)
DELETE /api/v1/runs/{id}
GET    /api/v1/runs/{id}/items            # the materialized selection
GET    /api/v1/runs/{id}/spec             # the stored document (never a token)
GET    /api/v1/runs/{id}/events?after=    # the event stream after a cursor; wait= long-polls
GET    /api/v1/runs/{id}/verdicts         # dispatch, shard, status filters; limit/offset
POST   /api/v1/runs/{id}/dispatch         # dispatch or export a stored run
POST   /api/v1/runs/{id}/cancel           # ?force=true closes it whoever owns it (§4)
POST   /api/v1/runs/{id}/rerun            # scope: all | failed
POST   /api/v1/runs/{id}/finalize
GET    /api/v1/runs/{id}/dispatches/{dispatch_id}/spec    # the exact bytes handed out
POST   /api/v1/runs/{id}/dispatches/{dispatch_id}/token   # rotate the ingest token
POST   /api/v1/runs/{id}/results          # ingestion push (multipart; token)

GET    /api/v1/quarantine
POST   /api/v1/quarantine/{id}/attach     # bind to an existing run (logged)
POST   /api/v1/quarantine/{id}/promote    # its own `origin: ingested` run
DELETE /api/v1/quarantine/{id}

GET    /artifacts/{run_id}/{dispatch_id}/…   # local artifact files (§6.4)
```

**Nothing waits for an execution.** `POST /runs` with a dispatch, `POST
/runs/{id}/dispatch` and `/rerun` answer as soon as the dispatch is recorded
(§4); the run comes back `DISPATCHED` or `RUNNING`. A dispatch refused within
the hand-off wait is the error of that request (`409`, `dispatch-refused`)
— and when it was composed by the same request, the run is removed again
unless the request said `keep_run`; the error body says which (`run_kept`).
Everything checkable up front — the runner id, its parameters — is checked
before a run exists. A refusal that arrives *after* the answer leaves the run
`COMPOSED` with a `dispatch.refused` event: by then the run is the record of
the refusal.

**A run row is complete for a list** — verdict counts by status, planned
count, duration, the latest dispatch's mode, labels, origin — and `total` is
the size of the whole filtered set. **A run detail** adds deliveries, verdicts
(shard, attempt, duration, `live`), a per-shard roll-up (`shards`: counts and
computed completion per shard — "green on one partition, red on the other" is
read, not aggregated), artifacts with hrefs, recent events, and the
`selection` the run was composed from (filter AST, picked ids, history
provenance), so a client can load it back into a builder. The verdict list is
flat and labelled rather than nested by shard: a per-item view wants it flat.

**Exports are byte-exact.** An export answers with `spec_url`, `spec_sha256`
and — once — `ingest_token`. The URL serves exactly the bytes the hash was
computed over, any number of times, never containing the token; a client that
needs the document downloads it rather than re-serializing JSON it was shown.

**Errors are uniform.** Every error body is `{"detail": <string>, "code":
<stable id>}` — a domain error, a failed request validation (`400`,
`invalid-request`, plus a `fields` list), an unknown route. Status codes
follow from the *type* of the error, mapped in one place: not found `404`,
invalid request `400`, state conflict `409` (`run-active`,
`dispatch-refused`, `spec-conflict`, …), token `401`/`403`, unreadable bundle
`422`, too large `413`; a deployment that broke under a running server (the
taxonomy file) is a `500` that says what is wrong. Request bodies are
validated before anything acts on them.

`?resolve=true` on the taxonomy is a parameter rather than a second endpoint:
the unresolved body is the published shape and a client that asks for the
file must keep getting the file. Static files are served with
`Cache-Control: no-cache` for `/` and the locale files, and as immutable for
the hashed `/assets/`.

Runner-specific admin actions (e.g. a pool reload) are `[planned]`.

**CLI** — the flag-level reference is [docs/cli.md](docs/cli.md):

| | |
|---|---|
| look | `catalog` · `taxonomy-check` · `compile` · `validate` · `doctor` |
| compose | `spec` (`--dry-run`, `--export`, `--runner` / `--param`) · `export-spec` |
| run | `run` (compose → dispatch → wait → summary) · `dispatch` (a spec file or `--run`) · `rerun` (`--scope all\|failed`) · `cancel` (`--force`) · `watch` |
| read | `runs` (`--failed-in`) · `show` · `export --format ctrf` |
| results | `ingest` · `quarantine list\|attach\|promote\|discard` · `finalize` |
| operate | `serve` · `gc` · `delete` · `demo` |

`run`, `dispatch` and `rerun` execute the runner in the CLI's own process and
return when it did; `--no-wait` only concerns a run left `AWAITING_RESULTS`.
A spec *file* handed to `dispatch` whose run is already stored must be that
run's document: an edited selection, source or results contract is refused.
Its `runner` section is the exception — the one open section states the
per-run parameters for this dispatch, and is validated like `--param`.
`--json` prints one JSON document — the same shapes the API returns. Exit
codes are the same everywhere: `0` done, or the run is `PASS`; `1` the answer
is no (the run is not `PASS`, a spec is invalid, a bundle refused, a run
unknown); `2` the command line or the configuration is wrong; `3` the run
ended without a result (refused, failed, cancelled, stale, timed out). Every
command that reads a deployment takes `--config` — the only way to select a
config file. Plus the
vendorable **`runcomposer-exec`** consumer (§6.2c), shipped as its own tiny
artifact.

Logging uses the `logging` module under `runcomposer.*`; `serve` installs a
default handler (`--log-level`), the other commands print warnings and, with
`-v`, what they do.

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
   sugar lives here.
2. **Config-driven environment** — whatever the deployment puts under `ui:` is
   served from `/api/v1/ui-config`, never hardcoded. The UI reads
   `quick_filters` today; `examples` and partition pickers are `[planned]`, so
   a key the UI does not yet read is served and silently ignored.
3. **Runner-aware compose footer** — "Run with: [robot-pool ▾] | Export spec";
   runs list shows dispatch mode + lifecycle state incl. `AWAITING_RESULTS`;
   a quarantine view for unmatched deliveries.

**The bundled UI predates the 0.2 API** and is being reworked against it.
What the API offers and the UI does not use yet is `[planned]` on the UI
side: a form rendered from a runner's declared parameters (§6.2), cancel /
rerun / delete, the event stream and live progress, paging and search in the
runs list, the per-shard roll-up and artifact links in the run detail, the
exported document downloaded byte-exact from `spec_url` (§9), and the states
and statuses 0.2 added (`FAILED`, `CANCELLED`, `STALE`; `MISSING`,
`INCOMPLETE`).

**Stack — decided:** current React/Vite/deps at P1; the SVAR filter widget
stays as the filter-builder (MIT, actively maintained, React-19 peer deps;
verified 2026-07-06), kept behind the existing adapter so it remains
swappable. `react-aria-components` (Apache-2.0) carries the tag picker's tree
and modal: a multi-select tree with real keyboard semantics and screen-reader
behaviour is the whole point of that dialog and a bad thing to hand-roll. It
costs ~58 kB gzip of the bundle, and its tree is virtualized because the
resolved tree grows with the catalog. P1 spike: SVAR built-in label localization for the i18n story;
override via the adapter if not exposed. Per-item *live* state requires the
listener path (§6.2a); without it the grid shows planned → final, honestly
labeled.

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
  evaluators).
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
| 3 | UI stack | **Refresh to current React/Vite at P1; keep SVAR** behind the adapter; localization spike noted. |
| 4 | License | **MIT.** |
| 5 | `runcomposer-exec` distribution | **Both by construction**: stdlib-only single file, also pipx-installable; JSON spec input so zero deps. |
| 6 | Who mints the dispatch id (2026-08-28; the mechanism is superseded by #11/#12) | **runcomposer does**, offered to the runner as a `DispatchReservation` via the optional `bind_dispatch` hook (§6.2); the runner decides *when* the hand-off is recorded, and the returned handle refines the declaration. Rejected: passing the id into `dispatch()` (breaks the published signature), and letting bound runners write their own dispatch rows (duplicates the ledger, unavailable to unbound runners, and lets a runner's own id drift from its configured `mode`). A refused dispatch — refusal always precedes the recording — leaves **no** row. |
| 7 | Shape of the shard in run detail (2026-08-28) | **Label the flat verdict list, add a sibling roll-up** (§9). Rejected: nesting verdicts by shard — it breaks every existing reader for a view a per-item table does not want; and leaving the aggregation to clients — the fan-out question is the reason shards exist, so answering it is the payload's job. |
| 8 | Serving local artifacts (2026-08-28) | **Containment + sandbox** (§6.4): resolve the path fully and refuse anything not strictly inside `core.artifact_dir` (one rule covering `..`, absolute paths and symlinks), refuse as `404` so probing is uninformative, and serve with `sandbox`/`default-src 'none'`/`nosniff` because the bytes are attacker-influenced and share an origin with the UI. Accepted cost: a scripted HTML report renders inert; the §6.4 pass-through (host it elsewhere, record the URL) is the answer for those. Rejected: a bare static mount (no containment story, scripts live), and forcing `Content-Disposition: attachment` on everything (kills the one-click log the feature exists for). |
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
