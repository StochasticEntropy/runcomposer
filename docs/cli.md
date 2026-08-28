# The CLI

Every command runcomposer ships, every flag it accepts, and what each one
actually does. This page is the reference; for *why* the surface looks like
this see [DESIGN.md](../DESIGN.md) §9, and for how to fit it to your
environment [ADOPTING.md](../ADOPTING.md).

Two executables are installed:

| Command | What it is |
|---|---|
| `runcomposer` | The tool: compose, dispatch, ingest, browse, serve. |
| `runcomposer-exec` | The vendorable single-file consumer that *executes* a spec somewhere else. Stdlib-only, no install needed. [Below](#runcomposer-exec). |

```
runcomposer [--version] [-h] <command> [options]

  validate   check a runspec document
  demo       boot the neutral web-shop demo end-to-end
  catalog    list a manifest catalog and its snapshot hash
  compile    preview a selection
  spec       compose a run and emit its runspec
  export     export a run's results as a normalized document
  dispatch   hand a runspec to an in-process runner
  runs       list stored runs / query history
  ingest     ingest a results bundle
  gc         apply retention
  serve      run the API + UI server
```

---

## Common ground

### `--config` — the only way to point at another config file

Eight commands take it: `compile`, `spec`, `export`, `dispatch`, `runs`,
`ingest`, `gc`, `serve`. The rule is short:

```
--config PATH      # default: ./config.yaml if it exists, else built-in defaults
```

There is no `RUNCOMPOSER_CONFIG` environment variable and no config search
path — plugin selection lives in that file, and picking it up from the
environment is exactly the ambiguity [DESIGN.md](../DESIGN.md) §8 rules out. So
if you run more than one environment, `--config` is how you switch, and every
command in a script needs it:

```bash
runcomposer compile 'Payments' --config envs/staging/config.yaml
runcomposer runs    --config envs/staging/config.yaml
runcomposer serve   --config envs/staging/config.yaml
```

A missing file is refused rather than silently ignored:

```console
$ runcomposer runs --config nope.yaml
error: config file not found: nope.yaml
```

With no config file at all, runcomposer still works everywhere: the `sqlite`
store in `./runcomposer.db`, the bundled demo corpus as the `manifest` source,
the bundled demo taxonomy, and the `demo` runner.

**Two path bases, and the difference will bite you.** Paths under `core:` are
resolved relative to **the config file's directory** — `taxonomy_file`,
`artifact_dir`, `ingestion.inbox`, `ingestion.quarantine_dir` — so a config
directory is portable. Plugin options are handed to the plugin verbatim, so
`store.sqlite.path` is resolved relative to **the current working
directory**:

```console
$ runcomposer runs --config envs/staging/config.yaml
RUN ID                      STATE             RESULT  CREATED               TITLE
01M1513JZMZ3CSH51RCCE72WYN  COMPOSED          -       2026-08-28T20:31:15Z  Two picks

$ cd elsewhere
$ runcomposer runs --config ../envs/staging/config.yaml
no runs stored
$ ls
staging.db                          # a second, empty database — created right here
```

Give `store.sqlite.path` an **absolute path** in any config you invoke from
more than one directory. That is the whole workaround, and it is worth doing
before you have two databases.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | The operation ran and failed: an invalid document, an unresolvable selection, a refused dispatch, a bundle that could not be attached. |
| `2` | The command could not start: bad usage, a missing or invalid config file, an unreadable spec, an unknown plugin id, a malformed taxonomy. |

Errors go to stderr as a single `error: …` line. `dispatch` prints a refusal
from a runner as `refused: …`.

### Where the output goes

`spec` and `export` write their **document to stdout** and their commentary
(run id, warnings, dispatch id) to **stderr**, so a spec can be piped or
redirected without post-processing:

```bash
runcomposer spec 'Payments' --format json > spec.json     # clean JSON
```

With `-o`, the document goes to the file and the commentary stays on stderr.

---

## `validate`

Check that a document is a runspec this build understands. Pure reading — it
touches no store and needs no config.

```
runcomposer validate SPEC [--for-dispatch]
```

| Flag | Default | Meaning |
|---|---|---|
| `SPEC` | — | Path to the document. YAML or JSON; the format is detected. |
| `--for-dispatch` | off | Additionally require the **dispatch/export profile**: a materialized selection and a results contract ([DESIGN.md](../DESIGN.md) §3.1). |

The two levels exist because a preview spec is a legitimate document that is
simply not executable yet. Plain `validate` accepts it; `--for-dispatch` is the
check to run before you hand a document to anything:

```console
$ runcomposer validate nomat.json
nomat.json: valid runspec document

$ runcomposer validate nomat.json --for-dispatch
error: dispatch/export requires selection.materialized — the embedded item list is the authoritative executed set (DESIGN.md §3.1)
nomat.json: INVALID (1 error(s))
```

```console
$ runcomposer validate spec.json --for-dispatch
spec.json: valid runspec document (dispatch profile)
```

Exit code is `1` for an invalid document and `2` for one that could not be read
at all — worth distinguishing in a CI gate.

---

## `demo`

```
runcomposer demo
```

No flags. Boots the neutral web-shop corpus end to end and prints the whole
story: the corpus and its snapshot, the taxonomy with live item counts, a
compiled selection, a validated spec, a dispatch to the `demo` runner, and then
a *second*, history-derived run — "rerun what failed" against the history those
runs just produced, which is the one feature a fresh store cannot show on its
own.

```console
$ runcomposer demo
────────────────────────────────────────────────────────────────
runcomposer demo — fictional web-shop corpus
────────────────────────────────────────────────────────────────
Corpus: 60 items via the 'manifest' source
Catalog snapshot: sha256:734b1c4803268df2d3246b…

Taxonomy (curated tree over tag patterns — data, not code):
  Areas
    Payments      17 items   (Payments)
    Checkout      13 items   (Checkout)
…
Selection: (Payments OR prefix:Checkout- OR regex:^Cart(V2)?$) AND NOT prefix:Quarantine-
  matched 36 of 60 items
Run spec 01M1518ZC5W9HFJ1WHV2ZWG1YQ: validates against the runspec-1.0 schema (dispatch profile)
…
Dispatched to the 'demo' runner: dispatch 01M1518ZC60S4VRGAZEAQPD49F, 1 shard delivered
Results: 28 PASS, 8 FAIL  (36 items, 76.2s simulated)
…
Seeded history: 3 completed runs over 'Regression' (46 items each)
Latest completed run 01M1518ZC91C0NVYSJ0P0M12R1: 5 FAIL
Derived selection: 5 items, provenance recorded in selection.derived_from
```

Two things it does **not** do. It does not start a server — it prints and exits
`0`; for the UI run `runcomposer serve`, which with no config serves this same
demo corpus. And it does not write to a store: the runs, verdicts and history it
narrates live in memory for the length of the command, so it leaves no files
behind and the store is exactly as empty afterwards as before.

```console
$ runcomposer demo          # in an empty directory
…
$ ls -la                    # nothing created
$ runcomposer runs
no runs stored
$ runcomposer runs --failed-in latest
error: history selection 'failed@latest' matched no completed run — history features are dark on a fresh store (DESIGN.md §6.3)
```

That is worth knowing before you reach for `--failed-in` after a demo: history
features stay dark until *your* runs accrue. Compose and dispatch a couple of
real runs first ([the export loop](#the-export-loop-end-to-end)).

---

## `catalog`

List what a test source sees, and the snapshot hash that identifies it.

```
runcomposer catalog [--manifest FILE] [--limit N]
```

| Flag | Default | Meaning |
|---|---|---|
| `--manifest FILE` | the bundled demo corpus | A manifest catalog (JSON or YAML) to read. |
| `--limit N` | `0` (all) | Show at most N items. `0` means no limit. |

This command reads a **manifest file directly** and does not consult your
config — it is the quick "are my tags good enough to select on" check from
[ADOPTING.md](../ADOPTING.md) §8, usable before any config exists.

```console
$ runcomposer catalog --limit 5
# 60 items — snapshot sha256:734b1c4803268df2d3246b38c30c9fa1c675767d6bfc904aad06dfc4ece9f0a2
Shop.Payments.Cards.T001  [Payments, Payments-Cards, Smoke, Sprint-12, SHOP-1200]
Shop.Payments.Cards.T002  [Payments, Payments-Cards, Regression, Sprint-13]
Shop.Payments.Cards.T003  [Payments, Payments-Cards, Regression, Sprint-14]
Shop.Payments.Cards.T004  [Payments, Payments-Cards, Regression, Sprint-12, SHOP-1203]
Shop.Payments.Cards.T005  [Payments, Payments-Cards, Regression, Sprint-13]
# … 55 more (use --limit 0 for all)
```

The snapshot is what makes drift detectable: it is recorded into every spec and
compared before execution. A pytest-flavoured catalog reads the same way — the
ids are just node ids:

```console
$ runcomposer catalog --manifest examples/pytest-shop/manifest.json
# 7 items — snapshot sha256:980cdcc95afee2f86a56fead71730921ee1bf80dc6cfcbb62df10be3dd777d57
test_cart.py::test_add_item  [Cart, Smoke]
test_cart.py::test_remove_item  [Cart, Regression]
test_cart.py::test_checkout[visa]  [Cart, Payments, Regression]
…
```

---

## `compile`

Preview a selection. Matches items, prints warnings, stores nothing.

```
runcomposer compile [FILTER] [--id ITEM_ID ...] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `FILTER` | — | A tag filter: a pattern string, or a YAML/JSON filter AST. |
| `--id ITEM_ID` | none | An explicit item pick. Repeatable. **Intersects** with the filter. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

At least one of `FILTER` / `--id` is required:

```console
$ runcomposer compile
error: provide a filter, --id picks, and/or --from-history
```

A bare pattern is the common case:

```console
$ runcomposer compile 'Payments'
# 17 item(s) matched
Shop.Payments.Cards.T001  [Payments, Payments-Cards, Smoke, Sprint-12, SHOP-1200]
…
```

The `FILTER` argument is parsed as YAML, so the full AST fits in one shell
argument — which is how you check a boolean selection before composing it:

```console
$ runcomposer compile '{op: AND, items: [Payments, {not: prefix:Quarantine-}]}'
# 16 item(s) matched
Shop.Payments.Cards.T001  [Payments, Payments-Cards, Smoke, Sprint-12, SHOP-1200]
…
```

Seventeen items down to sixteen: one quarantined test dropped out. Remember
that a bare literal matches **case-insensitively** while `prefix:` and `regex:`
do not ([DESIGN.md](../DESIGN.md) §3.1) — a filter that matches nothing is
usually that, not a typo.

---

## `spec`

Compose a run and emit its runspec document. This is the compose step of the
export workflow, and the busiest command in the tool.

```
runcomposer spec [FILTER] [--id ITEM_ID ...] [--from-history QUERY]
                 [--title TITLE] [--label KEY=VALUE ...]
                 [--format yaml|json] [-o OUT] [--export]
                 [--runner RUNNER_ID] [--expect-format FORMAT]
                 [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `FILTER` | — | Tag filter, as in `compile`. |
| `--id ITEM_ID` | none | Explicit item pick, repeatable; intersects with the filter. |
| `--from-history QUERY` | none | History-based selection resolved at compose time, e.g. `failed@latest?suite=nightly`. |
| `--title TITLE` | `Untitled run` | The run title. |
| `--label KEY=VALUE` | none | Free-form provenance label, repeatable. Stored, never interpreted — and what scopes a later `failed@latest`. |
| `--format yaml\|json` | `yaml` | Output format of the document. |
| `-o`, `--out OUT` | stdout | Write the document to this file instead. |
| `--export` | off | Mint an **export dispatch** for the emitted bytes, moving the run to `AWAITING_RESULTS`. |
| `--runner RUNNER_ID` | none | Embed that runner's configured options as the spec's `runner` section. |
| `--expect-format FORMAT` | `runcomposer-verdicts` | The results format the run expects back — a registered `ResultParser` id. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

At least one of `FILTER` / `--id` / `--from-history` is required. Composing
always **creates a run record** in state `COMPOSED` — the id on stderr is how
you find it again.

```console
$ runcomposer spec 'Payments-Refunds' --title "Refunds smoke" --label suite=nightly
run: 01M1512MG1S3R8P1R3J3SB7EAV (state COMPOSED)
runspec: '1.0'
run:
  id: 01M1512MG1S3R8P1R3J3SB7EAV
  title: Refunds smoke
  created_at: '2026-08-28T20:30:44Z'
  labels:
    suite: nightly
selection:
  tag_filter: Payments-Refunds
  materialized:
    item_ids:
    - Shop.Payments.Refunds.T001
    - Shop.Payments.Refunds.T002
    - Shop.Payments.Refunds.T003
    - Shop.Payments.Refunds.T004
    at: '2026-08-28T20:30:44Z'
    count: 4
source:
  provider: manifest
  snapshot: sha256:734b1c4803268df2d3246b38c30c9fa1c675767d6bfc904aad06dfc4ece9f0a2
results:
  expect:
  - format: runcomposer-verdicts
  shards: 1
  deliver: none
  token: rct_FD8s54_8ihsjIWSU7qJ6vi9v53dEkfoC
```

### `--export` — freezing the document for someone else to run

`--export` mints a dispatch over the exact bytes that were written, so the
bundle that comes back can be correlated and its `spec_sha256` verified:

```console
$ runcomposer spec 'Payments' --title "Nightly payments" --label suite=nightly \
      --format json -o spec.json --export
spec written to spec.json
run: 01M1512MKQK2SKVQTN7Q42EPMB (state COMPOSED)
export dispatch: 01M1512MKWMR41W9VZF0TDZFJA (1 shard(s) declared, state AWAITING_RESULTS)
```

Without `--export` the document is just a document: composed, valid, and
nothing is waiting for it.

### `--id` — explicit picks

Repeat it. With no `FILTER`, the picks *are* the selection:

```console
$ runcomposer spec --id Shop.Payments.Cards.T001 --id Shop.Payments.Cards.T002 \
      --title "Two picks" --runner demo --expect-format robot-output-xml
run: 01M1513JZMZ3CSH51RCCE72WYN (state COMPOSED)
…
selection:
  item_ids:
  - Shop.Payments.Cards.T001
  - Shop.Payments.Cards.T002
  materialized:
    item_ids:
    - Shop.Payments.Cards.T001
    - Shop.Payments.Cards.T002
    at: '2026-08-28T20:31:15Z'
    count: 2
…
results:
  expect:
  - format: robot-output-xml
…
runner:
  demo: {}
```

Note what `--runner demo` did: it copied that runner's **configured options**
out of your config into the document's one open section. An unconfigured runner
yields an empty section, as above — the flag embeds configuration, it does not
dispatch anything.

`--expect-format` names the parser that should read the results. Leave it at
`runcomposer-verdicts` when the executor emits the reference JSON format (what
`runcomposer-exec --simulate` writes); set it to `robot-output-xml` or
`junit-xml` when the executor returns its framework's native artifacts.

### `--from-history` — rerun what failed

The query is `<verdict>@<selector>[?key=value&…]`
([DESIGN.md](../DESIGN.md) §7). The scope is the part that matters: unscoped,
"latest" means the latest completed run of *anything* in the store.

```console
$ runcomposer spec --from-history 'failed@latest?suite=nightly' \
      --title "Rerun failures" --label suite=nightly
run: 01M15139DS2XHFXZKYS11CT67D (state COMPOSED)
…
selection:
  item_ids:
  - Shop.Checkout.Gift.T002
  materialized:
    item_ids:
    - Shop.Checkout.Gift.T002
    at: '2026-08-28T20:31:05Z'
    count: 1
  derived_from:
  - provider: history
    query:
      run: LATEST
      verdicts:
      - FAIL
      labels:
        suite: nightly
    resolved_run_id: 01M15133G77CT95140WMJEYP1Z
    resolved_run_completed_at: '2026-08-28T20:30:59Z'
    resolved_run_labels:
      suite: nightly
```

The resolution happens **once, at compose time**: the emitted spec holds a
static item list plus `derived_from` provenance saying which run it came from
and why. Labelling the new run the same way (`--label suite=nightly`) is what
keeps the stream joined for the next rerun.

---

## `dispatch`

Hand an existing runspec document to an in-process runner and execute it.

```
runcomposer dispatch SPEC --runner RUNNER [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `SPEC` | — | Path to the runspec document (YAML or JSON). |
| `--runner RUNNER` | **required** | Runner plugin id: `demo`, `robot-pool`, `ci-trigger`, or your own. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

```console
$ runcomposer dispatch checkout.json --runner demo
run 01M15133G77CT95140WMJEYP1Z: dispatch 01M15133K4JQ9VJSMJTWSVY9XE via 'demo' (1 shard(s))
state: COMPLETE (FAIL) — 1 FAIL, 11 PASS, 1 SKIP
```

Two things to know before you script this:

- **The document is imported as a run** of its own. `dispatch` takes a
  *document*, not a run id.
- **It blocks for the whole execution.** In-process dispatch is synchronous
  ([DESIGN.md](../DESIGN.md) §4): a forty-minute suite means a forty-minute
  command, and the state it prints at the end is already `COMPLETE`. Runners
  whose completion is out-of-band (`ci-trigger` with `completion: callback`)
  return immediately and leave the run in `AWAITING_RESULTS`.

If the runner exposes a plan, it is printed verbatim before the summary —
`robot-pool` puts its chunk plan there, including the round-robin cold-start
note. A runner that refuses (drift, for example) exits `1` with `refused: …`.

---

## `ingest`

Take a results bundle into a run.

```
runcomposer ingest BUNDLE [--run RUN_ID] [--dispatch DISPATCH_ID]
                   [--shard SHARD] [--allow-unsolicited] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `BUNDLE` | — | Path to the bundle: a directory or a single file. |
| `--run RUN_ID` | from the bundle marker | The run to attach to. Required when the bundle has no marker. |
| `--dispatch DISPATCH_ID` | from the marker, else the latest | Which dispatch this delivery belongs to. |
| `--shard SHARD` | from the marker, else `1` | The shard label for this delivery. |
| `--allow-unsolicited` | off | Promote a bundle with no marker / an unknown run to its **own** run with `origin: ingested`. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

The normal path needs no flags at all, because the marker
(`runcomposer_run.json`) that `runcomposer-exec` wrote carries the correlation:

```console
$ runcomposer ingest results
01M1512MKQK2SKVQTN7Q42EPMB shard 1: delivery recorded (17 verdict(s))
run state: COMPLETE (PASS)
```

**Re-ingesting is safe.** Every delivery is content-hashed, so a byte-identical
bundle is a no-op — which is what makes inbox pollers, git re-pulls and CI
retries harmless:

```console
$ runcomposer ingest results
01M1512MKQK2SKVQTN7Q42EPMB shard 1: byte-identical bundle already ingested — no-op (17 verdict(s))
run state: COMPLETE (PASS)
```

A *different* bundle for the same `(run, dispatch, shard)` **replaces** that
shard's verdicts — last-writer-wins, so a correction can flip `FAIL` back to
`PASS`.

A bundle nobody asked for is refused rather than guessed at:

```console
$ runcomposer ingest stray
error: bundle has no runcomposer_run.json marker and no run id was given — unsolicited bundles are not auto-attached (DESIGN.md §4/§5); quarantine it, or promote explicitly (--allow-unsolicited)

$ runcomposer ingest stray --allow-unsolicited
unsolicited bundle promoted to run 01M1515C659531A1AGK8HVH87D (origin: ingested)
01M1515C659531A1AGK8HVH87D shard 1: delivery recorded (1 verdict(s))
run state: COMPLETE (PASS)
```

Use `--run` and `--shard` when you are correlating by hand — a fan-out that
returns one bundle per partition, for instance:

```console
$ runcomposer ingest results-env1 --run 01M151EYEVGK3128HXB6NNKWS6 --shard env1
warning: run has no dispatch — delivery attached to the run directly
01M151EYEVGK3128HXB6NNKWS6 shard env1: delivery recorded (2 verdict(s))
run state: COMPLETE (PASS)

$ runcomposer ingest results-env2 --run 01M151EYEVGK3128HXB6NNKWS6 --shard env2
warning: run has no dispatch — delivery attached to the run directly
01M151EYEVGK3128HXB6NNKWS6 shard env2: delivery recorded (2 verdict(s))
run state: COMPLETE (FAIL)
```

Two shards, two deliveries, one run — and the run is `FAIL` because `env2` is.
The warning is worth reading: this spec was composed without `--export`, so
there was no dispatch to attach the deliveries to. Compose with `--export` (or
dispatch it) and they land under that dispatch instead.

---

## `runs`

List stored runs — or ask the store which items failed.

```
runcomposer runs [--state STATE] [--label KEY=VALUE ...]
                 [--since ISO] [--until ISO] [--limit N]
                 [--failed-in SELECTOR] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `--state STATE` | all | Lifecycle state: `COMPOSED`, `DISPATCHED`, `RUNNING`, `AWAITING_RESULTS`, `COMPLETE`. |
| `--label KEY=VALUE` | none | Only runs carrying this label. Repeatable; **all** must match. |
| `--since ISO` | none | Only runs created at/after this ISO-8601 UTC time. |
| `--until ISO` | none | Only runs created at/before this ISO-8601 UTC time. |
| `--limit N` | `20` | Maximum rows. |
| `--failed-in SELECTOR` | — | Switch modes: print the **item ids** that FAILED in the selected run. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

```console
$ runcomposer runs --state COMPLETE --label suite=nightly
RUN ID                      STATE             RESULT  CREATED               TITLE
01M15133G77CT95140WMJEYP1Z  COMPLETE          FAIL    2026-08-28T20:30:59Z  Checkout regression
01M1512MKQK2SKVQTN7Q42EPMB  COMPLETE          PASS    2026-08-28T20:30:44Z  Nightly payments
```

`STATE` is the lifecycle state; `RESULT` is the computed completion of a
finished run. Filters combine, and the window is on **creation** time:

```console
$ runcomposer runs --since 2026-08-28T00:00:00Z --until 2026-08-29T00:00:00Z --limit 2
RUN ID                      STATE             RESULT  CREATED               TITLE
01M15139DS2XHFXZKYS11CT67D  COMPOSED          -       2026-08-28T20:31:05Z  Rerun failures
01M15133G77CT95140WMJEYP1Z  COMPLETE          FAIL    2026-08-28T20:30:59Z  Checkout regression
```

### `--failed-in` — the id list, for piping

`--failed-in` changes what the command prints: a `#` header naming the run it
resolved to, then one item id per line. The selector is `latest`, `run:<id>`,
or `before:<ISO time>`, each optionally scoped:

```console
$ runcomposer runs --failed-in latest --label suite=nightly
# 1 item(s) FAILED in run 01M15133G77CT95140WMJEYP1Z (scope: {'suite': 'nightly'})
Shop.Checkout.Gift.T002
```

So piping means dropping the header:

```console
$ runcomposer runs --failed-in 'latest?suite=nightly' | grep -v '^#'
Shop.Payments.Cards.T002
```

The scope may be written either way — `--label suite=nightly`, or inline in the
selector as above. **Scope it.** Unscoped, `latest` is the
latest completed run of anything in the store, so on a shared instance
somebody's ad-hoc five-test selection silently becomes the reference for your
nightly rerun. A scope on `run:<id>` is refused rather than ignored, since it
could only mislead.

To *compose* from that answer rather than read it, use
[`spec --from-history`](#--from-history--rerun-what-failed) — it records
provenance, which this listing does not.

---

## `export`

Emit a run's results as a normalized, cross-tool document.

```
runcomposer export RUN_ID --format ctrf [-o OUT] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `RUN_ID` | — | The run to export. |
| `--format FORMAT` | **required** | Currently `ctrf` is the only supported value. |
| `-o`, `--out OUT` | stdout | Write to this file instead. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

```console
$ runcomposer export 01M15133G77CT95140WMJEYP1Z --format ctrf
{
  "results": {
    "tool": {
      "name": "runcomposer"
    },
    "summary": {
      "tests": 13,
      "passed": 11,
      "failed": 1,
      "skipped": 1,
      "pending": 0,
      "other": 0,
      "start": 1787949059000,
      "stop": 1787949059000
    },
    "tests": [
      {
        "name": "Shop.Checkout.Express.T001",
        "status": "passed",
        "duration": 3277
      },
…
```

Anything else is refused up front, with the supported list in the message:

```console
$ runcomposer export 01M15133G77CT95140WMJEYP1Z --format junit
error: unknown export format 'junit' (supported: ctrf)
```

Note the direction: `export` reads results *out* of runcomposer.
`spec --export` is an unrelated thing — it freezes a spec for an external
executor.

---

## `gc`

Apply retention. Safe to run from cron; safe to run twice.

```
runcomposer gc [--config PATH]
```

No flags of its own — the policy lives in `core.retention` and
`core.ingestion` ([DESIGN.md](../DESIGN.md) §6.4), and `gc` is what enforces
it. It reports each of the four things it bounds:

```console
$ runcomposer gc
quarantine: removed 0 entr(y/ies) beyond the configured bound
inbox/processed: removed 0 expired bundle(s)
artifacts: removed 0 expired file(s)
store: pruned 0 run(s) past retention
```

`gc` expires artifact **files** as well as run records, so a reference whose
file it removed answers `404` with a message naming retention as the likely
cause. `max_age_days` defaults to `90`.

---

## `serve`

Run the API and the bundled web UI.

```
runcomposer serve [--host HOST] [--port PORT] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `--host HOST` | `core.api.host`, else `127.0.0.1` | Bind address. |
| `--port PORT` | `core.api.port`, else `8100` | Bind port. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

The flags **override** the config, which is what makes one config file usable
for two processes:

```console
$ runcomposer serve --config envs/staging/config.yaml
INFO:     Uvicorn running on http://127.0.0.1:8200 (Press CTRL+C to quit)

$ runcomposer serve --config envs/staging/config.yaml --port 8201
INFO:     Uvicorn running on http://127.0.0.1:8201 (Press CTRL+C to quit)
```

Startup is **strict on purpose**: the config is loaded, every configured plugin
id is resolved, and the taxonomy is validated before the socket opens. A
malformed taxonomy or an unknown plugin id exits `2` with a message naming the
problem, rather than booting a server that serves an empty tree.

With no config at all, `serve` comes up on the demo corpus, so the UI is never
empty on a fresh install:

```console
$ curl -s localhost:8100/api/v1/health
{"status":"ok","version":"0.1.0"}
$ curl -s localhost:8100/api/v1/runners
[{"id":"demo","capabilities":["fake-execution"]}]
```

`--host 0.0.0.0` is what you need when something outside the machine has to
reach the callback URL — a CI container posting results back, for instance.
There is no authentication in front of the API beyond the per-run ingest token
([DESIGN.md](../DESIGN.md) §13); put a reverse proxy in front of anything
public.

---

## `runcomposer-exec`

The other executable: a **single stdlib-only file** that executes a spec
wherever your tests actually live. Vendorable by copying
`src/runcomposer_exec.py` — no install, no venv, no network
([DESIGN.md](../DESIGN.md) §6.2c).

```
runcomposer-exec SPEC (--simulate | --command TEMPLATE)
                 [--out DIR] [--dispatch ID] [--shard LABEL] [--seed SEED]
```

| Flag | Default | Meaning |
|---|---|---|
| `SPEC` | — | The run spec. **JSON always**; YAML only if a YAML parser happens to be importable there. |
| `--simulate` | — | Fake execution. Deterministic per `--seed`. Mutually exclusive with `--command`; one of the two is required. |
| `--command TEMPLATE` | — | A shell command to run. Placeholders: `{ids_file}`, `{run_id}`, `{out_dir}`. |
| `--out DIR` | `runcomposer_results` | Output directory for the results and the marker. |
| `--dispatch ID` | none | Dispatch id to record in the marker. |
| `--shard LABEL` | none | Shard label to record in the marker. |
| `--seed SEED` | `exec` | Seed for `--simulate`. |

It executes exactly `selection.materialized.item_ids` and refuses a spec
without one; it refuses any `runspec` MAJOR other than `1`. Either way it
writes `runcomposer_run.json` next to the outputs, carrying `run_id` and
`spec_sha256` so the bundle can find its way home.

**`--simulate` runs no tests.** It is the round-trip proof — it shows that
compose → transport → ingest works before your test framework is involved at
all:

```console
$ runcomposer-exec spec.json --out results --simulate
runcomposer-exec: simulated 17 items (16 PASS, 1 SKIP)
runcomposer-exec: bundle ready at results (marker: runcomposer_run.json)
```

**`--command` is the real thing.** Your script receives a file of item ids and
an output directory, and writes its native result files there:

```console
$ runcomposer-exec spec.json --out cmd-results --command "./run-tests.sh {ids_file} {out_dir}"
runcomposer-exec: running: ./run-tests.sh cmd-results/item_ids.txt cmd-results
ran 17 test(s)
runcomposer-exec: bundle ready at cmd-results (marker: runcomposer_run.json)

$ ls cmd-results
item_ids.txt  results.json  runcomposer_run.json

$ cat cmd-results/runcomposer_run.json
{
  "run_id": "01M1512MKQK2SKVQTN7Q42EPMB",
  "spec_sha256": "d39e1f3e2092536893ed2c2ab1d93484b3e942851c37554f18581729792f91b8"
}
```

The exit code is your command's exit code, so a CI stage fails when the tests
did. Whatever format your command wrote, name it with
`spec --expect-format` when you compose, so ingestion picks the right parser.

---

## The export loop, end to end

The five commands in the order they are actually used
([ADOPTING.md](../ADOPTING.md) §6 has the reasoning):

```bash
# 1 — here: look before you compose
runcomposer compile 'Payments' --config envs/staging/config.yaml

# 2 — here: freeze the plan and mint an export dispatch
runcomposer spec 'Payments' --title "Nightly payments" --label suite=nightly \
    --format json -o spec.json --export --config envs/staging/config.yaml

# 3 — there: one vendored file, on the machine that has the tests
python3 runcomposer_exec.py spec.json --out results \
    --command "./run-tests.sh {ids_file} {out_dir}"

# 4 — here: however the bundle travelled back
runcomposer ingest results --config envs/staging/config.yaml

# 5 — here: read it
runcomposer runs --label suite=nightly --config envs/staging/config.yaml
runcomposer export <run-id> --format ctrf --config envs/staging/config.yaml
```
