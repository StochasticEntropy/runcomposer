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

`python -m runcomposer …` is the same command line as `runcomposer …`, exit
codes included — for an interpreter whose scripts directory is not on the
`PATH` (`tests/test_python_m.py`).

```
runcomposer [--version] [-v] [-h] <command> [options]

  looking and composing
    validate        check a runspec document
    catalog         list the catalog, its tags and its snapshot hash
    taxonomy-check  compare the taxonomy with the catalog
    compile         preview a selection
    spec            compose a run and emit its runspec

  running
    run             compose, dispatch to a runner, wait, summarise
    plan            show what a dispatch would do, without doing it
    dispatch        dispatch a runspec file or a stored run to a runner
    rerun           run a stored run again — all of it, or what failed
    cancel          cancel a run's dispatch in flight

  reading
    watch           follow a run's events until it is over
    show            one run in full: dispatches, shards, artifacts
    runs            list stored runs / query history
    export          export a run's results as a normalized document

  results and housekeeping
    export-spec     hand a stored run's document out for execution elsewhere
    ingest          ingest a results bundle
    quarantine      list, attach, promote or discard quarantined deliveries
    finalize        close a run as it stands
    delete          delete a run, its results and its artifacts
    gc              apply retention, expire dispatches that wait too long
    history         seed and read the duration history plans balance with

  operating
    demo            boot the neutral web-shop demo end-to-end
    doctor          check a deployment
    serve           run the API + UI server
```

The samples on this page were produced by running the commands shown, against
the bundled web-shop corpus and the example Robot Framework suite
(`examples/robot-shop`). Run ids, timestamps and tokens are those of the
sessions they were taken in; yours will differ.

---

## Common ground

### `--config` — the only way to point at another config file

Every command takes it except `validate` (which reads one document and
nothing else) and `demo` (which writes its own). The rule is short:

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

A missing file is refused rather than silently ignored, and so is a section
the file should not have:

```console
$ runcomposer runs --config nope.yaml
error: config file not found: nope.yaml

$ runcomposer serve --config envs/broken/config.yaml
error: envs/broken/config.yaml: unknown top-level section(s) ['shop_alerts'] — the sections are ['core', 'store', 'sources', 'runners', 'parsers', 'observers', 'ui', 'connectors']. Settings of your own connector package belong under 'connectors:' (connectors.<name>: …), which runcomposer never interprets
```

With no config file at all, runcomposer still works everywhere: the `sqlite`
store in `./runcomposer.db`, the bundled demo corpus and taxonomy, and the
`demo` runner.

**Only what is configured under `runners:` can be dispatched to.** The key of
an entry is the runner id every `--runner` flag takes; a config *file* that
has no `runners:` section has no dispatchable runner at all (it can still
compose, export and ingest). The config used for most samples on this page:

```yaml
# envs/staging/config.yaml
core:
  api: { port: 8200 }
store:
  sqlite: { path: runcomposer.db }
runners:
  demo: {}
  demo-slow: { plugin: demo, delay_s: 1 }    # the same plugin, a second time
```

```console
$ runcomposer run Checkout --runner robot-pool --config envs/staging/config.yaml
error: no runner is configured under the id 'robot-pool' (configured: ['demo', 'demo-slow'])
```

**One path base.** Every relative path in a config file is resolved against
**the config file's own directory** — the `core:` keys (`taxonomy_file`,
`artifact_dir`, `ingestion.inbox`, `ingestion.quarantine_dir`) and the plugin
sections (`store.sqlite.path`, `sources.robotframework.root`,
`runners.robot-pool.suite_root`) alike. The config file's own path is made
absolute when it is loaded, so every anchored path is absolute — `doctor`
prints them so — and a worker process or a later change of directory
finds the same files (`tests/test_config_paths_absolute.py`). A config
directory is therefore portable, and the same `--config` behaves
identically from anywhere:

```console
$ runcomposer runs --config envs/staging/config.yaml --limit 2
RUN ID                      STATE             RESULT      MODE          CREATED               TITLE
01M7X3BB905RSZHZ3HGKPSX63Y  COMPOSED          -           -             2026-11-20T18:26:24Z  Two picks
01M7X3BAYWFJ8T23DSVVT3NNV8  AWAITING_RESULTS  -           export        2026-11-20T18:26:23Z  Nightly checkout
# 2 of 4 run(s) — page with --limit / --offset

$ cd elsewhere
$ runcomposer runs --config ../envs/staging/config.yaml --limit 2
RUN ID                      STATE             RESULT      MODE          CREATED               TITLE
01M7X3BB905RSZHZ3HGKPSX63Y  COMPOSED          -           -             2026-11-20T18:26:24Z  Two picks
01M7X3BAYWFJ8T23DSVVT3NNV8  AWAITING_RESULTS  -           export        2026-11-20T18:26:23Z  Nightly checkout
# 2 of 4 run(s) — page with --limit / --offset
$ ls                                # no second database created here
```

Absolute paths are used exactly as written.

### Exit codes

The same four, for every command:

| Code | Meaning |
|---|---|
| `0` | Done. For a command that waits on a run: the run is `COMPLETE (PASS)`. |
| `1` | The answer is "no": the run completed but is not `PASS`, a spec is invalid, a selection cannot be compiled, a bundle was refused, a named run or runner does not exist, a check found drift or problems. |
| `2` | The command line or the configuration is wrong: bad usage, a missing or invalid config file, an unknown plugin or plugin option, a malformed taxonomy, an unreadable spec file. |
| `3` | The run ended without a result: the dispatch was refused, failed, was cancelled or went stale, or the wait timed out. |

So a CI stage can tell "tests failed" (`1`) from "nothing was tested" (`3`)
from "the pipeline is misconfigured" (`2`). `Ctrl-C` exits `130`.

Errors go to stderr as a single `error: …` line. A runner's refusal is printed
as `refused: …`.

### Where the output goes

`spec`, `export-spec` and `export` write their **document to stdout** and
their commentary (run id, warnings, dispatch id, token) to **stderr**, so a
document can be piped or redirected without post-processing:

```bash
runcomposer spec 'Payments' --format json > spec.json     # clean JSON
```

With `-o`, the document goes to the file and the commentary stays on stderr.

### `--json` — one document for a script

`catalog`, `compile`, `run`, `plan`, `dispatch`, `rerun`, `cancel`, `show`,
`runs`, `finalize`, `ingest`, `quarantine list|attach|promote`, `gc`,
`history import|durations` and `doctor` take `--json`: instead of text they print **one JSON document on stdout** — the same
shapes the HTTP API returns ([DESIGN.md](../DESIGN.md) §9). `watch --json`
prints one JSON object per event, one per line. Exit codes are unchanged.

```console
$ runcomposer ingest results --json --config envs/staging/config.yaml
{
  "run_id": "01M7X3BA2EBNAEW1XZXKWXS8JD",
  "dispatch_id": "01M7X3BA2M33C4BNM7B6BFMKJ2",
  "shard": "1",
  "outcome": "duplicate",
  "verdict_count": 17,
  "run_state": "COMPLETE",
  "completion": "PASS",
  "warnings": []
}
```

### `-v`, `--verbose` — what runcomposer is doing

A global flag, given *before* the command. It turns on the `runcomposer.*`
loggers at `INFO` on stderr — every event of a run as it is recorded:

```console
$ runcomposer -v run Payments-Wallets --runner demo --title "Wallets" --config envs/staging/config.yaml
INFO runcomposer.events: run.composed run=01M7X3KTG1NNP0BWEPPCY4CXG1 dispatch=- 
INFO runcomposer.events: run.dispatched run=01M7X3KTG1NNP0BWEPPCY4CXG1 dispatch=01M7X3KTG23HRFEWR3H7XDTW1E state=DISPATCHED
INFO runcomposer.events: run.running run=01M7X3KTG1NNP0BWEPPCY4CXG1 dispatch=01M7X3KTG23HRFEWR3H7XDTW1E state=RUNNING
INFO runcomposer.events: delivery.recorded run=01M7X3KTG1NNP0BWEPPCY4CXG1 dispatch=01M7X3KTG23HRFEWR3H7XDTW1E shard=1 outcome=new
INFO runcomposer.events: run.completed run=01M7X3KTG1NNP0BWEPPCY4CXG1 dispatch=01M7X3KTG23HRFEWR3H7XDTW1E state=COMPLETE completion=FAIL
demo runner: simulating 4 item(s) with seed 'demo'
…
```

`serve` has its own [`--log-level`](#serve) instead. For `doctor` the
global flag also does what its own `-v` does: print every runner's health
data ([below](#doctor)).

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
error: dispatch/export requires a results section (DESIGN.md §3.1)
nomat.json: INVALID (2 error(s))
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
runcomposer demo [--workspace DIR]
```

Boots the neutral web-shop corpus end to end and prints the whole story: the
corpus and its snapshot, the taxonomy with live item counts, a compiled
selection, a validated spec, a dispatch to the `demo` runner, and then a
*second*, history-derived run — "rerun what failed" against the history those
runs just produced, which is the one feature a fresh store cannot show on its
own.

The `demo` runner **simulates** execution: no test is run, verdicts are
derived from a seed. The demo says so wherever it reports a result.

| Flag | Default | Meaning |
|---|---|---|
| `--workspace DIR` | `./runcomposer-demo` | Where to seed the demo's `config.yaml` and store. Must be empty, absent, or a previous demo workspace. |

```console
$ runcomposer demo
────────────────────────────────────────────────────────────────
runcomposer demo — fictional web-shop corpus
────────────────────────────────────────────────────────────────
Corpus: 60 items via the 'manifest' source
Catalog snapshot: sha256:ee00db96d49d82f4564bfb…
Workspace: runcomposer-demo

Taxonomy (curated tree over tag patterns — data, not code):
  Areas
    Payments      17 items   (Payments)
    Checkout      13 items   (Checkout)
…
Selection: (Payments OR prefix:Checkout- OR "regex:^Cart(V2)?$") AND NOT prefix:Quarantine-
  matched 36 of 60 items
Run spec 01M7X3ATV98NR9RMVVWWHC2TWX: validates against the runspec-1.0 schema (dispatch profile)
…
Dispatched to the 'demo' runner (simulated): dispatch 01M7X3ATVGPE63E5T2HYS4S2WH, 1 shard delivered — run COMPLETE (FAIL)
Results: 28 PASS, 8 FAIL  (36 items, 76.2s simulated)
…
Seeded history: 3 completed runs over 'Regression' (46 items each), persisted in runcomposer-demo/runcomposer.db
Latest completed 'suite=nightly' run 01M7X3ATVS9W2EPX3DFNGPV9C0: 5 FAIL
Derived selection: 5 items, provenance recorded in selection.derived_from (run 01M7X3ATVS9W2EPX3DFNGPV9C0, scope {'suite': 'nightly'})
Results: 3 PASS, 2 FAIL  (5 items, 9.3s simulated)
…
────────────────────────────────────────────────────────────────
Demo complete. 5 completed runs and 179 verdicts are in runcomposer-demo/runcomposer.db — history features are live there.
Delete the workspace to undo all of it:  rm -rf runcomposer-demo

Next steps:
  runcomposer runs --config runcomposer-demo/config.yaml
  runcomposer runs --config runcomposer-demo/config.yaml --failed-in latest --label suite=nightly
  runcomposer compile --config runcomposer-demo/config.yaml Payments
  runcomposer export 01M7X3ATVS9W2EPX3DFNGPV9C0 --format ctrf --config runcomposer-demo/config.yaml
  runcomposer run Checkout --runner demo --config runcomposer-demo/config.yaml
  runcomposer serve --config runcomposer-demo/config.yaml
```

Those next steps are meant to be pasted, and they work — the history the demo
narrated is in the store it printed:

```console
$ runcomposer runs --config runcomposer-demo/config.yaml --failed-in latest --label suite=nightly
# 5 item(s) FAILED in run 01M7X3ATVS9W2EPX3DFNGPV9C0 (scope: {'suite': 'nightly'})
Shop.Payments.Cards.T003
Shop.Cart.Core.T004
Shop.Cart.Core.T005
Shop.Catalog.Search.T002
Shop.Auth.Login.T002
```

(The `run Checkout` step exits `1`: the simulated run contains a failure, and
[`run`](#run) exits `0` only for `PASS`.)

**What it writes, and what it will not.** Everything lands in the workspace
directory: a generated `config.yaml` — which configures the `demo` runner
explicitly — and the `runcomposer.db` it points at. Nothing else on the
machine is touched — in particular *not* `./runcomposer.db`, which is the
zero-config default store, and which a demo writing there would leave full of
`Shop.…` runs for your first real command to trip over. Deleting the directory
undoes the demo completely, and re-running it re-seeds from scratch rather
than piling more runs on.

If the target is somebody's actual config directory, the demo refuses instead
of overwriting it (exit `2`):

```console
$ runcomposer demo --workspace envs/staging
error: envs/staging/config.yaml exists and was not written by `runcomposer demo` — refusing to overwrite it or delete the store it points at. Pass --workspace DIR to seed the demo somewhere else.
```

One thing it does **not** do: start a server. It prints and exits `0`; for the
UI, the last next step (`runcomposer serve --config runcomposer-demo/config.yaml`)
serves the seeded workspace, so the run list is populated from the first page
load.

Your *own* store still starts cold, as it must: `--failed-in` and
duration-balanced planning stay dark there until your runs accrue.

```console
$ runcomposer runs --failed-in latest        # a fresh store of your own
error: history selection 'failed@latest' matched no completed run — history features are dark on a fresh store (DESIGN.md §6.3)
```

---

## `catalog`

List what the configured sources see, their tag world, and the snapshot hash
that identifies it.

```
runcomposer catalog [--manifest FILE] [--tags] [--limit N] [--json] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `--manifest FILE` | — | Read this manifest catalog (JSON or YAML) instead of the configured sources — through the deployment's own `sources.manifest` entry when it has one (its `plugin:` / `module:` and options apply, and a typo in them is refused with exit `2`), else the plugin installed as `manifest`. A relative `FILE` is read from the working directory. |
| `--tags` | off | List every tag with the number of items carrying it, instead of the items. |
| `--limit N` | `0` (all) | Show at most N items — or, with `--tags`, the N most common tags. `0` means no limit. |
| `--json` | off | [One JSON document](#--json--one-document-for-a-script): counts, snapshot, duplicate ids, entries. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

Without `--manifest` this lists **the catalog your config configures** — all
`sources:` entries merged into one — because "which tests can I select, and by
which tags" is a question about the deployment. With no config file it is the
bundled demo corpus, so the command works before any config exists — the quick
"are my tags good enough to select on" check from
[ADOPTING.md](../ADOPTING.md) §8.

```console
$ runcomposer catalog --limit 5
# 60 items, 47 distinct tags — snapshot sha256:ee00db96d49d82f4564bfb894e0de60a363672643b0a82d9afcb6298d67fe79f
Shop.Payments.Cards.T001  [Payments, Payments-Cards, Smoke, Sprint-12, SHOP-1200]
Shop.Payments.Cards.T002  [Payments, Payments-Cards, Regression, Sprint-13]
Shop.Payments.Cards.T003  [Payments, Payments-Cards, Regression, Sprint-14]
Shop.Payments.Cards.T004  [Payments, Payments-Cards, Regression, Sprint-12, SHOP-1203]
Shop.Payments.Cards.T005  [Payments, Payments-Cards, Regression, Sprint-13]
# … 55 more (use --limit 0 for all)
```

`--tags` answers the question you have *before* you can write a filter — what
is there to filter on:

```console
$ runcomposer catalog --tags --limit 5
# 60 items, 47 distinct tags — snapshot sha256:ee00db96d49d82f4564bfb894e0de60a363672643b0a82d9afcb6298d67fe79f
    46  Regression
    20  Sprint-12
    20  Sprint-13
    20  Sprint-14
    17  Payments
# … 42 more (use --limit 0 for all)
```

If two items in the catalog share one id — within a source or across two — the
header says so and names them. A selection cannot tell such items apart, and
neither can the results that come back; it is worth knowing before you compose
against them.

The snapshot is what makes drift detectable: it is recorded into every spec and
compared before execution. It covers each item's id and tags — what a selection
can depend on — so renaming a test is not drift, and retagging one is. A
pytest-flavoured catalog reads the same way — the ids are just node ids:

```console
$ runcomposer catalog --manifest examples/pytest-shop/manifest.json
# 7 items, 5 distinct tags — snapshot sha256:f8c5f98376284492993575b9f03a5e36ecd01ded5ebb949e471d40c0193f9f7f
test_cart.py::test_add_item  [Cart, Smoke]
test_cart.py::test_remove_item  [Cart, Regression]
test_cart.py::test_checkout[visa]  [Cart, Payments, Regression]
…
```

---

## `taxonomy-check`

Hold the taxonomy against the catalog, in both directions.

```
runcomposer taxonomy-check [--warn-only] [--limit N] [--tree] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `--warn-only` | off | Report and exit `0`. Without it, exit `1` when either side has drifted. |
| `--limit N` | `0` (all) | Show at most N entries per section (with `--tree`: N nodes). |
| `--tree` | off | Print the **resolved** tree — the one the UI renders — instead of the drift report. Always exits `0`. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

The taxonomy is hand-written and the catalog moves underneath it, so the two
drift apart silently. A newly introduced tag has no home in the tree and is
invisible to anyone browsing it; a leaf whose tag was renamed stays clickable
and selects nothing. Neither shows up anywhere — the tree renders, the filter
parses, the answer is just empty.

```console
$ runcomposer taxonomy-check --limit 3
# 11 taxonomy leaf pattern(s) over 47 distinct catalog tag(s)

tags no leaf claims (invisible in the tree): 34
  Auth-Login
  Auth-SSO
  Auth-Signup
  … 31 more (use --limit 0 for all)
every taxonomy leaf matches at least one tag

resolved tree (GET /api/v1/taxonomy?resolve=true, and what the UI renders):
  14 written node(s) → 53, 0 collapsed as matching nothing
  47 of 47 tag(s) selectable on their own; 34 reachable only under the unassigned node
  see the tree itself with --tree
```

Worth running in CI over the config you ship: a red build is a cheaper way to
learn that a rename orphaned half the tree than a colleague reporting that a
filter "does nothing". [`doctor`](#doctor) includes the same counts in its
report. Each leaf is read as filter text, as the resolved tree reads it: the
demo's quoted leaf `'"regex:^Cart(V2)?$"'` claims `Cart` and `CartV2`
([docs/taxonomy.md](taxonomy.md#the-pattern-in-a-leaf)).

The last block is the same file read the other way round: not "does the tree
still fit the catalog" but "what does the tree become once its patterns are
resolved against it" ([docs/taxonomy.md](taxonomy.md#resolution-the-tree-the-ui-renders)).
Those 34 unclaimed tags are still *reachable* — resolution gathers them under
one synthetic node — which is why they are worth fixing but not urgent.

### `--tree` — read the resolved tree

```console
$ runcomposer taxonomy-check --tree --limit 14
Areas  (60 item(s), 6 tag(s))
  Payments  Payments  (17 item(s), 1 tag(s))
  Checkout  Checkout  (13 item(s), 1 tag(s))
  Cart  regex:^Cart(V2)?$  (9 item(s), 2 tag(s))
    Cart  Cart  (5 item(s), 1 tag(s))
    CartV2  CartV2  (4 item(s), 1 tag(s))
  Catalog  Catalog  (9 item(s), 1 tag(s))
  Auth  Auth  (12 item(s), 1 tag(s))
Suites  (60 item(s), 2 tag(s))
  Smoke  Smoke  (14 item(s), 1 tag(s))
  Regression  Regression  (46 item(s), 1 tag(s))
Sprints  (60 item(s), 3 tag(s))
  Sprint 12  Sprint-12  (20 item(s), 1 tag(s))
  Sprint 13  Sprint-13  (20 item(s), 1 tag(s))
… 39 more (use --limit 0 for all)

# 14 written node(s) → 53; 47 of 47 tag(s) selectable on their own; 60 of 60 item(s) reachable
```

`Cart` is the whole point: one written leaf carrying `regex:^Cart(V2)?$`, and
under it the two tags it actually covers, each selectable on its own. This is
the answer to "why does that node not show what I expected" — it prints the
tree the browser gets, with the pattern and the counts next to every row.

---

## `compile`

Preview a selection. Matches items, prints warnings, stores nothing.

```
runcomposer compile [FILTER] [--id ITEM_ID ...] [--from-history QUERY]
                    [--json] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `FILTER` | — | A tag filter: filter text (`'Auth OR Cart'`, [below](#filter-text)), or a YAML/JSON filter AST. |
| `--id ITEM_ID` | none | An explicit item pick. Repeatable. **Intersects** with the filter. |
| `--from-history QUERY` | none | A history selection, e.g. `failed@latest?suite=nightly` ([below](#--from-history--rerun-what-failed)). |
| `--json` | off | [One JSON document](#--json--one-document-for-a-script): `count`, `total` (the catalog's size), `items`, `warnings`, `derived_from`, `snapshot`. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

At least one of `FILTER` / `--id` / `--from-history` is required:

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

### Filter text

A boolean selection is written the way the UI's text field shows it — the
one-line text form of the filter ([DESIGN.md](../DESIGN.md) §3.1):

```console
$ runcomposer compile 'Auth OR Cart'
# 17 item(s) matched
Shop.Cart.Core.T001  [Cart, Cart-Core, Smoke, Sprint-12, SHOP-1230]
…
$ runcomposer compile 'Payments AND NOT (Payments-Cards OR prefix:Quarantine-)'
# 11 item(s) matched
Shop.Payments.Wallets.T001  [Payments, Payments-Wallets, Smoke, Sprint-12, SHOP-1206]
…
```

- The operators are `AND`, `OR` and `NOT`, in upper case — a tag spelled
  `and` is just a tag. `NOT` binds tighter than `AND`, and `AND` tighter
  than `OR`: `Auth OR Cart AND Smoke` is `Auth OR (Cart AND Smoke)`.
  Parentheses group.
- A pattern is a literal tag, `prefix:…` or `regex:…`, written bare as long
  as it reads back as one pattern. A pattern that contains whitespace, a
  parenthesis, a double quote or a backslash, or that is one of the operator
  words, is written in double quotes; inside them a backslash takes the next
  character literally (`\"`, `\\`). A colon needs nothing: `Checkout:Gift`.

  ```console
  $ runcomposer compile '"Payments-Cards" OR "regex:^Cart(V2)?$"'
  # 15 item(s) matched
  ```
- `AND (x)` and `OR (x)` are a group of a single item — the one filter shape
  that infix notation cannot write otherwise; you will only meet it in the
  text of a stored filter that has one.
- `*` is every item of the catalog. The run spec has no node of its own
  for that: `*` is stored as `{"not": "regex:^$"}` — NOT of a pattern no tag
  matches, true of every item — and that node is written `*` wherever a
  filter is shown as text. Only the bare word is: `Cart*` is a pattern, and
  `'"*"'` is a tag literally named `*`.

  ```console
  $ runcomposer compile '*'
  # 60 item(s) matched
  $ runcomposer compile '* AND NOT Regression'
  # 14 item(s) matched
  ```

Text that cannot be read is a mistake on the command line: exit `2`, with
the column where reading stopped and what was expected there.

```console
$ runcomposer compile 'Payments AND (Smoke'
error: cannot read the filter text at column 14: the '(' opened here is not closed — expected AND, OR or ')'
  Payments AND (Smoke
               ^
```

There is one grammar and one answer, here, in the API and in the web UI's
text editor alike: a single tag or `prefix:`/`regex:` pattern is one word
of the grammar and reads as itself, and a string that does not parse is
refused — whatever it might have meant as one pattern. Quote a pattern
that holds whitespace, a parenthesis or a quote:

```console
$ runcomposer compile 'regex:^Cart(V2)?$'
error: cannot read the filter text at column 1: the pattern 'regex:^Cart(V2)?$' runs into '(' — a pattern containing a parenthesis or a quote is written in double quotes, e.g. "regex:^Cart(V2)?$"
  regex:^Cart(V2)?$
  ^
$ runcomposer compile '"regex:^Cart(V2)?$"'
# 9 item(s) matched
```

`Payments Smoke`, `Payments and Smoke` (`and` is a tag, not the operator),
`(Payments` and `Payments)` are refused the same way, with the column. So
are an empty or blank filter (`empty` — leave the filter out to select
without one) and a `prefix:` or `regex:` with nothing after it (`empty`,
at that word). Over the API the same strings are a `400 filter-syntax`
whose body carries `problem`, `position` (0-based, in characters) and,
where there is one, `token`
(`tests/test_filter_text.py::test_a_filter_string_reads_as_the_shared_cases_say`).

A tag that is itself spelled like an expression — a tag literally named
`Auth OR Cart` — is the one case that reads differently now: unquoted it is
the union of two tags, so write it as `'"Auth OR Cart"'`.

The same reading applies wherever a filter is configured or handed over as
one string: the `filter` of `ui.quick_filters`, `ui.presets` and
`ui.default_filter` (a string that does not parse refuses startup, naming
the entry), a taxonomy leaf's `filter` (which must read as one pattern —
quote a tag spelled like an expression or holding a space), and the
testing kit's
`FakeDispatchContext.from_source(source, selection="Auth OR Cart")`. The SDK
exports the text form: `parse_filter_text`, `read_filter_string`,
`quote_pattern` and `FilterSyntaxError`.

### A filter as a YAML/JSON AST

A `FILTER` that starts with `{` or `[`, or is a YAML mapping of a filter node
(`not: …`, `op: …` with `items: …`), is the AST itself, so the stored form
fits in one shell argument too:

```console
$ runcomposer compile '{op: AND, items: [Payments, {not: prefix:Quarantine-}]}'
# 16 item(s) matched
Shop.Payments.Cards.T001  [Payments, Payments-Cards, Smoke, Sprint-12, SHOP-1200]
…
```

Seventeen items down to sixteen: one quarantined test dropped out. Whichever
way the filter is written, the run spec stores the AST — never the text.
Remember that a bare literal matches **case-insensitively** while `prefix:`
and `regex:` do not ([DESIGN.md](../DESIGN.md) §3.1) — a filter that matches
nothing is usually that, not a typo. A filter whose pattern is not a valid
pattern exits `1`:

```console
$ runcomposer compile 'regex:['
error: tag_filter: invalid regular expression 'regex:[': unterminated character set at position 0
```

With `--from-history` the preview names the run the selection resolved to:

```console
$ runcomposer compile --from-history 'failed@latest?suite=nightly'
# from history: run 01M7X3BQGZF71M0YWCBJ9W0EJD
# 1 item(s) matched
Shop.Checkout.Gift.T002  [Checkout, Checkout-Gift, Regression, Sprint-13]
```

---

## `spec`

Compose a run and emit its runspec document. This is the compose step of the
export workflow.

```
runcomposer spec [FILTER] [--id ITEM_ID ...] [--from-history QUERY]
                 [--title TITLE] [--label KEY=VALUE ...]
                 [--format yaml|json] [-o OUT]
                 [--dry-run] [--export] [--token-file FILE]
                 [--runner RUNNER_ID] [--param NAME=VALUE ...]
                 [--expect-format FORMAT] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `FILTER` | — | Tag filter — [filter text](#filter-text) or a YAML/JSON AST, as in `compile`. |
| `--id ITEM_ID` | none | Explicit item pick, repeatable; intersects with the filter. |
| `--from-history QUERY` | none | History-based selection resolved at compose time, e.g. `failed@latest?suite=nightly`. |
| `--title TITLE` | `Untitled run` | The run title. |
| `--label KEY=VALUE` | none | Free-form provenance label, repeatable. Stored, never interpreted — and what scopes a later `failed@latest`. |
| `--format yaml\|json` | `yaml` | Output format of the document. |
| `-o`, `--out OUT` | stdout | Write the document to this file instead. |
| `--dry-run` | off | Compose and print the document **without storing a run**. |
| `--export` | off | Hand the document out: mint an **export dispatch** for exactly the bytes emitted, and its ingest token. The run moves to `AWAITING_RESULTS`. |
| `--token-file FILE` | — | With `--export`: write the ingest token to this file (mode `0600`) instead of printing it. |
| `--runner RUNNER_ID` | none | Compose *for* this configured runner: validate `--param` against its per-run parameters and write them into the spec's `runner` section. |
| `--param NAME=VALUE` | none | A per-run runner parameter, repeatable; needs `--runner`. [How values are read](#--param--per-run-runner-parameters). |
| `--expect-format FORMAT` | the runner's, else `runcomposer-verdicts` | The results format the run expects back — a `ResultParser` format id. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

At least one of `FILTER` / `--id` / `--from-history` is required. Unless
`--dry-run` is given, composing **creates a run record** in state `COMPOSED` —
the id on stderr is how you find it again.

```console
$ runcomposer spec 'Payments-Refunds' --title "Refunds smoke" --label suite=nightly
runspec: '1.0'
run:
  id: 01M7X3B9PDNEFDHZ9PK7J66N0G
  title: Refunds smoke
  created_at: '2026-11-20T18:26:22Z'
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
    at: '2026-11-20T18:26:22Z'
    count: 4
source:
  provider: manifest
  snapshot: sha256:ee00db96d49d82f4564bfb894e0de60a363672643b0a82d9afcb6298d67fe79f
results:
  expect:
  - format: runcomposer-verdicts
  shards: 1
  deliver: none
run: 01M7X3B9PDNEFDHZ9PK7J66N0G (state COMPOSED)
```

(The last line is on stderr.) The document carries **no token**: a spec is a
record of what to run, not a credential.

### `--dry-run` — look at the document, store nothing

```console
$ runcomposer spec 'Payments-Refunds' --dry-run | tail -3
dry run: nothing was stored
  - format: runcomposer-verdicts
  shards: 1
  deliver: none
```

The same compile, the same validation, the same document — and no run in the
store afterwards. `--dry-run` and `--export` exclude each other: there is no
run to export.

### `--export` — handing the document to someone else to run

`--export` mints a dispatch over the exact bytes that were written, so the
bundle that comes back can be correlated and its `spec_sha256` verified:

```console
$ runcomposer spec 'Payments' --title "Nightly payments" --label suite=nightly \
      --format json -o spec.json --export
spec written to spec.json
run: 01M7X3BA2EBNAEW1XZXKWXS8JD (state COMPOSED)
export dispatch: 01M7X3BA2M33C4BNM7B6BFMKJ2 (1 shard(s) declared, state AWAITING_RESULTS)
spec sha256: 0683a775a426eb5e335a87015340aff0ae376737e8d86ce129f643aa3903db6b
ingest token (shown once — needed to push results over HTTP): rct_DhsSnuCooKQRbpZqZbyYjXamrbmEM5hS

$ shasum -a 256 spec.json
0683a775a426eb5e335a87015340aff0ae376737e8d86ce129f643aa3903db6b  spec.json
```

Three things about the **ingest token**:

- It is **shown once**, here, and stored only as a hash. It is not in
  `spec.json`, not in the stored spec, and no command or API call reads it
  back. Lost it? Export again (`export-spec`) — or rotate it through the API —
  and use the new one.
- It belongs to **this dispatch**: results pushed with it are delivered to
  this dispatch and no other.
- Only the **HTTP push** (`POST /api/v1/runs/{id}/results`) asks for it.
  `runcomposer ingest` and the file-drop inbox are local, trusted paths and
  need none — so a loop that brings bundles home by file copy can ignore the
  token entirely. With `core.ingestion.tokens: disabled` none is minted.

`--token-file` keeps it out of a terminal scrollback and a CI log:

```console
$ runcomposer spec 'Checkout' --title "Nightly checkout" --format json -o checkout.json \
      --export --token-file checkout.token
spec written to checkout.json
run: 01M7X3BAYWFJ8T23DSVVT3NNV8 (state COMPOSED)
export dispatch: 01M7X3BAZ2NSV8BDFAGV7KYDDQ (1 shard(s) declared, state AWAITING_RESULTS)
spec sha256: 15109ce2b68b88f1485644f3b3a54f1a9030506d05ebf97c36607a906f1cd6c0
ingest token written to checkout.token

$ ls -l checkout.token | cut -c1-10
-rw-------
```

Without `--export` the document is just a document: composed, valid, and
nothing is waiting for it. To hand out a run that is already stored, use
[`export-spec`](#export-spec).

### `--id` — explicit picks

Repeat it. With no `FILTER`, the picks *are* the selection:

```console
$ runcomposer spec --id Shop.Payments.Cards.T001 --id Shop.Payments.Cards.T002 \
      --title "Two picks" --runner demo --param seed=nightly-7
runspec: '1.0'
run:
  id: 01M7X3BB905RSZHZ3HGKPSX63Y
  title: Two picks
  created_at: '2026-11-20T18:26:24Z'
selection:
  item_ids:
  - Shop.Payments.Cards.T001
  - Shop.Payments.Cards.T002
  materialized:
    item_ids:
    - Shop.Payments.Cards.T001
    - Shop.Payments.Cards.T002
    at: '2026-11-20T18:26:24Z'
    count: 2
…
runner:
  demo:
    seed: nightly-7
run: 01M7X3BB905RSZHZ3HGKPSX63Y (state COMPOSED)
```

An id the catalog does not know is refused at compose time:

```console
$ runcomposer spec --id Shop.No.Such.T999
error: unknown item ids (not in the catalog snapshot): Shop.No.Such.T999
```

### `--runner` and `--param` — composing for a runner

Note what `--runner demo --param seed=nightly-7` did above: it checked the
value against the per-run parameters the `demo` runner declares and wrote the
accepted values into the document's one open section, `runner.demo`. Nothing
is dispatched — [`dispatch --run`](#dispatch) does that later and finds the
parameters in the document.

What is written there is **only** what the runner declares as a per-run
parameter. A runner's *configured* options (suite roots, worker counts,
credentials) stay in the config file: they never travel in a spec, and a spec
cannot override them.

A wrong name or an unconfigured runner is refused before anything is stored:

```console
$ runcomposer spec Payments --runner demo --param sed=1
error: unknown parameter 'sed' (this runner accepts: ['seed'])

$ runcomposer spec Payments --runner robot-pool
error: no runner is configured under the id 'robot-pool' (configured: ['demo', 'demo-slow'])
```

`--expect-format` names the parser that should read the results. Composing for
a runner defaults it to what that runner produces; otherwise it is
`runcomposer-verdicts`, the reference JSON format (what
`runcomposer-exec --simulate` writes). Set it to `robot-output-xml` or
`junit-xml` when an external executor returns its framework's native
artifacts.

### `--from-history` — rerun what failed

The query is `<verdict>@<selector>[?key=value&…]`
([DESIGN.md](../DESIGN.md) §7): a verdict word (`failed`, `passed`, `skipped`,
`error`, `missing`, or several joined with `+`), a selector (`latest`,
`run:<id>`, `before:<ISO time>`), and an optional label scope. The scope is the
part that matters: unscoped, "latest" ranges over every completed run in the
store.

```console
$ runcomposer spec --from-history 'failed@latest?suite=nightly' \
      --title "Rerun failures" --label suite=adhoc
runspec: '1.0'
run:
  id: 01M7X3BR2XJ1AGCYK6YNRNVER3
  title: Rerun failures
  created_at: '2026-11-20T18:26:37Z'
  labels:
    suite: adhoc
selection:
  item_ids:
  - Shop.Checkout.Gift.T002
  materialized:
    item_ids:
    - Shop.Checkout.Gift.T002
    at: '2026-11-20T18:26:37Z'
    count: 1
  derived_from:
  - provider: history
    query:
      run: LATEST
      verdicts:
      - FAIL
      labels:
        suite: nightly
    resolved_run_id: 01M7X3BQGZF71M0YWCBJ9W0EJD
    resolved_run_completed_at: '2026-11-20T18:26:36Z'
    resolved_run_labels:
      suite: nightly
    resolved_run_title: Checkout regression
…
run: 01M7X3BR2XJ1AGCYK6YNRNVER3 (state COMPOSED)
```

The resolution happens **once, at compose time**: the emitted spec holds a
static item list plus `derived_from` provenance saying which run it came from
and why. Label the new run to say which stream it belongs to: with the same
label as the scope (`--label suite=nightly`) the rerun *joins* that stream and
becomes its "latest"; with another label, as here, it stays out of it.

[`rerun --scope failed`](#rerun) is the one-command form of this for a run you
can name.

---

## `run`

Compose a selection, dispatch it to a configured runner, wait, and summarise.
The everyday command, and the one for CI: it exits `0` **only if the run is
`PASS`**.

```
runcomposer run [FILTER] [--id ITEM_ID ...] [--from-history QUERY]
                --runner RUNNER [--param NAME=VALUE ...]
                [--title TITLE] [--label KEY=VALUE ...]
                [--no-wait] [--timeout SECONDS] [--json] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `FILTER`, `--id`, `--from-history` | — | The selection, as in [`spec`](#spec). |
| `--runner RUNNER` | **required** | A runner id configured under `runners:`. |
| `--param NAME=VALUE` | none | Per-run runner parameter, repeatable. |
| `--title`, `--label` | `Untitled run`, none | As in `spec`. |
| `--no-wait` | off | Return once the runner has handed the work off, instead of waiting for results that arrive later. |
| `--timeout SECONDS` | none | Give up waiting for those results after this long (exit `3`). |
| `--json` | off | Print the run detail as [one JSON document](#--json--one-document-for-a-script) instead of the summary. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

```console
$ runcomposer run Checkout --runner demo --title "Checkout regression" --label suite=nightly
demo runner: simulating 13 item(s) with seed 'demo'
run 01M7X3BQGZF71M0YWCBJ9W0EJD: dispatch 01M7X3BQH16V2JPX75VGMVNB5T via 'demo' (1 shard(s))
state: COMPLETE (FAIL) — 11 PASS, 1 FAIL, 1 SKIP
  shard 1: FAIL — 11 PASS, 1 FAIL, 1 SKIP
$ echo $?
1

$ runcomposer run Payments-Refunds --runner demo --title "Refunds" --param seed=demo-1
demo runner: simulating 4 item(s) with seed 'demo-1'
run 01M7X3BQQ8FY60Q5Y93TBY0RVF: dispatch 01M7X3BQQAE4AXA2KH2DR3TWE1 via 'demo' (1 shard(s))
state: COMPLETE (PASS) — 4 PASS
  shard 1: PASS — 4 PASS
$ echo $?
0
```

What is printed: the runner's **plan** verbatim (whatever it declared at the
hand-off), the dispatch, the state with the computed completion and verdict
counts, one line per shard group when the runner grouped its shards, and one
line per shard. To see the plan *before* anything runs, use
[`plan`](#plan).

### The runner executes in this process

`run`, `dispatch` and `rerun` dispatch **in the foreground**: the runner's
work happens inside the CLI process, and the command returns when the runner
did. For a runner that executes the tests itself (`robot-pool`, `command`,
`demo`) that is the whole run — a forty-minute suite is a forty-minute
command, and `Ctrl-C` cancels it.

`--no-wait` and `--timeout` are about the *other* kind of runner: one that
hands the work to an external executor and returns with results outstanding
(`jenkins` with `completion: callback`). Such a run is left
`AWAITING_RESULTS`; by default the command then waits for the results to
arrive — they come in through a server's push endpoint or a file drop, into
the same store — and `--no-wait` returns instead (exit `0`), `--timeout` bounds
the wait (exit `3` when it passes). For a runner that finished in the
foreground neither flag changes anything.

To start a run and *not* stay attached to it, use the server:
`POST /api/v1/runs` returns at once and the run is followed with
[`watch`](#watch).

### `--param` — per-run runner parameters

`NAME=VALUE`, repeatable. The value is read as YAML, so a list is a list and a
boolean is a boolean — except the values of a mapping, which are text as
typed: `--param 'variables={ORDER_LIMIT: 1300, STRICT: true}'` passes the
texts `1300` and `true`, as a `mapping` parameter wants them.
[`doctor`](#doctor) prints what each configured runner accepts. Here a `robot-pool` runner configured with two partitions:

```yaml
# shop/config.yaml
store:
  sqlite: { path: runcomposer.db }
sources:
  robotframework: { root: tests }
runners:
  robot-pool:
    suite_root: tests
    max_workers: 4
    partitions: [env1, env2]
```

```console
$ runcomposer run Payments --runner robot-pool --title "Payments on both environments" --config shop/config.yaml
robot-pool plan: 13 item(s) × 2 partition(s) → 2 chunk(s)/partition, max_workers=4
  cold start: no duration history — round-robin chunking; balance improves as completed dispatches accrue
  chunk 1: 7 item(s), est 0ms
  chunk 2: 6 item(s), est 0ms
  env1: 2 shard(s), 13 item(s)
  env2: 2 shard(s), 13 item(s)
run 01M7XRKA4YGXM1EDKQNT4Q130Z: dispatch 01M7XRKA5RH5MNAY3JTAAEJ3E8 via 'robot-pool' (4 shard(s))
state: COMPLETE (FAIL) — 24 PASS, 2 FAIL
  group env1: FAIL — 12 PASS, 1 FAIL
  group env2: FAIL — 12 PASS, 1 FAIL
  shard env1-1 [env1]: PASS — 7 PASS
  shard env1-2 [env1]: FAIL — 5 PASS, 1 FAIL
  shard env2-1 [env2]: PASS — 7 PASS
  shard env2-2 [env2]: FAIL — 5 PASS, 1 FAIL

$ runcomposer run Payments --runner robot-pool --param 'partitions=[env1]' --title "Payments on env1" --config shop/config.yaml
robot-pool plan: 13 item(s) × 1 partition(s) → 4 chunk(s)/partition, max_workers=4
  duration-balanced chunking: history for 13/13 item(s)
  chunk 1: 4 item(s), est 4ms
  chunk 2: 3 item(s), est 3ms
  chunk 3: 3 item(s), est 3ms
  chunk 4: 3 item(s), est 3ms
  env1: 4 shard(s), 13 item(s), est ~0.0s
run 01M7XRKGFV0DQBBC9EQK0GNJK4: dispatch 01M7XRKGGF4SGCJJYA61AH01X4 via 'robot-pool' (4 shard(s))
state: COMPLETE (FAIL) — 12 PASS, 1 FAIL
  group env1: FAIL — 12 PASS, 1 FAIL
  shard env1-1 [env1]: PASS — 4 PASS
  shard env1-2 [env1]: FAIL — 2 PASS, 1 FAIL
  shard env1-3 [env1]: PASS — 3 PASS
  shard env1-4 [env1]: PASS — 3 PASS

$ runcomposer run Payments --runner robot-pool --param 'partitions=[env3]' --config shop/config.yaml
error: parameter 'partitions' must be a list drawn from ['env1', 'env2'], got ['env3']
```

Thirteen items on two partitions is twenty-six verdicts, rolled up per shard
and per partition — "green on `env1-1`, red on `env1-2`" is read, not
computed. The second run plans from the first one's durations — these
tests take a millisecond or less, and one that ran in under a millisecond is
recorded as 1 ms, so history covers all of them. The third never became a
run.

**`run` leaves nothing behind when nothing ran.** If the runner id or a
parameter is wrong, the request is refused before a run exists. If the runner
*refuses* the dispatch (a drift check, a failed readiness hook), the composed
run is removed again and the command exits `3`.

---

## `plan`

Show what a dispatch would do — which item goes to which shard and group,
with duration estimates where history allows — without executing, storing or
triggering anything.

```
runcomposer plan [FILTER] [--id ITEM_ID ...] [--from-history QUERY]
                 [--run RUN_ID] --runner RUNNER [--param NAME=VALUE ...]
                 [--items] [--json] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `FILTER`, `--id`, `--from-history` | — | A selection, as in [`spec`](#spec). It is composed for the preview and **not stored**. |
| `--run RUN_ID` | — | Plan a stored run instead of a selection. Without `--param`, the parameters its document carries for the runner are used — the preview of what `dispatch --run` would start. The run is left alone, whatever its state. |
| `--runner RUNNER` | **required** | A runner id configured under `runners:`. It must offer previews (the `plan` capability; every runner that ships does). |
| `--param NAME=VALUE` | the runner's defaults | Per-run runner parameter, repeatable; validated as for a dispatch. |
| `--items` | off | List every item under its shard (default: counts only). |
| `--json` | off | The API's answer ([DESIGN.md](../DESIGN.md) §9): `refused`, `refusal`, `plan` with `text`, `shards`, `groups`, `notes`, `data`. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

A selection and `--run` exclude each other. The answer is the runner's own:
the plan text a dispatch would record, then the groups and shards, then
notes. The `robot-lanes` entry of
[`examples/robot-pool/config.yaml`](../examples/robot-pool/config.yaml)
distributes a selection over two partitions in pieces of two tests, each
piece a shard of its own — on a fresh store, round-robin:

```console
$ runcomposer plan Cart --runner robot-lanes --config robot-pool/config.yaml
plan for this selection on runner robot-lanes: 8 item(s); parameters {'partitions': ['eu', 'us'], 'allow_drift': False, 'browser': 'firefox'}
  robot-pool plan: 8 item(s) distributed over 2 of 2 partition(s) — each test once, max_workers=2
    cold start: no duration history — round-robin chunking; balance improves as completed dispatches accrue
    chunk 1: 4 item(s), est 0ms
    chunk 2: 4 item(s), est 0ms
    at most 2 test(s) per execution, each execution a shard of its own
    eu: 2 shard(s), 4 item(s)
    us: 2 shard(s), 4 item(s)
group eu: 2 shard(s), 4 item(s)
group us: 2 shard(s), 4 item(s)
shard eu-01 [eu]: 2 item(s)
shard eu-02 [eu]: 2 item(s)
shard us-01 [us]: 2 item(s)
shard us-02 [us]: 2 item(s)
note: cold start: no duration history for this selection — the distribution is round-robin until executions (or imported durations) say how long tests take
nothing was executed, stored or triggered
```

With durations known — here imported with [`history import`](#history) into
a fresh copy of the same example — the same preview is balanced, and
`--items` says which test goes where:

```console
$ runcomposer history import cart-durations.csv --config robot-pool/config.yaml
imported 8 duration(s)

$ runcomposer plan Cart --runner robot-lanes --items --config robot-pool/config.yaml
plan for this selection on runner robot-lanes: 8 item(s); parameters {'partitions': ['eu', 'us'], 'allow_drift': False, 'browser': 'firefox'}
  robot-pool plan: 8 item(s) distributed over 2 of 2 partition(s) — each test once, max_workers=2
    duration-balanced chunking: history for 8/8 item(s)
    chunk 1: 4 item(s), est 29000ms
    chunk 2: 4 item(s), est 29000ms
    at most 2 test(s) per execution, each execution a shard of its own
    eu: 2 shard(s), 4 item(s), est ~29.0s
    us: 2 shard(s), 4 item(s), est ~29.0s
group eu: 2 shard(s), 4 item(s), est 29.0s
group us: 2 shard(s), 4 item(s), est 29.0s
shard eu-01 [eu]: 2 item(s), est 20.0s
    Tests.Cart.Add To Cart Increases The Count
    Tests.Cart.Two Promo Codes Do Not Stack
shard eu-02 [eu]: 2 item(s), est 9.0s
    Tests.Cart.Expired Promo Code Is Refused
    Tests.Cart.Cart Subtotal Sums Line Items
shard us-01 [us]: 2 item(s), est 20.0s
    Tests.Cart.Remove From Cart Decreases The Count
    Tests.Cart.Promo Code Reduces The Total
shard us-02 [us]: 2 item(s), est 9.0s
    Tests.Cart.Free Gift Threshold Is Honoured
    Tests.Cart.Quantity Cannot Go Below One
nothing was executed, stored or triggered
```

**The preview says what the split was balanced on.** An item without a
known duration weighs the mean of the known ones, and an uneven count of
items per partition is often the balance working: with durations known for
six of the eight tests, one of them far longer than the rest, the plan says
how many were known, what the other two were weighed as, the estimate per
partition, and that the long test outweighs an even share on its own:

```console
$ cat cart-partial.csv
item,duration_s
Tests.Cart.Add To Cart Increases The Count,80
Tests.Cart.Quantity Cannot Go Below One,4
Tests.Cart.Promo Code Reduces The Total,9
Tests.Cart.Two Promo Codes Do Not Stack,8
Tests.Cart.Remove From Cart Decreases The Count,11
Tests.Cart.Cart Subtotal Sums Line Items,3

$ runcomposer history import cart-partial.csv --config robot-pool/config.yaml
imported 6 duration(s)

$ runcomposer plan Cart --runner robot-lanes --config robot-pool/config.yaml
plan for this selection on runner robot-lanes: 8 item(s); parameters {'partitions': ['eu', 'us'], 'allow_drift': False, 'browser': 'firefox'}
  robot-pool plan: 8 item(s) distributed over 2 of 2 partition(s) — each test once, max_workers=2
    duration-balanced chunking: history for 6/8 item(s); the other 2 weigh the mean of the known durations, 19167ms each
    chunk 1: 1 item(s), est 80000ms
    chunk 2: 7 item(s), est 35000ms measured + 2 without history → balanced on 73333ms
    Tests.Cart.Add To Cart Increases The Count alone weighs 80000ms, more than an even share of 76667ms per chunk: its chunk is the longest whatever the split, and the other items are balanced around it
    at most 2 test(s) per execution, each execution a shard of its own
    eu: 1 shard(s), 1 item(s), est ~80.0s
    us: 4 shard(s), 7 item(s), est ~73.3s
group eu: 1 shard(s), 1 item(s), est 80.0s
group us: 4 shard(s), 7 item(s)
shard eu-01 [eu]: 1 item(s), est 80.0s
shard us-01 [us]: 2 item(s)
shard us-02 [us]: 2 item(s), est 20.0s
shard us-03 [us]: 2 item(s), est 12.0s
shard us-04 [us]: 1 item(s), est 3.0s
note: duration history for 6 of 8 item(s); the other 2 are weighed with the mean of the known durations, 19.2s each — the estimates per partition assume they take that long
note: Tests.Cart.Add To Cart Increases The Count alone takes 80.0s, more than an even share of the selection: the partition holding it is the longest whatever the split, and the other items are balanced around it — an uneven count of items per partition is the balance working, not a fault
nothing was executed, stored or triggered
```

(A shard or group holding an item of unknown duration shows no estimate of
its own.) Robot Framework reports whole milliseconds, so a test that passed
or failed in under one is recorded as 1 ms — known to be fast, not unknown.

**A refusal is an answer** — the one the dispatch would meet — and exits
`1`. A run composed before the corpus changed, planned by id, before and
after a test was renamed:

```console
$ runcomposer plan --run 01M7XRFKXW5B6KBPNXDPRWZQF8 --runner robot-pool --config robot-pool/config.yaml
plan for run 01M7XRFKXW5B6KBPNXDPRWZQF8 on runner robot-pool: 8 item(s); parameters {'partitions': ['eu', 'us'], 'allow_drift': False}
  robot-pool plan: 8 item(s) × 2 partition(s) → 2 chunk(s)/partition, max_workers=4
    duration-balanced chunking: history for 8/8 item(s)
    chunk 1: 4 item(s), est 29000ms
    chunk 2: 4 item(s), est 29000ms
    eu: 2 shard(s), 8 item(s), est ~29.0s
    us: 2 shard(s), 8 item(s), est ~29.0s
    failures are re-executed up to 1 time(s)
group eu: 2 shard(s), 8 item(s), est 58.0s
group us: 2 shard(s), 8 item(s), est 58.0s
shard eu-1 [eu]: 4 item(s), est 29.0s
shard eu-2 [eu]: 4 item(s), est 29.0s
shard us-1 [us]: 4 item(s), est 29.0s
shard us-2 [us]: 4 item(s), est 29.0s
nothing was executed, stored or triggered

$ runcomposer plan --run 01M7XRFKXW5B6KBPNXDPRWZQF8 --runner robot-pool --config robot-pool/config.yaml
plan for run 01M7XRFKXW5B6KBPNXDPRWZQF8 on runner robot-pool: 8 item(s); parameters {'partitions': ['eu', 'us'], 'allow_drift': False}
would be refused: live corpus snapshot differs from source.snapshot in the spec — refusing (§3.3 drift check; composed sha256:21ee74fed1fcf5e66b71b3d4b6f4845c96d3acb4d3a9bfd4bcb58603bb1e95e4, live sha256:b61d4defb1f8f38933d1dc2c34296c66b03a1fc69945d7f56d07517f12379572). Set the allow_drift parameter to execute the intersection.
$ echo $?
1

$ runcomposer plan --run 01M7XRFKXW5B6KBPNXDPRWZQF8 --runner robot-pool --param allow_drift=true --param 'partitions=[eu]' --config robot-pool/config.yaml
plan for run 01M7XRFKXW5B6KBPNXDPRWZQF8 on runner robot-pool: 8 item(s); parameters {'partitions': ['eu'], 'allow_drift': True}
  robot-pool plan: 7 item(s) × 1 partition(s) → 4 chunk(s)/partition, max_workers=4
    duration-balanced chunking: history for 7/7 item(s)
    chunk 1: 1 item(s), est 12000ms
    chunk 2: 2 item(s), est 14000ms
    chunk 3: 2 item(s), est 13000ms
    chunk 4: 2 item(s), est 14000ms
    eu: 4 shard(s), 7 item(s), est ~14.0s
    failures are re-executed up to 1 time(s)
    drift: 1 planned item(s) no longer exist — reported as SKIP
group eu: 4 shard(s), 7 item(s), est 53.0s
shard eu-1 [eu]: 1 item(s), est 12.0s
shard eu-2 [eu]: 2 item(s), est 14.0s
shard eu-3 [eu]: 2 item(s), est 13.0s
shard eu-4 [eu]: 2 item(s), est 14.0s
shard drift: 1 item(s)
nothing was executed, stored or triggered
```

The estimate per partition (`~`) is what the balancer weighed it at — in
`split` its lane, in `each` the longest of its chunks, which run side by
side; a group's estimate is the sum of its shards'.

A runner that hands the work to somebody else previews what it would hand
over: the `jenkins` runner shows the build parameters a trigger would send —
long values as `‹N bytes›`, the token and the dispatch id as `‹at dispatch›`
— and its shards as "items decided by the executor", since the job
distributes them itself. It needs no Jenkins to answer; here the fan-out
entry of [`examples/jenkins/config.yaml`](../examples/jenkins/config.yaml),
whose field provider lives next to it (hence `PYTHONPATH`):

```console
$ PYTHONPATH=jenkins runcomposer plan Payments-Cards --runner jenkins-fanout --param 'regions=[eu,apac]' --config jenkins/config.yaml
plan for this selection on runner jenkins-fanout: 6 item(s); parameters {'regions': ['eu', 'apac'], 'hold_s': 0}
  jenkins: job http://localhost:8080/job/runcomposer-fanout would be triggered, 2 shard(s) ['eu', 'apac']; completion by polling the build
    SPEC_B64 = ‹592 bytes›
    REGIONS = eu,apac
    INCLUDE = Payments-Cards
    DISPATCH_ID = ‹at dispatch›
    HOLD_SECONDS = 0
shard eu: items decided by the executor
shard apac: items decided by the executor
note: the job distributes the items over its shards itself — a preview cannot say which item goes where
note: nothing was sent to Jenkins; the token and the dispatch id exist only once the run is dispatched
nothing was executed, stored or triggered
```

The `demo` runner says that nothing would run:

```console
$ runcomposer plan Payments-Refunds --runner demo --config demo/config.yaml
plan for this selection on runner demo: 4 item(s); parameters {'seed': 'demo'}
  demo runner: simulating 4 item(s) with seed 'demo'
shard 1: 4 item(s), est 10.7s
note: simulated: no test would be executed
nothing was executed, stored or triggered
```

A wrong runner id or parameter is an error (exit `1`), exactly as for
[`run`](#run) — and so is a value the runner's own plan refuses as an
invalid parameter, such as a variable the `robot-regions` entry protects:

```console
$ runcomposer plan Cart --runner robot-regions --param 'variables={shop_url: https://test.shop.example}' --config robot-pool/config.yaml
error: parameter 'variables': the Robot variable 'shop_url' cannot be set per run — this runner protects 'SHOP_URL' (protected_variables)
```

---

## `dispatch`

Dispatch something that already exists — a runspec file, or a stored run — to
a configured runner.

```
runcomposer dispatch (SPEC | --run RUN_ID) --runner RUNNER [--param NAME=VALUE ...]
                     [--no-wait] [--timeout SECONDS] [--json] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `SPEC` | — | Path to a runspec document (YAML or JSON). |
| `--run RUN_ID` | — | Dispatch this stored run instead of a file. Exactly one of the two. |
| `--runner RUNNER` | **required** | A runner id configured under `runners:`. |
| `--param NAME=VALUE` | what the document carries | Per-run runner parameter, repeatable. Given: these values are used and written into `runner.<id>` of the stored spec. Not given: the values already in the document's `runner.<id>` section are used. |
| `--no-wait`, `--timeout`, `--json` | | As in [`run`](#run). |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

A file is **imported as a run** under the id it carries (if that run is not
stored yet) and then dispatched:

```console
$ runcomposer spec 'Catalog' --title "Catalog check" --format json -o catalog.json
spec written to catalog.json
run: 01M7X3BR8KEPYEHH3H5Y4DH6XJ (state COMPOSED)

$ runcomposer dispatch catalog.json --runner demo
demo runner: simulating 9 item(s) with seed 'demo'
run 01M7X3BR8KEPYEHH3H5Y4DH6XJ: dispatch 01M7X3BREBEJ2TP96QVRRA4VAB via 'demo' (1 shard(s))
state: COMPLETE (PASS) — 9 PASS
  shard 1: PASS — 9 PASS
```

`--run` dispatches a run by id — here the one composed earlier with
`--runner demo --param seed=nightly-7`, whose parameter travels in its
document:

```console
$ runcomposer dispatch --run 01M7X3BB905RSZHZ3HGKPSX63Y --runner demo
demo runner: simulating 2 item(s) with seed 'nightly-7'
run 01M7X3BB905RSZHZ3HGKPSX63Y: dispatch 01M7X3BRM26QQ9HF48DBPDP87A via 'demo' (1 shard(s))
state: COMPLETE (PASS) — 2 PASS
  shard 1: PASS — 2 PASS
```

Things to know before you script this:

- **A spec file is a record, not an editable input.** A file whose run id is
  already stored must *be* that run's document; a copy whose selection, source
  or results contract was edited is refused, not silently ignored. (The one
  section that is the file's to state is `runner`: the runner's declared
  per-run parameters written there are taken as the parameters of this
  dispatch and validated like `--param`; a value that does not validate
  refuses the dispatch and leaves the stored run untouched.)

  ```console
  $ runcomposer dispatch catalog-edited.json --runner demo
  error: run 01M7X3BR8KEPYEHH3H5Y4DH6XJ is already stored and the given document differs from it — a run's spec does not change after composing. Compose a new run for a different selection (a spec file is a record, not an editable input)
  ```

- **A document cannot reconfigure the runner.** Of what a spec's
  `runner.<id>` section carries, only the runner's declared per-run parameters
  are honoured; any other key is ignored, with a warning recorded on the run.
- **It runs in the foreground**, like [`run`](#the-runner-executes-in-this-process).
- **Dispatching again is a new dispatch** of the same run. The run's headline
  state follows its latest dispatch; earlier dispatches and their results stay
  readable ([`show`](#show)). A run with a dispatch still in flight is refused
  until that one is over or cancelled.
- **The exit code is the run's**: `0` for `PASS`, `1` for any other
  completion, `3` when there is no result.

A runner that refuses leaves no dispatch behind — the run is as it was, and
the reason is in its event stream:

```console
$ runcomposer dispatch account.json --runner robot-pool --config shop/config.yaml
refused: live corpus snapshot differs from source.snapshot in the spec — refusing (§3.3 drift check; composed sha256:21ee74fed1fcf5e66b71b3d4b6f4845c96d3acb4d3a9bfd4bcb58603bb1e95e4, live sha256:9e3e09b7be07964e88673e34c1f81fd5a05b9f6b2a31873b5a970eef8086de5f). Set the allow_drift parameter to execute the intersection.
$ echo $?
3

$ runcomposer dispatch account.json --runner robot-pool --param allow_drift=true --param 'partitions=[env1]' --config shop/config.yaml
robot-pool plan: 8 item(s) × 1 partition(s) → 4 chunk(s)/partition, max_workers=4
…
state: COMPLETE (PASS) — 8 PASS
…
```

---

## `rerun`

Run a stored run again — all of it, or what did not pass.

```
runcomposer rerun RUN_ID [--scope all|failed] [--title TITLE] [--runner RUNNER]
                  [--param NAME=VALUE ...]
                  [--no-wait] [--timeout SECONDS] [--json] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `RUN_ID` | — | The run to run again. |
| `--scope all\|failed` | `all` | `all`: a new dispatch of the same run. `failed`: a **new run** composed from what did not pass. |
| `--title TITLE` | the source run's title | With `--scope failed`: the title of the run it composes. |
| `--runner RUNNER` | the one the run last used | A configured runner id. Required for a run that was only ever exported or ingested. |
| `--param NAME=VALUE` | those of the last dispatch (same runner) | Per-run runner parameter, repeatable. |
| `--no-wait`, `--timeout`, `--json` | | As in [`run`](#run). |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

```console
$ runcomposer rerun 01M7X3BQGZF71M0YWCBJ9W0EJD
demo runner: simulating 13 item(s) with seed 'demo'
run 01M7X3BQGZF71M0YWCBJ9W0EJD: dispatch 01M7X3BRZVSNG3EPE3NJP1YA9F via 'demo' (1 shard(s))
state: COMPLETE (FAIL) — 11 PASS, 1 FAIL, 1 SKIP
  shard 1: FAIL — 11 PASS, 1 FAIL, 1 SKIP
```

Same run id, a second dispatch. `--scope failed` is a different thing — a
different item set is a different run. In the demo workspace
(`runcomposer demo --workspace demo`), against a "Checkout regression" run
composed there with one failure:

```console
$ runcomposer rerun 01M7X98NN8478Z22NS2YQ0BKKY --scope failed --param seed=retry-1 --config demo/config.yaml
rerun of 01M7X98NN8478Z22NS2YQ0BKKY: composed run 01M7X98WYBW7Z1NGPE39YXJM18
demo runner: simulating 1 item(s) with seed 'retry-1'
run 01M7X98WYBW7Z1NGPE39YXJM18: dispatch 01M7X98WYCBQVEQN21W3Z1EWQ6 via 'demo' (1 shard(s))
state: COMPLETE (PASS) — 1 PASS
  shard 1: PASS — 1 PASS

$ runcomposer rerun 01M7X98NN8478Z22NS2YQ0BKKY --scope failed --title "Checkout failures, second look" --config demo/config.yaml
rerun of 01M7X98NN8478Z22NS2YQ0BKKY: composed run 01M7X98X3WADVQC9YYZTY5PW6A
demo runner: simulating 1 item(s) with seed 'demo'
run 01M7X98X3WADVQC9YYZTY5PW6A: dispatch 01M7X98X3XDH3JPD10S6VJCNR0 via 'demo' (1 shard(s))
state: COMPLETE (FAIL) — 1 FAIL
  shard 1: FAIL — 1 FAIL

$ runcomposer runs --config demo/config.yaml --limit 3
RUN ID                      STATE             RESULT      MODE          CREATED               TITLE
01M7X98X3WADVQC9YYZTY5PW6A  COMPLETE          FAIL        demo          2026-11-20T20:09:55Z  Checkout failures, second look
01M7X98WYBW7Z1NGPE39YXJM18  COMPLETE          PASS        demo          2026-11-20T20:09:55Z  Checkout regression
01M7X98NN8478Z22NS2YQ0BKKY  COMPLETE          FAIL        demo          2026-11-20T20:09:47Z  Checkout regression
# 3 of 8 run(s) — page with --limit / --offset
```

"Failed" here means everything that is not a pass or a skip: `FAIL`, `ERROR`
and `MISSING` (planned, never reported). The new run keeps the original's
title unless `--title` gives it another, carries the original's labels — so
it joins the same stream — and says what it is as data rather than in its
title: origin `rerun`, `derived_from` provenance naming the run it came
from, and `rerun_of` in its detail:

```console
$ runcomposer show 01M7X98WYBW7Z1NGPE39YXJM18 --json --config demo/config.yaml | grep -A3 rerun_of
  "rerun_of": {
    "run_id": "01M7X98NN8478Z22NS2YQ0BKKY",
    "scope": "failed"
  },
```

Without `--param`, a rerun takes the parameters of the source run's last
dispatch to the same runner — which is why the second rerun simulated with
the seed `demo` again. `--scope failed` needs a run that is over; the exit
code is the *new* run's.

---

## `cancel`

Cancel a run's dispatch in flight.

```
runcomposer cancel RUN_ID [--force] [--json] [--config PATH]
```

What happens depends on where the dispatch is:

- **Executing in another process** — typically `runcomposer serve`. The
  request is **recorded in the store**, and the process executing the run picks
  it up (within about half a second), tells the runner to stop, and marks the
  dispatch `CANCELLED` when the runner comes back. So `cancel` works from any
  shell that can reach the same store; it does not need to talk to the server.
- **Only waiting for results** (`AWAITING_RESULTS` — an export, or a runner
  that handed off to an external executor). The runner, if it can, is asked to
  stop the executor, and the dispatch is `CANCELLED` at once. Results that
  arrive for it afterwards are refused.

`--force` is the way out, not the normal path: it closes the dispatch as
`CANCELLED` here and now, whoever owns it. It is for a dispatch whose owning
process can no longer be reached to honour the request — its process id is
alive, but belongs to something else by now.

A run started through the server with a slow runner, cancelled from the CLI:

```console
$ curl -s -X POST localhost:8200/api/v1/runs -H 'Content-Type: application/json' \
      -d '{"selection": {"tag_filter": "Checkout"}, "title": "Slow checkout", "dispatch": {"runner": "demo-slow"}}'
…
$ runcomposer show 01M7X9DZHKZD64WRKBQXF0XC7J --config envs/staging/config.yaml
run 01M7X9DZHKZD64WRKBQXF0XC7J  Slow checkout
state: RUNNING — no verdicts
planned: 13 item(s); created 2026-11-20T20:12:41Z; origin api; labels {}
dispatch 01M7X9DZHN62WK74DM0JJ8MNC8  demo-slow  RUNNING  no verdicts
  shard 1: outstanding — no verdicts
  running now: 1 item(s)
  live: 4 item(s) reported so far by shards still executing

$ runcomposer cancel 01M7X9DZHKZD64WRKBQXF0XC7J --config envs/staging/config.yaml
run 01M7X9DZHKZD64WRKBQXF0XC7J: cancellation requested for dispatch 01M7X9DZHN62WK74DM0JJ8MNC8 (RUNNING) — the process executing it will stop it; follow with `runcomposer watch 01M7X9DZHKZD64WRKBQXF0XC7J`

$ runcomposer watch 01M7X9DZHKZD64WRKBQXF0XC7J --config envs/staging/config.yaml
2026-11-20T20:12:41Z  #1    run.composed
2026-11-20T20:12:41Z  #2    run.dispatched  state=DISPATCHED
2026-11-20T20:12:41Z  #3    run.running  state=RUNNING
2026-11-20T20:12:45Z  #4    run.cancel_requested  state=RUNNING
2026-11-20T20:12:46Z  #5    run.cancelled  state=CANCELLED  error=cancelled on request  13 MISSING
$ echo $?
3
```

(The `demo-slow` runner takes a second per item and streams each one's
status while it goes: one item `RUNNING`, three already reported.)

An export nobody will answer, cancelled at once:

```console
$ runcomposer cancel 01M7X3BAYWFJ8T23DSVVT3NNV8
run 01M7X3BAYWFJ8T23DSVVT3NNV8: dispatch 01M7X3BAZ2NSV8BDFAGV7KYDDQ CANCELLED
```

`cancel` exits `0` when the request was made. A run with nothing in flight
exits `1`:

```console
$ runcomposer cancel 01M7X9DZHKZD64WRKBQXF0XC7J --config envs/staging/config.yaml
error: run 01M7X9DZHKZD64WRKBQXF0XC7J has nothing in flight to cancel (state CANCELLED)
```

Whatever was delivered before the cancellation is kept; items that never
reported are `MISSING` — also the three seen passing live, since no result
was delivered for them. The run says both:

```console
$ runcomposer show 01M7X9DZHKZD64WRKBQXF0XC7J --config envs/staging/config.yaml
run 01M7X9DZHKZD64WRKBQXF0XC7J  Slow checkout
state: CANCELLED — 13 MISSING
planned: 13 item(s); created 2026-11-20T20:12:41Z; origin api; labels {}
dispatch 01M7X9DZHN62WK74DM0JJ8MNC8  demo-slow  CANCELLED  13 MISSING
  error: cancelled on request
  shard 1: outstanding — 13 MISSING
  last live status of shards that never delivered: 4 item(s)

$ curl -s localhost:8200/api/v1/runs/01M7X9DZHKZD64WRKBQXF0XC7J | python3 -m json.tool | grep -m1 -B7 -A6 '"last_live": {'
            "item_id": "Shop.Checkout.Express.T001",
            "status": "MISSING",
            "duration_ms": 0,
            "message": "not-reported",
            "shard": "1",
            "attempt": 1,
            "live": false,
            "last_live": {
                "status": "PASS",
                "duration_ms": 3277,
                "message": "",
                "shard": "1"
            }
        },
```

Every `MISSING` row of a run detail carries `last_live` — what was last
streamed for that item, or `null` — so "never started" and "ran, result not
delivered" can be told apart without matching rows up. To cancel a run this
very shell is executing, press `Ctrl-C`.

---

## `watch`

Follow a run's event stream until the run is over.

```
runcomposer watch RUN_ID [--after SEQ] [--no-follow] [--timeout SECONDS]
                  [--json] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `RUN_ID` | — | The run to follow. |
| `--after SEQ` | `0` | Only events after this sequence number (the `#n` column) — resume where you stopped. |
| `--no-follow` | off | Print what is there and return (exit `0`). |
| `--timeout SECONDS` | none | Stop following after this long (exit `3`). |
| `--json` | off | One JSON object per event, one per line. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

Every state change, every delivery, every refusal and every runner log line of
a run is an event. `watch` reads them out of the store, so it follows a run
whichever process is executing it:

```console
$ runcomposer watch 01M7X3BQGZF71M0YWCBJ9W0EJD --no-follow
2026-11-20T18:26:36Z  #7    run.composed
2026-11-20T18:26:36Z  #8    run.dispatched  state=DISPATCHED
2026-11-20T18:26:36Z  #9    run.running  state=RUNNING
2026-11-20T18:26:36Z  #10   delivery.recorded  shard=1  outcome=new
2026-11-20T18:26:36Z  #11   run.completed  state=COMPLETE  completion=FAIL  11 PASS, 1 FAIL, 1 SKIP
2026-11-20T18:26:38Z  #27   run.dispatched  state=DISPATCHED
2026-11-20T18:26:38Z  #28   run.running  state=RUNNING
2026-11-20T18:26:38Z  #29   delivery.recorded  shard=1  outcome=new
2026-11-20T18:26:38Z  #30   run.completed  state=COMPLETE  completion=FAIL  11 PASS, 1 FAIL, 1 SKIP
```

Two dispatches of one run, in order (sequence numbers are per store, so a run's
own events are not consecutive). Without `--no-follow` the command stays until
the run is in a terminal state — `COMPLETE`, `FAILED`, `CANCELLED`, `STALE` —
or back to `COMPOSED` after a refusal, and **exits with the run's code**: `0`
for `PASS`, `1` for any other completion, `3` for no result. That makes
`watch` the way to block a script on a run somebody else started. When the
process executing the run is gone ([`show`](#show)), `watch` says so once
on stderr and keeps following — until somebody reconciles the dispatch, or
`--timeout`:

```console
$ runcomposer watch 01M7XRJCMQKS9MGQSZEZK9CSQG --timeout 1 --config slow/config.yaml
2026-11-21T00:37:15Z  #1    run.composed
2026-11-21T00:37:15Z  #2    run.dispatched  state=DISPATCHED
2026-11-21T00:37:15Z  #3    run.running  state=RUNNING
warning: 01M7XRJCMQKS9MGQSZEZK9CSQG: the process executing dispatch 01M7XRJCNKQ7Y2H9P8J793GJ4Z (ci-runner-1:87492:01M7XRJCKQ777F3DMPSCSJBD8H) is gone — nothing will finish it. `runcomposer doctor` (or starting `serve`) marks it STALE; `runcomposer cancel --force 01M7XRJCMQKS9MGQSZEZK9CSQG` closes it
timed out
```

```console
$ runcomposer watch 01M7X3BQGZF71M0YWCBJ9W0EJD --no-follow --json | tail -1
{"seq": 30, "run_id": "01M7X3BQGZF71M0YWCBJ9W0EJD", "at": "2026-11-20T18:26:38Z", "kind": "run.completed", "dispatch_id": "01M7X3BRZVSNG3EPE3NJP1YA9F", "data": {"state": "COMPLETE", "mode": "demo", "title": "Checkout regression", "labels": {"suite": "nightly"}, "latest": true, "completion": "FAIL", "summary": {"PASS": 11, "SKIP": 1, "FAIL": 1}}}
```

---

## `show`

One run in full.

```
runcomposer show RUN_ID [--json] [--config PATH]
```

The headline, every dispatch with its own state and counts, the roll-up per
shard group and per shard of the latest dispatch, and the artifacts recorded
for the run:

```console
$ runcomposer show 01M7XRKA4YGXM1EDKQNT4Q130Z --config shop/config.yaml
run 01M7XRKA4YGXM1EDKQNT4Q130Z  Payments on both environments
state: COMPLETE (FAIL) — 24 PASS, 2 FAIL
planned: 13 item(s); created 2026-11-21T00:37:45Z; origin cli; labels {}
dispatch 01M7XRKA5RH5MNAY3JTAAEJ3E8  robot-pool  COMPLETE (FAIL)  24 PASS, 2 FAIL
  group env1: FAIL — 12 PASS, 1 FAIL
  group env2: FAIL — 12 PASS, 1 FAIL
  shard env1-1 [env1]: PASS — 7 PASS
  shard env1-2 [env1]: FAIL — 5 PASS, 1 FAIL
  shard env2-1 [env2]: PASS — 7 PASS
  shard env2-2 [env2]: FAIL — 5 PASS, 1 FAIL
  artifact log.html (env1-2): /artifacts/01M7XRKA4YGXM1EDKQNT4Q130Z/01M7XRKA5RH5MNAY3JTAAEJ3E8/env1-2/log.html
  artifact report.html (env1-2): /artifacts/01M7XRKA4YGXM1EDKQNT4Q130Z/01M7XRKA5RH5MNAY3JTAAEJ3E8/env1-2/report.html
  artifact output.xml (env1-2): /artifacts/01M7XRKA4YGXM1EDKQNT4Q130Z/01M7XRKA5RH5MNAY3JTAAEJ3E8/env1-2/output.xml
…
```

A group line appears for a runner that grouped its shards (`robot-pool`
groups by partition); its result is `n/m shard(s) delivered` until every
shard of the group is in. An artifact line is the path under which
`runcomposer serve` serves the file (`http://<host>:<port>/artifacts/…`); the
file itself is under `core.artifact_dir` with the same relative path. Only
recorded files are served. A recorded *directory* — `robot-pool` records
each shard's directory as `files (<shard>)`, so that what a log refers to is
servable too — is printed as its path on disk, since it is not a document to
open. An artifact that is a remote URL (a CI build link) is printed as that
URL.

The counts are over **effective** verdicts — the last attempt of each item per
shard — and include `MISSING`, the status of an item that was planned and
never reported. A run with a missing item is `INCOMPLETE` at best, never
`PASS`:

```console
$ runcomposer show 01M7X3B9PDNEFDHZ9PK7J66N0G
run 01M7X3B9PDNEFDHZ9PK7J66N0G  Refunds smoke
state: COMPLETE (INCOMPLETE) — 3 PASS, 1 MISSING
planned: 4 item(s); created 2026-11-20T18:26:22Z; origin cli; labels {'suite': 'nightly'}
dispatch 01M7X3DD0GCKQGKG8PT3J34RHS  export  COMPLETE (INCOMPLETE)  3 PASS, 1 MISSING
  shard 1: PASS — 3 PASS
  reported by no shard: 1 MISSING
```

(The shard delivered three passes; the fourth planned item is missing from the
run, not from the shard's own report — an export does not say which shard
carries which item. A runner that declares its shards' items, as `robot-pool`
does, shows the missing item under its shard.) While shards are still
executing, `running now:` counts the items executing at this moment and
`live:` the items they have reported so far; for a run that ended without
those shards delivering, a last line counts what they had streamed
([`cancel`](#cancel) shows both). `--json` prints everything the API's run
detail has: verdicts with shard, attempt and duration (`last_live` on a
`MISSING` row), groups, `live_summary`, `rerun_of`, deliveries, events, and
the selection the run was composed from — and `owner_gone`, on the run and
on each dispatch (below). An artifact's `url_or_path` there is the path as
it was recorded, since the CLI runs where the files are; over HTTP it is
never a server path ([DESIGN.md](../DESIGN.md) §6.4).

**A dispatch whose process is gone.** A dispatch is executed by one process
— a `runcomposer run`, or a `serve`. When that process was killed, nothing
will finish the dispatch, though it still says `RUNNING` until somebody
reconciles it. `show`, `runs` and `watch` only read, so they do not mark it
`STALE`; they flag it, and say how to reconcile it. A `run` of a `demo`
runner configured with `delay_s: 3`, killed with `kill -9` while it
executed:

```console
$ runcomposer show 01M7XRJCMQKS9MGQSZEZK9CSQG --config slow/config.yaml
run 01M7XRJCMQKS9MGQSZEZK9CSQG  Refunds, slowly
state: RUNNING (owner gone) — no verdicts
planned: 4 item(s); created 2026-11-21T00:37:15Z; origin cli; labels {}
dispatch 01M7XRJCNKQ7Y2H9P8J793GJ4Z  demo  RUNNING (owner gone)  no verdicts
  shard 1: outstanding — no verdicts
  running now: 1 item(s)
  live: 1 item(s) reported so far by shards still executing
# 01M7XRJCMQKS9MGQSZEZK9CSQG: the process executing dispatch 01M7XRJCNKQ7Y2H9P8J793GJ4Z (ci-runner-1:87492:01M7XRJCKQ777F3DMPSCSJBD8H) is gone — nothing will finish it. `runcomposer doctor` (or starting `serve`) marks it STALE; `runcomposer cancel --force 01M7XRJCMQKS9MGQSZEZK9CSQG` closes it
```

The owner is `host:pid:token`; it is gone when that process is dead (on
Linux, also when it ended and was never reaped) or ran on another host.
[`doctor`](#doctor), or starting [`serve`](#serve), marks such a dispatch
`STALE`, and so does a `serve` already running on the same store, within
`core.dispatch.recovery_interval_s` (default 15 seconds); [`cancel --force`](#cancel) closes it as `CANCELLED`
(`tests/test_cli_owner_gone.py`, `tests/test_recovery_periodic.py`).

---

## `runs`

List stored runs — or ask the store which items failed.

```
runcomposer runs [--state STATE] [--label KEY=VALUE ...] [-q TEXT]
                 [--since ISO] [--until ISO] [--limit N] [--offset N]
                 [--failed-in SELECTOR] [--json] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `--state STATE` | all | Lifecycle state: `COMPOSED`, `DISPATCHED`, `RUNNING`, `AWAITING_RESULTS`, `COMPLETE`, `FAILED`, `CANCELLED`, `STALE`. |
| `--label KEY=VALUE` | none | Only runs carrying this label. Repeatable; **all** must match. |
| `-q`, `--query TEXT` | none | Case-insensitive substring of the title. |
| `--since ISO` | none | Only runs created at/after this ISO-8601 time. |
| `--until ISO` | none | Only runs created at/before this ISO-8601 time. |
| `--limit N` | `20` | Maximum rows. |
| `--offset N` | `0` | Skip this many rows — the next page. |
| `--failed-in SELECTOR` | — | Switch modes: print the **item ids** that FAILED in the selected run. |
| `--json` | off | `{"runs": […], "total", "limit", "offset"}` — each row with verdict counts, planned count, duration, the latest dispatch's mode, and `owner_gone` (on the row, for its latest dispatch, and on each of its `dispatches`). |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

In the demo workspace of the [`rerun`](#rerun) samples — the seeded nightly
runs, a "Checkout regression" and the two reruns of its failures, which
carry its labels:

```console
$ runcomposer runs --state COMPLETE --label suite=nightly --config demo/config.yaml
RUN ID                      STATE             RESULT      MODE          CREATED               TITLE
01M7X98X3WADVQC9YYZTY5PW6A  COMPLETE          FAIL        demo          2026-11-20T20:09:55Z  Checkout failures, second look
01M7X98WYBW7Z1NGPE39YXJM18  COMPLETE          PASS        demo          2026-11-20T20:09:55Z  Checkout regression
01M7X98NN8478Z22NS2YQ0BKKY  COMPLETE          FAIL        demo          2026-11-20T20:09:47Z  Checkout regression
01M7X96VB481BVG60R976QG44Z  COMPLETE          FAIL        demo          2026-11-20T20:08:48Z  Nightly regression #3
01M7X96VB101N9E42251J59VJC  COMPLETE          FAIL        demo          2026-11-20T20:08:48Z  Nightly regression #2
01M7X96VAZFA7RMHP5YPCG3HJZ  COMPLETE          FAIL        demo          2026-11-20T20:08:48Z  Nightly regression #1
```

`STATE` is the lifecycle state; `RESULT` is the computed completion of a
finished run (`PASS`, `FAIL`, `ERROR`, `INCOMPLETE`); `MODE` is how the run was
last dispatched — a runner id, `export`, or `ingest` (results that arrived for
a run nothing was dispatched for). Filters combine, the window is on
**creation** time, and times may carry an offset:

```console
$ runcomposer runs -q checkout --limit 2 --config demo/config.yaml
RUN ID                      STATE             RESULT      MODE          CREATED               TITLE
01M7X98X3WADVQC9YYZTY5PW6A  COMPLETE          FAIL        demo          2026-11-20T20:09:55Z  Checkout failures, second look
01M7X98WYBW7Z1NGPE39YXJM18  COMPLETE          PASS        demo          2026-11-20T20:09:55Z  Checkout regression
# 2 of 3 run(s) — page with --limit / --offset

$ runcomposer runs --since 2026-11-20T00:00:00Z --until 2026-11-22T00:00:00+01:00 --limit 2 --offset 1
RUN ID                      STATE             RESULT      MODE          CREATED               TITLE
01M7X3BR8KEPYEHH3H5Y4DH6XJ  COMPLETE          PASS        demo          2026-11-20T18:26:37Z  Catalog check
01M7X3BR2XJ1AGCYK6YNRNVER3  COMPOSED          -           -             2026-11-20T18:26:37Z  Rerun failures
# 2 of 9 run(s) — page with --limit / --offset
```

A dispatch whose executing process is gone ([`show`](#show)) reads
`RUNNING (owner gone)` — the column widens only when one does — followed by
a line that says how to reconcile it:

```console
$ runcomposer runs --config slow/config.yaml
RUN ID                      STATE                 RESULT      MODE          CREATED               TITLE
01M7XRJCMQKS9MGQSZEZK9CSQG  RUNNING (owner gone)  -           demo          2026-11-21T00:37:15Z  Refunds, slowly
# 01M7XRJCMQKS9MGQSZEZK9CSQG: the process executing dispatch 01M7XRJCNKQ7Y2H9P8J793GJ4Z (ci-runner-1:87492:01M7XRJCKQ777F3DMPSCSJBD8H) is gone — nothing will finish it. `runcomposer doctor` (or starting `serve`) marks it STALE; `runcomposer cancel --force 01M7XRJCMQKS9MGQSZEZK9CSQG` closes it
```

A state that does not exist, or a time that is not one, is an error rather
than an empty list:

```console
$ runcomposer runs --state DONE
error: unknown run state 'DONE' (one of ['COMPOSED', 'DISPATCHED', 'RUNNING', 'AWAITING_RESULTS', 'COMPLETE', 'FAILED', 'CANCELLED', 'STALE'])
```

### `--failed-in` — the id list, for piping

`--failed-in` changes what the command prints: a `#` header naming the run it
resolved to, then one item id per line. The selector is `latest`, `run:<id>`,
or `before:<ISO time>`, each optionally scoped:

```console
$ runcomposer runs --failed-in latest --label suite=nightly
# 0 item(s) FAILED in run 01M7X3BA2EBNAEW1XZXKWXS8JD (scope: {'suite': 'nightly'})
```

"Latest" means latest *completed*: in the store most samples on this page
use (`envs/staging`) that is "Nightly payments",
whose exported results were ingested after the other nightly runs finished
— and nothing failed in it. To ask about the "Checkout regression" run
instead, name it. Piping means dropping the header:

```console
$ runcomposer runs --failed-in 'run:01M7X3BQGZF71M0YWCBJ9W0EJD' | grep -v '^#'
Shop.Checkout.Gift.T002
```

The scope may be written either way — `--label suite=nightly`, or inline in the
selector: `'latest?suite=nightly'`. **Scope it.** Unscoped, `latest` ranges over every completed
run in the store, so where one-off selections and a nightly stream share an
instance, the rerun may follow a run it was never meant to. A scope on
`run:<id>` is refused rather than ignored, since it could only mislead:

```console
$ runcomposer runs --failed-in 'run:01M7X3BQGZF71M0YWCBJ9W0EJD?suite=nightly'
error: history selector 'run:01M7X3BQGZF71M0YWCBJ9W0EJD' already names one run, so the label scope {'suite': 'nightly'} could only be ignored — drop one of the two
```

"Latest" is decided by a run's **headline**, which follows its latest
dispatch: a completed run that is exported or dispatched again is
`AWAITING_RESULTS` or `RUNNING` until that dispatch is over, and is not a
candidate meanwhile.

To *compose* from that answer rather than read it, use
[`spec --from-history`](#--from-history--rerun-what-failed) or
[`rerun --scope failed`](#rerun) — they record provenance, which this listing
does not.

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
$ runcomposer export 01M7X3BQGZF71M0YWCBJ9W0EJD --format ctrf -o checkout-ctrf.json
CTRF report written to checkout-ctrf.json
$ head -24 checkout-ctrf.json
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
      "start": 1795199196000,
      "stop": 1795199198000
    },
    "tests": [
      {
        "name": "Shop.Checkout.Express.T001",
        "status": "passed",
        "duration": 3277,
        "extra": {
          "shard": "1",
          "attempt": 1
        }
…
```

It reports the run's **latest dispatch**, one entry per effective verdict.
`ERROR` and `MISSING` have no CTRF status of their own: they are reported as
`other`, with the original in `rawStatus`.

Anything else is refused up front (exit `2`), with the supported list in the
message:

```console
$ runcomposer export 01M7X3BQGZF71M0YWCBJ9W0EJD --format junit
error: unknown export format 'junit' (supported: ctrf)
```

Note the direction: `export` reads results *out* of runcomposer.
[`export-spec`](#export-spec) and `spec --export` go the other way — they hand
a spec to an external executor.

---

## `export-spec`

Hand a **stored** run's document out for execution elsewhere. What
`spec --export` does at compose time, for a run that already exists.

```
runcomposer export-spec RUN_ID [--format yaml|json] [-o OUT] [--token-file FILE]
                        [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `RUN_ID` | — | The run to hand out. |
| `--format yaml\|json` | `json` | Format of the document. (`runcomposer-exec` reads JSON everywhere.) |
| `-o`, `--out OUT` | stdout | Write the document to this file instead. |
| `--token-file FILE` | — | Write the ingest token to this file (mode `0600`) instead of printing it. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

```console
$ runcomposer export-spec 01M7X3B9PDNEFDHZ9PK7J66N0G -o refunds.json
spec written to refunds.json
export dispatch: 01M7X3DD0GCKQGKG8PT3J34RHS (1 shard(s) declared, state AWAITING_RESULTS)
spec sha256: 480c23d5fa2444d8ee042a9b80d24c828b88b4e9191dcc92faa48ddb55988001
ingest token (shown once — needed to push results over HTTP): rct_DagIT5E2eMqqM8TesLmwfgppSy13_2Wa
```

Every call is an explicit hand-out: it mints **a new export dispatch** — with
its own recorded bytes, hash and [token](#--export--handing-the-document-to-someone-else-to-run)
— and the run waits in `AWAITING_RESULTS` for it. The exact bytes of each
hand-out are kept and can be downloaded again from the server
(`GET /api/v1/runs/{id}/dispatches/{dispatch_id}/spec`); a marker written from
them verifies on the way back. A run that a runner is executing right now
cannot be exported until that is over.

---

## `ingest`

Take a results bundle into a run.

```
runcomposer ingest BUNDLE [--run RUN_ID] [--dispatch DISPATCH_ID] [--shard SHARD]
                   [--format FORMAT] [--allow-unsolicited] [--json] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `BUNDLE` | — | Path to the bundle: a directory or a single file. |
| `--run RUN_ID` | from the bundle marker | The run to attach to. Required when the bundle has no marker. |
| `--dispatch DISPATCH_ID` | from the marker; else the dispatch still awaiting results; else the latest | Which dispatch this delivery belongs to. |
| `--shard SHARD` | from the marker, else `1` | The shard label for this delivery. |
| `--format FORMAT` | what the run expects (`results.expect`) | The result format id to parse the bundle with. |
| `--allow-unsolicited` | off | Promote a bundle with no marker / an unknown run to its **own** run with `origin: ingested`. |
| `--json` | off | [One JSON document](#--json--one-document-for-a-script). |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

The normal path needs no flags at all, because the marker
(`runcomposer_run.json`) that `runcomposer-exec` wrote carries the correlation
— and no token: `ingest` is a local action on the store.

```console
$ runcomposer ingest results
01M7X3BA2EBNAEW1XZXKWXS8JD shard 1: delivery recorded (17 verdict(s))
run state: COMPLETE (PASS)
```

**Re-ingesting is safe.** Every delivery is content-hashed, so the same bundle
again is a no-op — which is what makes inbox pollers, git re-pulls and CI
retries harmless:

```console
$ runcomposer ingest results
01M7X3BA2EBNAEW1XZXKWXS8JD shard 1: the same bundle was already ingested — no-op (17 verdict(s))
run state: COMPLETE (PASS)
```

A *different* bundle for the same `(run, dispatch, shard)` **replaces** that
shard's verdicts — last-writer-wins, so a correction can flip `FAIL` back to
`PASS`.

**Completion is computed against what was planned.** A dispatch is complete
when every shard it declared has delivered — and then each planned item that
never reported counts as `MISSING`. Three of four planned results arriving is
not a green run:

```console
$ runcomposer ingest refunds-first --run 01M7X3B9PDNEFDHZ9PK7J66N0G
01M7X3B9PDNEFDHZ9PK7J66N0G shard 1: delivery recorded (3 verdict(s))
run state: COMPLETE (INCOMPLETE)
```

A bundle nobody asked for is refused rather than guessed at:

```console
$ runcomposer ingest stray
error: bundle has no runcomposer_run.json marker and no run id was given — unsolicited bundles are not auto-attached (DESIGN.md §4/§5); quarantine it, or promote explicitly (--allow-unsolicited)

$ runcomposer ingest stray --allow-unsolicited
unsolicited bundle promoted to run 01M7X3DTKNEXWQNQK5WHNR4E6B (origin: ingested)
01M7X3DTKNEXWQNQK5WHNR4E6B shard 1: delivery recorded (1 verdict(s))
run state: COMPLETE (PASS)
```

Use `--run` and `--shard` when you are correlating by hand:

```console
$ runcomposer ingest results-env1 --run 01M7X3M6CH4EY5JAP6RJ9VH5K8 --shard env1
warning: run had no dispatch — an 'ingest' dispatch was opened for the delivery
01M7X3M6CH4EY5JAP6RJ9VH5K8 shard env1: delivery recorded (4 verdict(s))
run state: COMPLETE (PASS)

$ runcomposer ingest results-env2 --run 01M7X3M6CH4EY5JAP6RJ9VH5K8 --shard env2
warning: marker carries spec_sha256 but the dispatch has no recorded spec hash — verification skipped
warning: shard 'env2' was not declared by dispatch 01M7X3M6MMRYNMAXT04C2Y661F (declared: ['env1']) — recorded, but it completes nothing
01M7X3M6CH4EY5JAP6RJ9VH5K8 shard env2: delivery recorded (4 verdict(s))
run state: COMPLETE (FAIL)
```

The warnings are worth reading. (The first one says the bundle's marker
carries a spec hash but this dispatch handed no document out, so there is
nothing to verify it against — it is skipped, and said so.) This run was composed and never dispatched or
exported, so the first delivery had no dispatch to belong to — one was opened
for it (mode `ingest`), declaring the one shard it arrived with, and the run
was complete the moment that shard was in. The second shard was therefore not
*expected*: it is recorded and it counts (the run is `FAIL` because `env2`
is), but nothing was waiting for it. When you know up front that results come
back in several shards, say so before they arrive — export the run, or
dispatch it to a runner that declares its shards — so that "complete" means
all of them.

A bundle that cannot be read, contradicts its marker, or names a run that does
not exist exits `1` with the reason.

---

## `quarantine`

List, attach, promote or discard quarantined deliveries.

```
runcomposer quarantine list    [--json] [--config PATH]
runcomposer quarantine attach  ENTRY_ID RUN_ID [--format FORMAT] [--json] [--config PATH]
runcomposer quarantine promote ENTRY_ID [--format FORMAT] [--json] [--config PATH]
runcomposer quarantine discard ENTRY_ID [--config PATH]
```

A bundle that arrives over a *transport* — the file-drop inbox, or an
authenticated HTTP push — and cannot be taken in is never dropped and never
silently attached to a run: it is copied to the quarantine
(`core.ingestion.quarantine_dir`) with the reason, for a person to decide.
(`runcomposer ingest` does not quarantine; it tells you directly.) The
quarantine is bounded: beyond `core.ingestion.quarantine_max` entries (default
`100`) adding one evicts the oldest.

Two bundles dropped into a running server's inbox — one whose marker names a
run that does not exist, one with no marker at all:

```console
$ runcomposer quarantine list
ENTRY                         RECEIVED              REASON              VIA        CLAIMED RUN
q-01M7X3HEJ5AM0PXHTJ11H5WFEV  2026-11-20T18:29:44Z  unknown-run         file-drop  01HXNOSUCHRUN0000000000000
q-01M7X3HMEKD8W9MPXCTBH1TPSF  2026-11-20T18:29:50Z  unsolicited         file-drop  -
```

**`attach`** binds an entry to an existing run. It is an explicit decision: the
marker's claims about run and spec hash are overridden by yours, normal
delivery rules apply from there, and the action is recorded on the run as a
`delivery.attached` event.

```console
$ runcomposer quarantine attach q-01M7X3HEJ5AM0PXHTJ11H5WFEV 01M7X3BR2XJ1AGCYK6YNRNVER3
01M7X3BR2XJ1AGCYK6YNRNVER3 shard 1: delivery recorded (1 verdict(s))
run state: COMPLETE (PASS)

$ runcomposer watch 01M7X3BR2XJ1AGCYK6YNRNVER3 --no-follow | tail -3
2026-11-20T18:30:03Z  #65   delivery.recorded  shard=1  outcome=new
2026-11-20T18:30:03Z  #66   run.completed  state=COMPLETE  completion=PASS  1 PASS
2026-11-20T18:30:03Z  #67   delivery.attached  shard=1  reason=unknown-run  message=quarantined delivery attached by an explicit action
```

**`promote`** makes an entry its own run, origin `ingested`, whose planned set
is what the bundle reported. An entry that arrived without saying which result
format it is in (a file drop for no known run) needs to be told:

```console
$ runcomposer quarantine promote q-01M7X3HMEKD8W9MPXCTBH1TPSF
error: quarantine entry q-01M7X3HMEKD8W9MPXCTBH1TPSF does not say which result format it is in — name the format to promote it (`--format` on the CLI, `format` in the API)

$ runcomposer quarantine promote q-01M7X3HMEKD8W9MPXCTBH1TPSF --format runcomposer-verdicts
01M7X3J1WWW9MQVRY2TDGS9WG8 shard 1: delivery recorded (2 verdict(s))
run state: COMPLETE (FAIL)
```

**`discard`** deletes an entry. An id that does not exist exits `1`:

```console
$ runcomposer quarantine discard q-01M7X3J62PZ5E1KAZKADSRDN7E
quarantine entry q-01M7X3J62PZ5E1KAZKADSRDN7E discarded

$ runcomposer quarantine list
quarantine is empty
```

---

## `finalize`

Close a run as it stands.

```
runcomposer finalize RUN_ID [--json] [--config PATH]
```

For a run whose results will not all come: the latest dispatch's outstanding
shards are given up, every planned item that never reported becomes `MISSING`,
and completion is computed from what is there. An export nobody answered:

```console
$ runcomposer finalize 01M7X3EBVENA1KXZ0SF6V2RKKR
run 01M7X3EBVENA1KXZ0SF6V2RKKR: COMPLETE (INCOMPLETE) — 14 MISSING
$ echo $?
1
```

The exit code is the run's (`0` only for `PASS`). `finalize` cannot invent a
result, and it does not interrupt anything:

```console
$ runcomposer finalize 01M7X3EBVENA1KXZ0SF6V2RKKR        # before it was ever dispatched or exported
error: run 01M7X3EBVENA1KXZ0SF6V2RKKR was never dispatched and has no results — there is nothing to finalize
```

A run that a runner is still executing is refused — [`cancel`](#cancel) it
first — and so is a cancelled dispatch.

---

## `delete`

Delete a run, everything recorded for it, and its artifact directory.

```
runcomposer delete RUN_ID [--config PATH]
```

```console
$ runcomposer delete 01M7X3EBVENA1KXZ0SF6V2RKKR
run 01M7X3EBVENA1KXZ0SF6V2RKKR deleted

$ runcomposer show 01M7X3EBVENA1KXZ0SF6V2RKKR
error: unknown run id '01M7X3EBVENA1KXZ0SF6V2RKKR'
```

Not undoable. A run that a runner is executing right now is refused until it
is cancelled or over.

---

## `gc`

Apply retention. Safe to run from cron; safe to run twice.

```
runcomposer gc [--json] [--config PATH]
```

The policy lives in `core.retention` and `core.ingestion`
([DESIGN.md](../DESIGN.md) §6.4), and `gc` is what enforces it:

```console
$ runcomposer gc
quarantine: removed 0 entr(y/ies) beyond the configured bound
inbox/processed: removed 0 expired bundle(s)
store: pruned 0 run(s) past retention
artifacts: removed 0 run director(y/ies)
```

- **Runs** created more than `max_age_days` ago (default `90`), and runs beyond
  the newest `max_runs` (default: no limit), are pruned with everything
  recorded for them. `max_age_days: null` means nothing expires by age.
- **Artifacts live and die with their run**: a pruned run's directory under
  `core.artifact_dir` goes with it, and a run directory that belongs to no
  stored run is swept. A reference whose file is gone answers `404`.
- **The quarantine** is trimmed to `quarantine_max` (it is also bounded
  whenever something is added).
- **Processed inbox bundles** older than `max_age_days` are removed.
- **Private directories** (`core.private_dir`) of runs that are gone are
  swept, like their artifact directories.
- **Dispatches waiting for results too long** are expired, when
  `core.dispatch.awaiting_timeout_s` is set (default: never). `serve` does
  this on its own; `gc` does it once, for a deployment without a server.

An export nobody answered, with `awaiting_timeout_s: 2` in the config:

```console
$ runcomposer spec Payments-Refunds --title "Refunds, handed out" --format json -o refunds.json --export --token-file refunds.token --config config.yaml
spec written to refunds.json
run: 01M7XVDHESSHQ28RHDJGBB5B93 (state COMPOSED)
export dispatch: 01M7XVDHG93S9NNA53FDWDKAEQ (1 shard(s) declared, state AWAITING_RESULTS)
spec sha256: ea93d83211eab3d14fcd2bdaf8f07e80eccc3bcf197be57c7628804ccb77724c
ingest token written to refunds.token

$ runcomposer gc --config config.yaml           # a few seconds later
WARNING runcomposer.expiry: dispatch 01M7XVDHG93S9NNA53FDWDKAEQ of run 01M7XVDHESSHQ28RHDJGBB5B93 expired while awaiting results
quarantine: removed 0 entr(y/ies) beyond the configured bound
inbox/processed: removed 0 expired bundle(s)
store: pruned 0 run(s) past retention
artifacts: removed 0 run director(y/ies)
dispatches: expired 1 that waited for results longer than core.dispatch.awaiting_timeout_s

$ runcomposer show 01M7XVDHESSHQ28RHDJGBB5B93 --config config.yaml
run 01M7XVDHESSHQ28RHDJGBB5B93  Refunds, handed out
state: FAILED — 4 MISSING
planned: 4 item(s); created 2026-11-21T01:27:01Z; origin cli; labels {}
dispatch 01M7XVDHG93S9NNA53FDWDKAEQ  export  FAILED  4 MISSING
  error: expired: no results for 5s, longer than core.dispatch.awaiting_timeout_s (2s) — outstanding shard(s): ['1']. Results that still arrive are taken in and may complete it
  shard 1: outstanding — no verdicts
  reported by no shard: 4 MISSING
```

The `run.failed` event carries `expired`, `waited_s`,
`awaiting_timeout_s` and `outstanding_shards`. `FAILED` is not final:
results that arrive afterwards are taken in, and complete the dispatch —
here, the bundle `runcomposer-exec` wrote from `refunds.json`:

```console
$ runcomposer ingest results --config config.yaml
01M7XVDHESSHQ28RHDJGBB5B93 shard 1: delivery recorded (4 verdict(s))
run state: COMPLETE (PASS)
```

The clock restarts with every delivery, so a job that reports shard after
shard is not expired between two of them. `--json` reports the expired
dispatches under `expired_dispatches`.

---

## `history`

The duration history runners balance their plans with: seed it from a file,
and read it as a planner sees it.

```
runcomposer history import FILE [--label KEY=VALUE ...] [--json] [--config PATH]
runcomposer history durations [--label KEY=VALUE ...] [--last N] [--status STATUS ...]
                              [--json] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `FILE` | — | `import`: a CSV file whose header names an `item` column and a `duration_ms` (or `duration_s`) column — other columns are ignored — or JSON: `{"durations": {"<item>": <ms>, …}}`, or a list of `{"item": …, "duration_ms": …}`. `item` is an item id or any native name the catalog resolves. |
| `--label KEY=VALUE` | none | `import`: store the seeds under these labels, as a run's labels scope its history — give what your runner's `history_selector` asks for. `durations`: only history of runs (and seeds) carrying them. Repeatable. |
| `--last N` | `5` | `durations`: the mean over the last N dispatches that delivered. |
| `--status STATUS` | any | `durations`: only executions that ended with this status, repeatable — `--status PASS` is what a runner with `history_statuses: [PASS]` sees. |
| `--json` | off | `import`: `{imported, unknown, without_duration, labels}`. `durations`: `{durations, items}`. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

A fresh store knows nothing about durations, so the first plans are
round-robin. A deployment that already knows how long its tests take — from
a CI server's reports, from an earlier tool — can start balanced instead.
The durations of the [`plan`](#plan) sample:

```console
$ cat cart-durations.csv
item,duration_s
Tests.Cart.Add To Cart Increases The Count,12
Tests.Cart.Quantity Cannot Go Below One,4
Tests.Cart.Promo Code Reduces The Total,9
Tests.Cart.Two Promo Codes Do Not Stack,8
Tests.Cart.Remove From Cart Decreases The Count,11
Tests.Cart.Cart Subtotal Sums Line Items,3
Tests.Cart.Expired Promo Code Is Refused,6
Tests.Cart.Free Gift Threshold Is Honoured,5

$ runcomposer history import cart-durations.csv --config robot-pool/config.yaml
imported 8 duration(s)
```

Every name goes through the catalog. What it does not know is reported and
not stored, and a duration of 0 is skipped — 0 means "not known", to the
store and to the planner alike. A file that imports nothing it was asked to
exits `1`. The sample file of the example world, imported into a freshly
seeded demo workspace (two of its names are not in the demo corpus), and a
file with nothing usable in it:

```console
$ runcomposer history import durations.csv --config demo/config.yaml
imported 7 duration(s)
  not in the catalog, skipped: Shop.Checkout.Cart.T001
  not in the catalog, skipped: Shop.Checkout.Cart.T002

$ runcomposer history import unknown.csv --config demo/config.yaml
imported 0 duration(s)
  skipped 1 with a duration of 0 (0 means: not known)
  not in the catalog, skipped: Shop.No.Such.T999
$ echo $?
1
```

**A seed answers only for an item nothing was measured for** — the first
real execution takes over, and a seed never dilutes a measurement.
`durations` prints the means as a planner sees them, longest first:

```console
$ runcomposer history durations --config demo/config.yaml | head -4
      3969 ms  Shop.Payments.Wallets.T002
      3897 ms  Shop.Payments.Invoicing.T003
      3880 ms  Shop.Auth.Login.T002
      3861 ms  Shop.Catalog.Search.T005
```

With `--status`, "measured" means measured with that status. In the demo's
history `Shop.Payments.Cards.T001` has one execution, and it failed: so its
measured mean answers without a filter, and restricted to passes its seed
does (6100 ms in `durations.csv`):

```console
$ runcomposer history durations --config demo/config.yaml | grep Cards.T001
      3016 ms  Shop.Payments.Cards.T001

$ runcomposer history durations --status PASS --config demo/config.yaml | grep Cards.T001
      6100 ms  Shop.Payments.Cards.T001
```

The file is plain on purpose: a CSV export of any report with a test name
and a time is enough. `examples/history/durations.csv` is the sample.

---

## `doctor`

Everything that can be wrong with a deployment before a run is started, in one
report.

```
runcomposer doctor [-v] [--json] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `-v`, `--details` | off | Also print every runner's health data — what its checks found. The global `-v` does the same. |
| `--json` | off | [One JSON document](#--json--one-document-for-a-script); it always carries the health data. |
| `--config PATH` | `./config.yaml` if present | [See above](#--config--the-only-way-to-point-at-another-config-file). |

The samples below were taken in `/srv`; the config file's path, and every
path anchored to it, is printed absolute:

```console
$ runcomposer doctor --config shop/config.yaml
runcomposer 0.2.0
config: /srv/shop/config.yaml
installed plugins:
  observers  command                runcomposer 0.2.0
  observers  jsonl                  runcomposer 0.2.0
  observers  webhook                runcomposer 0.2.0
  parsers    junit-xml              runcomposer 0.2.0
  parsers    robot-output-xml       runcomposer 0.2.0
  parsers    runcomposer-verdicts   runcomposer 0.2.0
  runners    command                runcomposer 0.2.0
  runners    demo                   runcomposer 0.2.0
  runners    jenkins                runcomposer 0.2.0
  runners    robot-pool             runcomposer 0.2.0
  sources    demo-corpus            runcomposer 0.2.0
  sources    manifest               runcomposer 0.2.0
  sources    robotframework         runcomposer 0.2.0
  stores     sqlite                 runcomposer 0.2.0
configured (what this deployment loads):
  store      sqlite                 runcomposer.plugins.sqlite_store:SqliteRunStore  runcomposer 0.2.0
  sources    robotframework         runcomposer.plugins.robot_source:RobotFrameworkSource  runcomposer 0.2.0
  runners    robot-pool             runcomposer.plugins.robot_pool:RobotPoolRunner  runcomposer 0.2.0
catalog: 58 items, 50 tags, sources ['robotframework'], snapshot sha256:21ee74fed1fc…
runners:
  robot-pool (robot-pool): ok — Robot Framework 7.5.1
      --param partitions=<multi_choice> ['env1', 'env2']  default ['env1', 'env2']
      --param allow_drift=<boolean>  default False
taxonomy: bundled demo taxonomy, 11 leaf pattern(s); 39 unclaimed tag(s), 0 dead leaf/leaves

no problems found
```

What it checks, in order:

1. **The config** — loaded, and *every configured plugin built once*: store,
   sources, runners, parsers, observers, and the taxonomy read. An unknown
   plugin, an option the plugin does not take, a missing environment variable
   behind a `${env:NAME}` value, a malformed taxonomy — each is reported here.
2. **Plugins** — every entry-point plugin *installed* in this environment,
   with the distribution that provides it and its version (is your connector
   package installed where runcomposer runs?), and what this deployment has
   *configured*: each entry of the config with the class behind it —
   `module:` plugins included — and where that class comes from.
3. **The catalog** — size, sources, snapshot; ids that more than one item
   carries are a problem.
4. **Runners** — each configured runner under the id it is dispatched by, its
   health probed now, and the per-run parameters it accepts, spelled as the
   `--param` flags you would type; for a `robot-pool` runner, whether every
   catalog item lies under its suite roots.
5. **Taxonomy drift** — the counts of [`taxonomy-check`](#taxonomy-check); a
   problem only for a taxonomy you configured.

It also performs the one repair that is always right: a dispatch that a
process which no longer exists left `DISPATCHED` or `RUNNING` is marked
`STALE` (as `serve` does at startup and every
`core.dispatch.recovery_interval_s` while it runs), and reported.

### `-v` — what the runners' health checks found

A runner's health carries data of its own, shown verbatim: `robot-pool` its
Robot Framework version, workers, suite roots, partitions and a list of
checks — Robot Framework installed, each suite root found, the rows of its
`health_checks`, and, added by `doctor`, whether every catalog item lies
under the suite roots; `jenkins` the
server version, the job URL and its checks. A list of checks reads as a
checklist (`[ok  ] server reachable`, `[FAIL] job … readable`). The
[`examples/robot-pool`](../examples/robot-pool/config.yaml) config, which
configures the plugin three times:

```console
$ runcomposer doctor -v --config robot-pool/config.yaml
runcomposer 0.2.0
config: /srv/robot-pool/config.yaml
installed plugins:
…
configured (what this deployment loads):
  store      sqlite                 runcomposer.plugins.sqlite_store:SqliteRunStore  runcomposer 0.2.0
  sources    robot-shop             runcomposer.plugins.robot_source:RobotFrameworkSource  runcomposer 0.2.0
  runners    robot-pool             runcomposer.plugins.robot_pool:RobotPoolRunner  runcomposer 0.2.0
  runners    robot-lanes            runcomposer.plugins.robot_pool:RobotPoolRunner  runcomposer 0.2.0
  runners    robot-regions          runcomposer.plugins.robot_pool:RobotPoolRunner  runcomposer 0.2.0
catalog: 58 items, 50 tags, sources ['robot-shop'], snapshot sha256:21ee74fed1fc…
runners:
  robot-pool (robot-pool): ok — Robot Framework 7.5.1
      robot_version: 7.5.1
      max_workers: 4
      suite_roots:
        - /srv/robot-pool/../robot-shop/tests
      partitions:
        - eu
        - us
      partition_mode: each
      checks:
        [ok  ] Robot Framework installed — Robot Framework 7.5.1
        [ok  ] suite root /srv/robot-pool/../robot-shop/tests — found
        [ok  ] catalog items under the suite roots — all 58 catalog item(s) are under the suite roots
      --param partitions=<multi_choice> ['eu', 'us']  default ['eu', 'us']
      --param allow_drift=<boolean>  default False
  robot-lanes (robot-pool): ok — Robot Framework 7.5.1
      robot_version: 7.5.1
      max_workers: 2
      suite_roots:
        - /srv/robot-pool/../robot-shop/tests
      partitions:
        - eu
        - us
      partition_mode: split
      checks:
        [ok  ] Robot Framework installed — Robot Framework 7.5.1
        [ok  ] suite root /srv/robot-pool/../robot-shop/tests — found
        [ok  ] catalog items under the suite roots — all 58 catalog item(s) are under the suite roots
      --param partitions=<multi_choice> ['eu', 'us']  default ['eu', 'us']
      --param allow_drift=<boolean>  default False
      --param variables=<mapping>  default None
      --param browser=<choice> ['firefox', 'chromium']  default 'firefox'
  robot-regions (robot-pool): ok — Robot Framework 7.5.1
      robot_version: 7.5.1
      max_workers: 4
      suite_roots:
        - /srv/robot-pool/../robot-shop/tests
      partitions:
        - eu
        - us
      partition_mode: each
      checks:
        [ok  ] Robot Framework installed — Robot Framework 7.5.1
        [ok  ] suite root /srv/robot-pool/../robot-shop/tests — found
        [ok  ] catalog items under the suite roots — all 58 catalog item(s) are under the suite roots
      --param partitions=<multi_choice> ['eu', 'us']  default ['eu', 'us']
      --param partition_mode=<choice> ['split', 'each']  default 'each'
      --param allow_drift=<boolean>  default False
      --param variables=<mapping>  default None
taxonomy: bundled demo taxonomy, 11 leaf pattern(s); 39 unclaimed tag(s), 0 dead leaf/leaves

no problems found
```

The `--param` lines name every kind a runner may declare — `string`, `text`,
`integer`, `boolean`, `choice`, `multi_choice`, `mapping`; a `mapping` is
given as YAML, its values as text: `--param 'variables={LOCALE: de}'`.

The suite-root row is `doctor`'s own: for every `robot-pool` runner it holds
the whole catalog against the runner's suite roots — the check a preview and
a dispatch make for their selection — and an item that lies under none of
them is a `[FAIL]` row and a problem (exit `1`).

`doctor` exits `1` when it found a problem, with the list at the end:

```console
$ runcomposer doctor --config envs/broken/config.yaml
runcomposer 0.2.0
config: /srv/envs/broken/config.yaml
installed plugins:
…

1 problem(s):
  - configuration: runners.demo: unknown option(s) ['sed'] — DemoRunner accepts: ['delay_s', 'seed']
```

---

## `serve`

Run the API and the bundled web UI.

```
runcomposer serve [--host HOST] [--port PORT] [--log-level LEVEL] [--config PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `--host HOST` | `core.api.host`, else `127.0.0.1` | Bind address. It also decides which host names are answered while `core.api.hosts` is unset ([below](#the-names-it-answers-to)). |
| `--port PORT` | `core.api.port`, else `8100` | Bind port. |
| `--log-level LEVEL` | `info` | `debug`, `info`, `warning` or `error` — what the `runcomposer.*` loggers and the HTTP server print. |
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
is built, and the taxonomy is validated before the socket opens. A typo in a
plugin option exits `2` naming what the plugin accepts, rather than booting a
server that fails at the first dispatch:

```console
$ runcomposer serve --config envs/broken/config.yaml
error: runners.demo: unknown option(s) ['sed'] — DemoRunner accepts: ['delay_s', 'seed']
```

Startup also reconciles what a previous server left behind: a dispatch that
was executing when that process died is marked `STALE`, with an event saying
so.

While it runs, the server:

- **executes dispatches off the request path** — `POST /api/v1/runs` with a
  dispatch answers at once, and at most `core.dispatch.max_concurrent`
  (default `2`) dispatches execute at a time;
- **watches the file-drop inbox** (`core.ingestion.inbox`, default
  `results_inbox/` next to the config; `inbox: null` turns it off). Each
  subdirectory dropped there is one bundle. It is taken in when it is
  complete: at once if it contains the `runcomposer_run.json` marker or an
  empty `.done` file, otherwise once nothing in it has changed for
  `core.ingestion.settle_s` (default: two poll intervals). Ingested or
  quarantined, the bundle is then moved to `processed/`;
- **expires dispatches that wait for results too long**, when
  `core.dispatch.awaiting_timeout_s` is set ([`gc`](#gc) shows what that
  looks like);
- **repeats the startup reconciliation** every
  `core.dispatch.recovery_interval_s` seconds (default `15`; `null` turns
  it off): a dispatch that another process on the same store — a
  foreground `runcomposer run` — was executing when it died becomes
  `STALE` without a restart. A dispatch whose process is alive, or that
  the server executes itself, is never touched
  (`tests/test_recovery_periodic.py`);
- **logs** one line per run event (stderr), next to the HTTP access log:

  ```
  2026-11-20T18:29:31Z INFO    runcomposer.events: run.running run=01M7X3H2HEJQN3WD6176WB48R8 dispatch=01M7X3H2HFPK2YAWR2DDNW98A9 state=RUNNING
  2026-11-20T18:29:32Z INFO    runcomposer.events: run.cancelled run=01M7X3H2HEJQN3WD6176WB48R8 dispatch=01M7X3H2HFPK2YAWR2DDNW98A9 state=CANCELLED error=cancelled on request
  2026-11-20T18:29:44Z WARNING runcomposer.inbox: inbox bundle lost quarantined (unknown-run): unknown run id '01HXNOSUCHRUN0000000000000' — refusing bundle
  ```

With no config at all, `serve` comes up on the demo corpus with the `demo`
runner, so the UI is never empty on a fresh install:

```console
$ curl -s localhost:8100/api/v1/health
{"status":"ok","version":"0.2.0"}
$ curl -s localhost:8100/api/v1/runners
[{"id":"demo","plugin":"demo","title":"Demo (simulated)","description":"Simulates an execution: no test is run, verdicts are derived from a seed.","capabilities":["simulated","live_status","cancel","plan"],"result_format":"runcomposer-verdicts","parameters":[{"name":"seed","kind":"string","label":"Seed","help":"Same seed, same simulated verdicts; change it to see results flip.","default":"demo","choices":[],"required":false,"pattern":null,"key_pattern":null}],"health":{"status":"ok","detail":"simulated — always available","data":{}}}]
$ curl -s localhost:8100/api/v1/presets
{"presets":[],"default_filter":null,"quick_filters":[]}
```

`/openapi.json` describes the API and is always served; the `/docs` and
`/redoc` viewers answer `404` unless `core.api.docs: true`, since they load
their script from a public CDN ([DESIGN.md](../DESIGN.md) §9). A
state-changing request a browser sends for another site's page is refused
before any route sees it — curl, the CLI and an executor pushing with its
token send no `Origin` and are unaffected:

```console
$ curl -s -X POST -H 'Origin: https://elsewhere.example' -H 'Content-Type: application/json' localhost:8100/api/v1/runs -d '{"selection":{"tag_filter":"Smoke"}}'
{"detail":"refused a state-changing request from a page on 'https://elsewhere.example': it is neither this server's own origin nor listed in core.api.cors","code":"cross-site-request"}
```

`Ctrl-C` shuts down in order: dispatches still executing are cancelled, the
inbox watcher stops. A server that is *killed* leaves its dispatches to the
next startup's reconciliation (`STALE`).

`--host 0.0.0.0` is what you need when something outside the machine has to
reach the push endpoints — a CI job posting results or progress back, for
instance; set `core.api.hosts` to the names it is reached by ([below](#the-names-it-answers-to)). There is no authentication in front of the API beyond the
per-dispatch ingest token on the push endpoints (`…/results`, `…/live`,
`…/log`; [DESIGN.md](../DESIGN.md) §5, §13); put a reverse proxy in front of
anything public.

### The names it answers to

A request for a host name the server does not answer to is refused `400`
(`unknown-host`) before anything else sees it, whatever its method — the
answer to DNS rebinding, where a page on a name its author controls is
resolved to this server's address ([SECURITY.md](../SECURITY.md) §6).
`core.api.hosts` lists the names: `runcomposer.shop.example` (any port),
`localhost:8100` (that port only), `*.shop.example` (every name below it),
`*` (every name). While it is unset, the address `serve` binds decides: on
a loopback address, as by default, only `localhost`, `127.0.0.1`, `[::1]`
and that address are answered; on any other (`--host 0.0.0.0`) every name
is, until the list is set. With `core.api: { port: 8237 }`:

```console
$ curl -s localhost:8237/api/v1/health
{"status":"ok","version":"0.2.0"}
$ curl -s -H 'Host: runcomposer.shop.example' localhost:8237/api/v1/health
{"detail":"refused a request for host 'runcomposer.shop.example': it is not a name this server answers to (core.api.hosts; unset, a server bound to a loopback address answers to localhost, 127.0.0.1 and [::1])","code":"unknown-host"}
```

So a proxy that passes its public name through to a server on a loopback
address needs that name listed. The list replaces the default — with
`core.api: { port: 8237, hosts: [runcomposer.shop.example, localhost] }`:

```console
$ curl -s -H 'Host: runcomposer.shop.example' localhost:8237/api/v1/health
{"status":"ok","version":"0.2.0"}
$ curl -s -w ' %{http_code}' 127.0.0.1:8237/api/v1/health
{"detail":"refused a request for host '127.0.0.1:8237': it is not a name this server answers to (core.api.hosts; unset, a server bound to a loopback address answers to localhost, 127.0.0.1 and [::1])","code":"unknown-host"} 400
```

An empty list or an entry that is not a host name refuses the start
(`tests/test_api_hosts.py`).

---

## `runcomposer-exec`

The other executable: a **single stdlib-only file** that executes a spec
wherever your tests actually live. Vendorable by copying
`src/runcomposer_exec.py` — no install, no venv, no network
([DESIGN.md](../DESIGN.md) §6.2c).

```
runcomposer-exec SPEC (--simulate | --command TEMPLATE)
                 [--out DIR] [--dispatch ID] [--shard LABEL] [--seed SEED]
                 [--ok-exit-codes CODES]
                 [--callback URL [--token TOKEN] [--push]]
                 [--ca-bundle PATH] [--verify-tls | --no-verify-tls]
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
| `--ok-exit-codes CODES` | `0` | Exit codes of `--command` after which the bundle is complete: `0`, `0,1`, `0-249`. |
| `--callback URL` | none | The run's results URL (`…/api/v1/runs/<id>/results`, as a runner hands it out): report log lines — and with `--simulate` the live status of every item — while executing. [Below](#talking-back----callback-and---push). |
| `--token TOKEN` | `$RUNCOMPOSER_INGEST_TOKEN` | The dispatch's ingest token, for `--callback` and `--push`. |
| `--push` | off | Upload the finished bundle to `--callback` (needs it). Exit `4` when the push is refused or cannot be delivered. |
| `--ca-bundle PATH` | `$RUNCOMPOSER_CA_BUNDLE` | A file of the CA certificates to trust for an `https` `--callback`, instead of the system's — a server behind a private certificate authority. With `--callback`, a file that does not exist stops it before anything executes, and so does, for an `https` callback, one that holds no certificate. |
| `--verify-tls`, `--no-verify-tls` | `$RUNCOMPOSER_VERIFY_TLS` (`true`/`false`, `yes`/`no`, `on`/`off`, `1`/`0`; unset: verify) | `--no-verify-tls` skips the certificate check of an `https` `--callback` — for a lab server only. The flag wins over the variable; any other value of the variable stops it with an `error:` line. |

It executes exactly `selection.materialized.item_ids` and refuses a spec
without one; it refuses any `runspec` MAJOR other than `1`. On success it
writes `runcomposer_run.json` next to the outputs, carrying `run_id` and
`spec_sha256` — the hash of the spec file's bytes — so the bundle can find its
way home and be verified against what was handed out.

**`--simulate` runs no tests.** It is the round-trip proof — it shows that
compose → transport → ingest works before your test framework is involved at
all:

```console
$ runcomposer-exec checkout.json --out sim-results --simulate
runcomposer-exec: simulated 13 items (2 FAIL, 9 PASS, 2 SKIP)
runcomposer-exec: bundle ready at sim-results (marker: runcomposer_run.json)
```

**`--command` is the real thing.** Your script receives a file of item ids and
an output directory, and writes its native result files there:

```console
$ runcomposer-exec spec.json --out results --command "./run-tests.sh {ids_file} {out_dir}"
runcomposer-exec: running: ./run-tests.sh results/item_ids.txt results
ran 17 test(s)
runcomposer-exec: bundle ready at results (marker: runcomposer_run.json)

$ ls results
item_ids.txt
results.json
runcomposer_run.json

$ cat results/runcomposer_run.json
{
  "run_id": "01M7X3BA2EBNAEW1XZXKWXS8JD",
  "spec_sha256": "0683a775a426eb5e335a87015340aff0ae376737e8d86ce129f643aa3903db6b"
}
```

Whatever format your command wrote, name it with `spec --expect-format` when
you compose, so ingestion picks the right parser.

**Only the three placeholders are replaced.** The template is a shell command
line, and shell is full of braces that are not placeholders; everything else
is passed through exactly as written:

```console
$ runcomposer-exec spec.json --out counted \
      --command "wc -l < {ids_file} | awk '{print \$1}' > {out_dir}/count.txt && ./run-tests.sh {ids_file} {out_dir}"
runcomposer-exec: running: wc -l < counted/item_ids.txt | awk '{print $1}' > counted/count.txt && ./run-tests.sh counted/item_ids.txt counted
ran 17 test(s)
runcomposer-exec: bundle ready at counted (marker: runcomposer_run.json)
```

**The marker is written only when the execution succeeded** — it is the
bundle's completion signal (a file-drop inbox takes a bundle in the moment it
sees one), and a failed execution cannot give it. A command that exits with a
code that is not accepted leaves no marker, and its exit code is
`runcomposer-exec`'s:

```console
$ runcomposer-exec spec.json --out broken --command "exit 3"
runcomposer-exec: running: exit 3
runcomposer-exec: the command exited 3 (accepted: 0) — no marker written, broken is NOT a complete bundle
$ echo $?
3
$ ls broken
item_ids.txt
```

Many test frameworks exit non-zero when *tests* fail, which is a result, not a
failure of the execution. Say which codes still mean "the results are valid":

```console
$ runcomposer-exec spec.json --out tolerated \
      --command "./run-tests.sh {ids_file} {out_dir}; exit 3" --ok-exit-codes 0-249
runcomposer-exec: running: ./run-tests.sh tolerated/item_ids.txt tolerated; exit 3
ran 17 test(s)
runcomposer-exec: bundle ready at tolerated (marker: runcomposer_run.json)
$ echo $?
0
```

With the marker written, `runcomposer-exec` exits `0` — whether the tests
passed is in the bundle, and becomes the run's completion when it is ingested.

### Talking back — `--callback` and `--push`

Optional, and still nothing but the stdlib. With `--callback` and the
dispatch's ingest token the consumer reports to a running server while it
works — a log line when it starts and, after a `--command`, one with the
command's exit code; with `--simulate` also each item's live status
(`RUNNING`, then how it ended) — through the token-guarded progress
endpoints ([DESIGN.md](../DESIGN.md) §5). With `--push` it then uploads the
finished bundle to the results endpoint, so no other transport is needed.
The samples use the single file as vendored — a copy of
`src/runcomposer_exec.py` next to the spec — against a `runcomposer serve`
on port 8237:

```console
$ runcomposer spec Payments-Wallets --title "Wallets, pushed back" --format json -o wallets.json --export --token-file wallets.token --config config.yaml
spec written to wallets.json
run: 01M7X9BPCTDN44ET1ZPG9YKM14 (state COMPOSED)
export dispatch: 01M7X9BPD1R6V42EB24Z365MSF (1 shard(s) declared, state AWAITING_RESULTS)
spec sha256: 3f6f19aa6d96bfd76411e7278fe47bdcebc0e14386dfbba9dcecadaf1ca19301
ingest token written to wallets.token

$ python3 runcomposer_exec.py wallets.json --out wallets --simulate --dispatch 01M7X9BPD1R6V42EB24Z365MSF \
      --callback http://127.0.0.1:8237/api/v1/runs/01M7X9BPCTDN44ET1ZPG9YKM14/results --token "$(cat wallets.token)" --push
runcomposer-exec: simulated 4 items (4 PASS)
runcomposer-exec: bundle ready at wallets (marker: runcomposer_run.json)
runcomposer-exec: bundle pushed: {"run_id":"01M7X9BPCTDN44ET1ZPG9YKM14","dispatch_id":"01M7X9BPD1R6V42EB24Z365MSF","shard":"1","outcome":"new","verdict_count":4,"run_state":"COMPLETE","completion":"PASS","warnings":[]}

$ runcomposer watch 01M7X9BPCTDN44ET1ZPG9YKM14 --no-follow --config config.yaml
2026-11-20T20:11:27Z  #1    run.composed
2026-11-20T20:11:27Z  #2    run.awaiting_results  state=AWAITING_RESULTS
2026-11-20T20:11:53Z  #3    log  message=runcomposer-exec: shard 1: 4 item(s) to execute
2026-11-20T20:11:53Z  #4    delivery.recorded  shard=1  outcome=new
2026-11-20T20:11:53Z  #5    run.completed  state=COMPLETE  completion=PASS  4 PASS
```

Progress is best-effort: a report that cannot be delivered is said once and
dropped, and the execution goes on. The push is not — a push that is refused
(or cannot be delivered) exits `4`, with the bundle and its marker left on
disk for another transport. Here with the token of another dispatch:

```console
$ python3 runcomposer_exec.py invoicing.json --out invoicing --simulate \
      --callback http://127.0.0.1:8237/api/v1/runs/01M7X9CP9ECYE6T9S4RP8AE7MM/results --token "$(cat wallets.token)" --push
runcomposer-exec: progress could not be reported (HTTP Error 403: Forbidden)
runcomposer-exec: simulated 3 items (3 PASS)
runcomposer-exec: bundle ready at invoicing (marker: runcomposer_run.json)
runcomposer-exec: the push was refused (403): {"detail":"the ingest token does not match this run","code":"token-rejected"}
$ echo $?
4
```

So the exit codes of `runcomposer-exec` are: `0` the bundle is complete
(and pushed, with `--push`); the command's own exit code (or `1`) when the
command failed and no marker was written; `4` the push was refused or could
not be delivered; `2` for an unknown or missing flag. A spec it cannot
execute, or `--push` without `--callback`, exits `1` with an `error:` line.

Over `https` the server's certificate is checked against the system's trust
store. A server behind a private certificate authority needs `--ca-bundle`
(or `RUNCOMPOSER_CA_BUNDLE` in the job's environment); without it the
progress report says once that it could not be delivered, and a `--push`
exits `4` with the reason and both ways out — `--ca-bundle`, or
`--no-verify-tls` for a server whose certificate nobody signed
(`tests/test_remote_tls.py::TestTheConsumerOverHttps`). The Robot listener
`RemoteLiveStatusListener` reads the same two variables.

---

## The export loop, end to end

The commands in the order they are actually used
([ADOPTING.md](../ADOPTING.md) §6 has the reasoning):

```bash
# 1 — here: look before you compose
runcomposer compile 'Payments' --config envs/staging/config.yaml

# 2 — here: compose, and hand the document out (an export dispatch)
runcomposer spec 'Payments' --title "Nightly payments" --label suite=nightly \
    --format json -o spec.json --export --config envs/staging/config.yaml

# 3 — there: one vendored file, on the machine that has the tests
python3 runcomposer_exec.py spec.json --out results \
    --command "./run-tests.sh {ids_file} {out_dir}"

# 4 — here: however the bundle travelled back
runcomposer ingest results --config envs/staging/config.yaml

# 5 — here: read it
runcomposer runs --label suite=nightly --config envs/staging/config.yaml
runcomposer show <run-id> --config envs/staging/config.yaml
runcomposer export <run-id> --format ctrf --config envs/staging/config.yaml
```

Step 4 has two other spellings that need no command at all: copy the bundle
into the inbox of a running `runcomposer serve` (the marker tells it the
bundle is complete), or have the executing side `POST` it to
`/api/v1/runs/<run-id>/results` with the ingest token step 2 printed —
which is what [`runcomposer-exec --callback … --push`](#talking-back----callback-and---push)
does. And when
the tests can run on the machine runcomposer is on, the whole loop is one
command: [`runcomposer run`](#run).
