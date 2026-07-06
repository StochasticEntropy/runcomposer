# runcomposer — design

An open-source, tag-based test run composer & orchestrator.

Status: rev 3 — 2026-07-06. Decisions final unless marked open.
This document supersedes all notes in the private predecessor prototype.

---

## 0. Why runcomposer exists

A private predecessor prototype (two iterations, 2026) proved the product idea:
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
- **Two proven runners at launch** — a local process pool and a CI trigger —
  plus a first-class **export mode**: the spec must let any third-party
  executor integrate with **zero core interface changes**.
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
index), npm free. Name reservation = publish the prepared 0.0.1 placeholders
in `reserve_name/` (owner action, needs account tokens). Runner naming axis is
*what-it-drives*: `robot-pool` (in-process Robot Framework execution),
`ci-trigger` (drives an external CI job).

**License — decided: MIT.**

---

## 2. Domain model (core vocabulary — the only words the core knows)

| Term | Meaning |
|---|---|
| **Item** | A runnable test: `id`, `name`, `tags: [str]`, optional `hierarchy`, optional `meta` (opaque). |
| **Item id (normative)** | An opaque, stable string **minted by the TestSource**. Invariant: `TestSource.items()[].id` and `TestSource.resolve(native_name)` define the *same id space* — every native name a ResultParser emits must resolve to exactly one Item id. The core never parses, splits, or normalizes ids or native names; it compares ids for equality only. (A Robot Framework source mints `id = longname`; a pytest source might mint `id = nodeid`.) |
| **TestSource** | A plugin that enumerates Items, produces a content-hashed **catalog snapshot**, and owns native-name→id resolution. |
| **Taxonomy** | A curated tree over tag patterns used for navigation and selection-building. Data, not code. |
| **Selection** | The lossless filter: `tag_filter` AST + optional explicit `item_ids`. Compiled against a catalog snapshot into a **materialized item list**. |
| **Run spec** | The versioned document: identity + selection (incl. materialization) + source snapshot + results contract + one opaque runner section. |
| **Run / Dispatch / Delivery** | A **Run** is the stored lifecycle record for one spec. Each hand-off to an executor is a **Dispatch** (`dispatch_id`). Each results bundle that arrives is a **Delivery** (content-hashed). One run may have several dispatches (re-runs) and several deliveries (shards, retries). |
| **Verdict** | Per-item result: `PASS / FAIL / SKIP / ERROR`, duration, message, artifacts, `attempt` (per-dispatch retry counter; `flaky` is derivable, not stored). |
| **Runner** | Anything that fulfills a run spec. In-process runners are plugins; external runners just consume the document. |

Framework-specific concepts (suites, Robot `longname`, stage fan-out,
`output.xml`) live entirely in the framework plugins' vocabulary.

---

## 3. The run spec (the centerpiece)

Versioned YAML — **JSON-isomorphic and accepted everywhere**; the CLI can emit
either (`runcomposer spec --format json|yaml`), so zero-dependency consumers can
read the spec with nothing but a JSON parser. Published JSON Schema. Design
rules:

- **Core sections are generic and closed** within a spec version (see
  versioning policy below).
- **Exactly one open section: `runner`.** Namespaced by plugin id; the core
  never interprets its contents.
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
    item_ids: ["Payments.Cart.T001", "..."]   # THE authoritative executed set
    at: "2026-07-06T09:14:02Z"
    count: 412
  derived_from:                     # optional provenance for materialized inputs
    - provider: "history"
      query: { run: "LATEST", verdicts: [FAIL] }
      resolved_run_id: "01JZ8ZZ..."

source:
  provider: "robotframework"
  root: "tests/"
  snapshot: "sha256:9f2c..."        # catalog hash at compose time — an INTEGRITY CHECK, not an alternate compile path

results:
  expect:
    - format: "robot-output-xml"    # registered ResultParser id
  shards: "runner-declared"         # or an integer when known at compose time (see §4)
  deliver: "api"                    # api | file-drop | none (advisory)
  callback: "https://runcomposer.example.internal/api/v1/runs/{run.id}/results"
  token: "rct_..."                  # per-run ingest token (see §5); omit only if deployment disables tokens

runner:                             # ONE opaque namespaced section
  robot-pool:
    suite_root: "tests/"
    partitions: ["env1", "env2"]
    variables: { STAGE: "test" }
    listener: "MyListener.py"
    # plus whatever else the plugin defines (§6.2a enumerates the reference set)
```

Filter grammar notes (deliberate, not inherited):

- `prefix:X` is a filter primitive defined as pure sugar for `regex:^X`
  (the prototype's filter grammar had literal + `regex:` only; `prefix`
  existed only as a taxonomy rule type — adopting it unifies the grammars).
- The prototype's `combine_op` between `tag_filter` and `item_names` is
  **dropped**: its code only ever produced AND, and fine-selection
  semantically *narrows* a filter result. Fixed rule: both present →
  intersection.
- The prototype's `allow_missing_test_names` flag is **dropped**: unknown ids
  at compose time are an error; re-execution drift is governed by the drift
  policy (§3.3).

### 3.2 What deliberately stays OUT of the core schema

Anything framework-, CI-, or deployment-specific: framework variables, code
generation flags, stage/partition vocabulary, CI job names and integer build
numbers, host-derived identity, artifact file naming, result-file paths. All
of it lives either in the `runner.*` namespace (the plugin's own vocabulary)
or in runner/store policy — never in core sections. In the prototype, exactly
one of ~19 exchange-file fields was generic; that ratio is the failure mode
this rule exists to prevent.

### 3.3 Executor contract

A conforming executor MUST:

1. **Execute exactly `selection.materialized.item_ids`.** The embedded list is
   authoritative. `tag_filter` is provenance; `snapshot` is an integrity
   check. Executors do not re-compile selections. (Convenience: in-process
   runners may fetch the list via `GET /runs/{id}/items` — same data, same
   authority.)
2. **Check drift before executing:** if the live corpus snapshot differs from
   `source.snapshot`, refuse by default. With an explicit override
   (`--allow-drift` / runner option), execute the intersection of
   `materialized.item_ids` with the live corpus and report the difference as
   `SKIP` verdicts with reason `drift`.
3. **Deliver results that reference `run.id`** — and, when the execution is
   split, a `shard` label per bundle (§4/§5).

It MAY ignore everything else, including the whole `runner` section. That is
the contract: three obligations, one document.

---

## 4. Run lifecycle & identity

```
COMPOSED ──dispatch(runner)──► DISPATCHED ──(optional live status)──► RUNNING
    │                               │                                    │
    │ dispatch(mode=export)         └──────────► AWAITING_RESULTS ◄──────┘
    └───────────────────────────────────────────────┘
                                                     │  deliveries ingested (§5)
                                                     ▼
                                          COMPLETE(PASS | FAIL | ERROR)
```

- **Identity is layered:** `run.id` (the spec) → `dispatch_id` (each execution
  attempt; an `export` download also mints one) → `shard` (a runner-declared
  partition/chunk label on each delivery). Re-running the same spec = a new
  dispatch under the same run; verdict sets group per dispatch; the run's
  headline state reflects the latest dispatch.
- **Completion is computed, never guessed.** At dispatch, the runner declares
  its expected shard set/count (after planning); the run reaches `COMPLETE`
  when all declared shards have delivered, or on explicit
  `POST /runs/{id}/finalize`. Export-mode dispatches default to `shards: 1`
  unless the spec says otherwise; `finalize` is always available. A
  store-level expiry policy (`AWAITING_RESULTS` > N days → `STALE`) handles
  abandonment.
- **Live status** (`RUNNING`, per-item progress) is an optional runner
  capability. For `robot-pool` it comes from the runcomposer Robot listener
  streaming verdicts during the run (§6.2a) — not from the pool itself. UIs
  degrade gracefully to "dispatched, awaiting results".
- **Unsolicited results** (unknown or missing `run.id`) do **not** create run
  records by default. They land in a visible **quarantine inbox** for manual
  attach/import (`runcomposer ingest --allow-unsolicited` or a UI action promotes
  them to `origin: ingested` runs). Rationale: on shared result transports,
  "the newest bundle is somebody else's run" is a routine event — auto-accept
  would silently swallow exactly those.

---

## 5. Result ingestion

Dispatch and result-return are **decoupled**. One pipeline, three transports,
pluggable parsers.

**Transports:** (1) API push — `POST /api/v1/runs/{id}/results` (multipart:
artifacts + declared format + optional `shard`); (2) **file-drop inbox** — a
watched directory for git-transported or air-gapped bundles; (3) manual — UI
upload or `runcomposer ingest <path> [--run <id>]`.

**Correlation & the bundle marker.** Every delivery must carry `run.id`. For
transports that can't set HTTP fields, the bundle contains a
**`runcomposer_run.json` marker** `{run_id, dispatch_id?, shard?, spec_sha256}`,
written next to the native artifacts by whatever executed the run
(`runcomposer-exec` writes it — §6.2c; CI jobs write it in their post step).
Ingestion matches on `run.id` **and** verifies `spec_sha256` when present; a
mismatched or markerless bundle goes to quarantine (§4), never silently into a
watched run.

**Idempotency (normative):** every delivery is content-hashed. Re-ingesting a
byte-identical bundle is a **no-op** (file-drop pollers, git re-pulls, and CI
webhook retries all redeliver in practice). A *new* bundle for the same
`(run.id, dispatch_id, shard)` **replaces** that shard's verdicts —
last-writer-wins per shard. There is no "monotonic merge": corrections and
re-runs must be able to flip FAIL→PASS.

**Security floor (not deferred):** ingestion endpoints require the per-run
**ingest token** minted at compose time (`results.token`; deployments may
disable for closed networks). XML parsing is defused (no external entities,
no DTD expansion) as a stated ResultParser obligation. Upload size and
quarantine-count limits are config. Full authN/authZ stays out of scope
(§13), but the write path is not open by default.

**ResultParser plugins** turn native artifacts into Verdicts.
`robot-output-xml` ships first; `junit-xml` second. **The anti-leak litmus
test is about identity, not parser count:** the core never string-compares
native names — every native name goes through
`TestSource.resolve(native_name) → id`, so all name-normalization quirks live
inside the source plugin that owns them. A guard test asserts the core
contains no native-name normalization.

**Partial results:** parsers emit per-item verdicts per delivery; the run
aggregates per §4's shard accounting. Verdicts carry `attempt` so
retry-within-run (flake detection) is representable; `runcomposer export --format
ctrf` (P3) emits a normalized cross-tool result document — fitting for a
project whose thesis is document-shaped interfaces.

---

## 6. Ports (in-process plugin protocols)

Four small protocols. **Plugin loading:** a config declaration
`module: mypkg.MyRunner` (import path — the hack-it-in-an-afternoon,
self-hosted path) **or** Python entry points (`runcomposer.*` groups — the
packaged-distribution path). Both first-class. No env-var module loading.

### 6.1 `TestSource`

```python
def items() -> list[Item]
def snapshot() -> str                            # content hash
def resolve(native_name: str) -> ItemId | None   # parser-name -> id (None = unknown, ingestion warns)
```

Reference impls:
- **`manifest`** — a JSON/YAML catalog; requires **only `id` + `tags`**
  (`name`, `hierarchy`, `meta` optional). This is the zero-dependency adopter
  path. Docs ship **two** examples: a Robot Framework catalog and a pytest
  catalog (`id: "tests/test_cart.py::test_checkout[visa]"`) — the
  framework-agnostic claim is demonstrated, not asserted.
- **`robotframework`** — walks `.robot` files via `robot.api` (a dependency of
  the plugin, not the core); mints `id = longname`; `resolve` owns any
  name-normalization quirks.

### 6.2 `Runner`

```python
def describe() -> RunnerInfo                 # id, capabilities (live_status, cancel, health)
def dispatch(spec: RunSpec) -> DispatchHandle
# DispatchHandle carries: dispatch_id, declared shards (set/count), links
# optional capabilities:
def status(handle) -> RunnerStatus
def cancel(handle) -> None
def health() -> dict                         # runner-defined, displayed verbatim; NO mandated keys
```

`dispatch` takes **the document only**. A core helper `materialize(spec)`
exists for in-process runners that want the item list as objects; external
consumers read the spec.

**a) `robot-pool`** — in-process Robot Framework execution: shared process
pool; partition fan-out; duration-balanced chunking; per-run artifact
isolation (unique output paths per dispatch) and any readiness markers are
**this runner's** policy. Its `runner.robot-pool` options include:
`suite_root`, `partitions`, `variables`, `listener`, `pre_run_hooks`, and
duration-history tuning. Two explicit facts:

- **Duration history** is read from the RunStore keyed by **item id** over the
  last N completed dispatches matching a configurable label selector
  (`history_selector: {labels: ...}`). **Cold start:** a fresh store has no
  durations; chunking degrades to round-robin (documented, visible in the plan
  output) and balances itself as history accrues.
- **Live per-item status** comes from the **runcomposer Robot listener** (a small
  listener the plugin injects) streaming verdicts to the store during the run.
  The output.xml parse at the end is the terminal/degraded path. Without the
  listener, the UI shows planned items + final results — stated, not hidden.

**b) `ci-trigger`** — triggers an existing parameterized CI job (Jenkins
first): templates the job URL/params from the spec, passes the rendered spec
+ `run.id` as a build artifact/param, and receives results. **The CI side
needs a thin consumer** — a job stage that runs `runcomposer-exec spec.json`
(§6.2c, same tool) and a post step that POSTs the results bundle (with marker
+ token) back to `/runs/{id}/results`. Completion signal = that webhook-out
POST; runcomposer-side polling of the CI build API is the fallback for CI systems
that can't call out. The consumer stage is a **named P3 deliverable**, not
assumed away.

**c) Export / remote round-trip** — for executors runcomposer never talks to
directly (e.g. a remote checkout that receives code via git and returns
result bundles the same way). A deliberately tiny consumer, **`runcomposer-exec`**,
reads the spec → renders the runner-native invocation (for Robot Framework:
`--test <id>` args from `materialized.item_ids`, plus
`runner.robot-pool.variables`) → runs it → writes the `runcomposer_run.json`
marker next to the outputs so any bundle-transport carries correlation home.
The compose side exports the spec; the file-drop inbox ingests the returned
bundle; marker mismatch → quarantine. Zero *core interface* changes for new
transports; zero *code* on the executing side was never realistic — the
consumer is the honest minimum, so it is designed to be minimal:

**`runcomposer-exec` distribution — decided: both, by construction.** It is built
as a single self-contained **stdlib-only** Python file (reads the spec as
JSON; YAML only if a parser happens to be importable) and distributed both as
a pipx-installable package and as that same file published per release for
vendoring/wget. Single-file is a property of the artifact, not a deployment
assumption — no adopter's host administration is baked into the design.

### 6.3 `RunStore`

Persisted schema (normative): `runs`, `specs`, `dispatches`, `deliveries`,
`verdicts`, `artifact_refs`. **No runner-lifecycle fields** — runner health
and pool state are ephemeral runner memory, never store schema (a prototype
lesson: leaked runtime keys calcified into persisted state).

The history-query surface is explicit because §7 depends on it:

- runs carry **completion status + completed_at** (the LATEST / by-date
  selectors operate on these — "latest" means latest *completed*);
- verdicts are queryable by `(run selector, verdict filter)` → item ids;
- duration aggregates by item id over recent completed dispatches (§6.2a).

Reference impls: `sqlite` (default, zero-setup) and `postgres`. Stated
plainly: **history features are dark on a fresh store** — failed-rerun and
duration-balancing activate as runs accrue; the demo pre-seeds both levels
(§12). runcomposer starts fresh everywhere; there is no migration machinery in
the core (a private adopter can seed its store with its own scripts if it
wants warm history).

### 6.4 Artifacts

Not a port: the store records artifact references
`(name, media_type, url_or_path)`; a built-in local artifact directory serves
`/artifacts/{run_id}/{dispatch_id}/`; remote URLs (CI links) pass through.
**Retention is config, not an afterthought:** max age / max runs per store
with a `runcomposer gc` command; quarantine and artifact dirs are bounded (§5).

---

## 7. History-based selection ("rerun what failed")

A **selection provider** resolved at compose time — the same
resolve-then-materialize pattern taxonomy refs use:

```
UI: "Select items by history: [failed] in [latest completed run | run <id> | last run before <date>] (filter: labels)"
→ compose-time: RunStore query → item ids materialized into selection
→ spec records derived_from provenance (§3.1)
```

The executed spec stays static and reproducible; provenance keeps it
auditable. Locale date words are UI sugar, never spec content. The
run-completion vs. item-verdict distinction (select the run by *completion
status*, filter items by *verdict*) is carried by §6.3's two-level history
contract.

---

## 8. Configuration

```yaml
# config.yaml (single file, layered sections; hot-reload out of scope for v1)
core:
  api: { host: 127.0.0.1, port: 8100, cors: [...] }
  taxonomy_file: taxonomy.yaml
  artifact_dir: artifacts/
  retention: { max_age_days: 90 }
  ingestion: { tokens: required, inbox: results_inbox/, max_upload_mb: 200 }
  locale_default: en
store:
  sqlite: { path: runcomposer.db }
sources:
  robotframework: { root: tests/ }
  # my-source: { module: mypkg.sources.MySource, ... }     # import-path plugin
runners:
  robot-pool: { max_workers: 20, history_selector: { labels: {...} } }
  ci-trigger: { base_url: ..., job_template: ... }
ui:                                  # served at /api/v1/ui-config
  examples: {...}
  quick_filters: [...]
```

Rules: plugin sections are owned and validated by the plugin; the core
validates only `core`; unknown active plugin ids fail startup loudly. Plugin
*selection* lives here (import path or entry-point name) — no env vars.

---

## 9. API & CLI surface

```
GET  /api/v1/taxonomy
POST /api/v1/selection/compile          # preview: matched items + warnings
POST /api/v1/selection/spec-preview     # render the would-be runspec
POST /api/v1/runs                       # compose (+ dispatch: {runner: id} | {mode: export})
GET  /api/v1/runs                       # list (filter: state, labels, time)
GET  /api/v1/runs/{id}                  # detail incl. dispatches + deliveries
GET  /api/v1/runs/{id}/items            # materialized selection
GET  /api/v1/runs/{id}/spec             # the exact runspec document
POST /api/v1/runs/{id}/results          # ingestion push (multipart; shard + token)
POST /api/v1/runs/{id}/finalize
GET  /api/v1/quarantine                 # unmatched deliveries; attach/promote actions
GET  /api/v1/runners                    # registry + capabilities + health (verbatim)
GET  /api/v1/health                     # core health only
GET  /api/v1/ui-config
```

Runner-specific admin actions (e.g. a pool reload) are
`POST /api/v1/runners/{id}/actions/<action>`, capability-gated — never core
endpoints.

**CLI** (adoption-critical): `runcomposer serve` · `runcomposer demo` (§12) ·
`runcomposer compile` (preview) · `runcomposer spec [--format json|yaml]` (emit a
runspec — the export workflow's compose step) · `runcomposer dispatch --runner
robot-pool spec.yaml` · `runcomposer ingest` · `runcomposer runs` / `runcomposer runs
--failed-in latest` · `runcomposer catalog` (list/snapshot the corpus) · `runcomposer
validate spec.yaml` · `runcomposer gc`. Plus the vendorable **`runcomposer-exec`**
consumer (§6.2c) shipped as its own tiny artifact.

---

## 10. UI

A React app is the product's face (the prototype's interaction model is kept:
taxonomy tree + filter-builder + auto-compiled preview table + fine
selection):

1. **i18n** — every literal in locale files; `en` + `de` shipped; locale date
   sugar lives here.
2. **Config-driven environment** — partition pickers, examples, quick filters
   come from `/api/v1/ui-config`, never hardcoded.
3. **Runner-aware compose footer** — "Run with: [robot-pool ▾] | Export spec";
   runs list shows dispatch mode + lifecycle state incl. `AWAITING_RESULTS`;
   a quarantine view for unmatched deliveries.

**Stack — decided:** current React/Vite/deps at P1; the SVAR filter widget
stays as the filter-builder (MIT, actively maintained, React-19 peer deps;
verified 2026-07-06), kept behind the existing adapter so it remains
swappable. P1 spike: SVAR built-in label localization for the i18n story;
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
appropriate plugin (`robot-pool`, `ci-trigger`) rather than in the core.
Private adopters keep their execution details in their own private
plugin and config packages that depend on this repo — nothing of theirs
appears here, in code, history, examples, or docs.

---

## 12. Demo world

`runcomposer demo` boots a neutral corpus — a fictional web-shop suite (~60
items: `Payments`, `Checkout`, `Catalog`, `Auth`; sprint/ticket/quarantine
tag patterns) via the `manifest` source, a matching taxonomy, a `demo` runner
faking executions, and **pre-seeded history at both levels** (completed runs
*and* per-item verdicts/durations) so failed-rerun selection and
duration-balanced planning are demonstrable immediately. The demo corpus
doubles as the E2E fixture. A pytest-flavored manifest example ships
alongside (§6.1).

---

## 13. Explicitly out of scope for v1

- Full authN/authZ (the per-run ingest token of §5 **is** in scope — it's the
  seam that lets real auth arrive later without redesign; everything else:
  reverse proxy).
- Scheduling/cron, cross-instance queues, HA.
- Result analytics/dashboards beyond run detail.
- Taxonomy editing UI.
- Hot-reload of config/taxonomy.

---

## 14. Phasing

- **P0 — reservations & skeleton:** publish the PyPI/npm name reservations
  (`reserve_name/`); scaffold `core/` + `plugins/`; runspec 1.0 schema +
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
- **P3 — reach:** `ci-trigger` + the thin CI-side consumer stage (named
  deliverable, §6.2b) against a real CI instance; `junit-xml` parser + pytest
  manifest example (the agnosticism proof); history-based selection provider;
  `runcomposer export --format ctrf`.
- **P4 — first private adopter:** a closed-source plugin/config package
  consuming the public core — the reference pattern for any company-internal
  adoption. Lives outside this repo by definition.

Each phase ends runnable + demoable.

---

## 15. Decision log

| # | Question | Decision (2026-07-06) |
|---|---|---|
| 1 | Name | **`runcomposer`** — verified free on PyPI + npm; placeholders in `reserve_name/` pending owner upload. |
| 2 | Warm history at adopters | **Start fresh everywhere**; no migration machinery in core; private adopters may seed their own stores. |
| 3 | UI stack | **Refresh to current React/Vite at P1; keep SVAR** behind the adapter; localization spike noted. |
| 4 | License | **MIT.** |
| 5 | `runcomposer-exec` distribution | **Both by construction**: stdlib-only single file, also pipx-installable; JSON spec input so zero deps. |
