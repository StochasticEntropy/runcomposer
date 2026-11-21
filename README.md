# runcomposer

[![CI](https://github.com/StochasticEntropy/runcomposer/actions/workflows/ci.yml/badge.svg)](https://github.com/StochasticEntropy/runcomposer/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10 – 3.13](https://img.shields.io/badge/python-3.10%20%E2%80%93%203.13-blue.svg)](pyproject.toml)

**runcomposer** is an open-source, tag-based test run composer & orchestrator:
see your test corpus through a curated tag taxonomy, compose precise
selections with a real filter language, and turn a selection into a
reproducible, portable **run spec** that any executor can fulfill — with
results flowing back from any transport into a run history that feeds new
selections ("rerun what failed").

**[stochasticentropy.github.io/runcomposer](https://stochasticentropy.github.io/runcomposer/)** —
how it works, in one page.

### Or watch it

- **[The 69-second teaser](https://stochasticentropy.github.io/runcomposer/#watch)** —
  narrated: why precise selection is awkward, and what changes when the
  selection becomes a document.
- **[The 6-minute explainer](https://stochasticentropy.github.io/runcomposer/#watch)** —
  seven chapters: the catalog, composing a selection, freezing it into a spec,
  the document itself, the three ways to execute it, results returning from any
  transport, and rerunning what failed.

Version 0.2.0 — a reworked backbone: asynchronous dispatch with an observable
lifecycle, one runner contract every executor implements, a conformance kit
and a copy-to-start package for writing your own connector
([CHANGELOG](CHANGELOG.md)). Read [ADOPTING.md](ADOPTING.md) to connect it to
your own corpus and machines, the [connector guide](docs/connectors.md) when
you write plugins, [DESIGN.md](DESIGN.md) for the architecture and the
reasoning behind it, or [CONTRIBUTING.md](CONTRIBUTING.md) to work on it.

## Quickstart

```bash
pip install runcomposer          # from a clone: pip install .

runcomposer demo                 # seed the neutral web-shop demo end to end
runcomposer run Checkout --runner demo --config runcomposer-demo/config.yaml
runcomposer runs --config runcomposer-demo/config.yaml --failed-in latest --label suite=nightly
runcomposer serve --config runcomposer-demo/config.yaml   # API (+ web UI) at http://127.0.0.1:8100
# or: docker build -t runcomposer . && docker run --rm -p 8100:8100 runcomposer
```

`demo` seeds a real store — five completed runs with per-item verdicts — into
`./runcomposer-demo/`, and prints the commands that read it back, including
"rerun what failed". The `demo` runner is **simulated** (it says so): verdicts
come from a seed, so the loop can be shown without a test framework. `run`
composes, dispatches, waits and prints the outcome; it exits `0` only when the
run is `PASS`. Everything the demo writes is in that one directory, so
`rm -rf runcomposer-demo` undoes it. `python -m runcomposer` is the same
command line. The image runs runcomposer under an init (`tini`) as process
1, which forwards signals and reaps what a stopped worker leaves behind, so
`docker run --init` is not needed.

## What it does

**Catalog.** A *test source* enumerates your tests as items — an opaque stable
id plus tags — and content-hashes the catalog. Two ship: `manifest` (a plain
JSON or YAML list, zero dependencies, the adoption path for any framework) and
`robotframework` (walks `.robot` files, ids are longnames).

**Compose.** Navigate a curated taxonomy, build a filter, watch the preview
recompile. The filter language is small and lossless: a bare word is a literal
tag, `prefix:Checkout-` is sugar for an anchored regex, `regex:` is the escape
hatch, and the operators are `AND`, `OR`, `NOT`. One line of text says it —
`runcomposer compile 'Payments AND NOT (Smoke OR prefix:Quarantine-)'` — read
alike by the web UI, the CLI and the API.

**Freeze.** The filter is compiled against the catalog snapshot and the
resulting item list is written *into* a versioned run spec document — so a
dispatched spec is self-sufficient, and the snapshot makes corpus drift
detectable. Core sections are generic and closed; exactly one section,
`runner`, is open, and the core never looks inside it. `runcomposer validate`
checks a document against the published JSON Schema.

**Execute — four ways, one contract.** In worker processes on this machine
with `robot-pool` (Robot Framework: partition fan-out, duration-balanced
chunking, live verdicts, reruns, readable logs, drift refusal); a command per
shard with `command`, for any framework with a command line; by triggering an
existing parameterized Jenkins job with `jenkins` (results by callback or by
polling the build); or on *your own agent anywhere* by exporting the document
to the vendorable `runcomposer-exec`. A dispatch runs off the request path:
the API answers at once, and the run is followed through its lifecycle
(`DISPATCHED → RUNNING → COMPLETE`, or `AWAITING_RESULTS`, `FAILED`,
`CANCELLED`, `STALE`) and its event stream, can be cancelled, and tells an
observer (`webhook`, `jsonl`, `command`) when it is over. Each way has a
runnable example run end to end by a test
([the connector guide, §8](docs/connectors.md#8-four-ways-to-execute)).
The two reference runners have modes beyond the basics:

- `robot-pool` can *distribute* a selection over its partitions instead of
  repeating it on each (`partition_mode: split`), make every bounded piece a
  shard of its own that delivers when done (`chunk_shards`), run
  preparation steps that see the plan before anything starts (`pre_dispatch`),
  keep a partition to one chunk at a time (`max_chunks_per_partition`),
  protect variables a run must not set, and merge each group's results and
  the whole run's into one log (`merge_groups`); shrink every `output.xml`
  in the worker before anything reads it (`clean_output` — Robot's
  `removekeywords` shapes only the log and report), run steps of your own
  after every execution (`post_execution`), and tell the workers where to
  stand (`worker_cwd`, `worker_env`).
- `jenkins` can drive a job that fans out into several shards in one build,
  pass the document compressed (`{spec_b64}`), compute build parameters from
  the selection (`field_providers`), and read results from any URL below the
  build, such as its console log (`result_urls`, `postprocess`), ride out a
  Jenkins restart (`outage_tolerance_s`), and check that the job declares
  every parameter it is sent (`check_parameters`).

**Look before you start.** `runcomposer plan Cart --runner robot-lanes
--config examples/robot-pool/config.yaml` (or `POST /api/v1/plan`) shows
the plan of a dispatch — every test with the
shard and group it would land in, with duration estimates, or the build parameters a
Jenkins trigger would send — and executes, stores and triggers nothing.
While a run executes, items show as `RUNNING`; an executor elsewhere reports
progress with its dispatch's token.

**Connect your own.** Everything a connector may import is one package,
`runcomposer.sdk`, with two toolkits — `runcomposer.sdk.robot` and
`runcomposer.sdk.jenkins`, the parts the reference runners are built from,
to compose instead of rewrite; `runcomposer.testing` checks your source,
runner, parser, store or observer against the contract and runs it inside
a real runcomposer for your tests; and
[examples/connector-template](examples/connector-template) is an installable
connector package to copy. Configuration validates every plugin's options at
startup, takes values from the environment as `${env:NAME}` (a secret,
blanked out of whatever is recorded) or `${env-public:NAME}`, merges several
sources into one catalog, and keeps a `connectors:` namespace for your
package's own settings.

**Ingest.** Results return over a token-guarded HTTP push, a watched file-drop
directory, or `runcomposer ingest` on the command line. Redelivery rules are
explicit — the same bundle again is a no-op, a different bundle replaces that
shard. Completion is computed against what was planned: an item that never
reported is `MISSING`, and its run is `INCOMPLETE`, not green. A dropped bundle
that cannot be matched to a run lands in a visible quarantine rather than
quietly entering history.

**Reuse.** Once runs accrue, history becomes a selection source:
`runcomposer runs --failed-in latest --label suite=nightly`, `spec
--from-history 'failed@latest?suite=nightly'`, and `rerun --scope failed`
(a run's rerun in the UI). The label scope keeps "latest" from meaning
somebody else's run, and the resolved reference run is recorded in
`selection.derived_from`. Durations balance the next plan; a fresh store can
be seeded with known ones (`runcomposer history import`).

**Hand off.** `runcomposer export <run> --format ctrf` for tools that speak
CTRF; `robot-output-xml` and `junit-xml` parse results coming the other way
(both refuse documents carrying entity or DTD declarations).

The web UI ships pre-built inside the wheel in English and German, so
evaluating it needs no Node toolchain. A server answers only to the host
names in `core.api.hosts` — unset, a server on a loopback address answers
to the loopback names only, which keeps DNS rebinding out of a local
setup. `/openapi.json` describes the API; the
`/docs` and `/redoc` viewers are off unless `core.api.docs: true`, since they
load their script from a public CDN. Persistence is sqlite by default — safe
for the threads of a server and for several processes on one file, because
every lifecycle decision is one write transaction; it needs SQLite 3.24.0 or
newer with the JSON functions, and refuses to open on an older one, naming
both versions — and `runcomposer gc` keeps runs, quarantine and
artifacts bounded.

## The export round trip

Compose a run spec, execute it *anywhere* with the vendorable single-file
consumer, and ingest the results bundle back — no coupling between composer
and executor beyond the spec document itself:

```bash
runcomposer spec 'Regression' --title "Nightly" --format json -o spec.json --export
runcomposer-exec spec.json --out results --simulate   # or --command "your-runner {ids_file}"
runcomposer ingest results                            # marker-correlated, idempotent
runcomposer runs                                      # → COMPLETE (PASS/FAIL)
```

`runcomposer-exec` is a single stdlib-only Python file — copy it next to any
executor (CI checkout, air-gapped host) and it renders the spec's materialized
item list, runs your command, and writes the `runcomposer_run.json` correlation
marker beside the results.

[examples/remote-agent](examples/remote-agent) turns that into a complete
adopter kit for the remote round trip: a documented config, an agent that
needs only `python3` + `robot` on the executing machine, and a
transport-agnostic driver whose local-directory default runs the whole loop —
compose, carry, execute, carry back, ingest — on one machine. It is the
neutral template a private adopter package copies.

## Examples

| | |
|---|---|
| [examples/robot-shop](examples/robot-shop) | 58 Robot Framework tests over a fictional web shop — the corpus behind the screenshot and the specs on the homepage. |
| [examples/robot-pool](examples/robot-pool) | Those tests executed by `robot-pool` on two partitions — once each (`robot-pool`), distributed over them in two-test shards (`robot-lanes`), or per region, one chunk of a region at a time, with merged logs and cleaned outputs (`robot-regions`). |
| [examples/pytest-shop](examples/pytest-shop) | The same world as pytest, catalogued through `manifest` with node ids and executed by the `command` runner — the framework-agnosticism proof. |
| [examples/jenkins](examples/jenkins) · [ci/jenkins](ci/jenkins) | The `jenkins` runner in both completion modes, also against a job that fans out into one shard per region, and a pinned Jenkins-in-Docker whose jobs run the vendored consumer; `ci/jenkins/e2e.py` drives one against the other. |
| [examples/remote-agent](examples/remote-agent) | The full remote round trip on one machine. |
| [examples/connector-template](examples/connector-template) | A connector package to copy: source, runner and observer, tested with `runcomposer.testing`. |
| [examples/webshop-regression.runspec.yaml](examples/webshop-regression.runspec.yaml) | A complete run spec you can read. |

Executing Robot Framework in-process needs one extra:

```bash
pip install "runcomposer[robot]"
```

A machine that only executes what runcomposer hands out — a build agent —
can do with `pip install --no-deps runcomposer` (plus `robotframework` for
the Robot toolkit): the SDK, the push clients and `runcomposer-exec` import
without the reading and serving dependencies, and push over HTTPS to a
server behind a private CA with a CA bundle (`--ca-bundle`,
`RUNCOMPOSER_CA_BUNDLE`)
([the connector guide, §1](docs/connectors.md#1-the-surface)).

## Developing

```bash
pip install -e ".[dev]" && pytest         # Python 3.10–3.13 (add the [robot] extra for the Robot tests)
ruff check src tests && mypy              # lint + type check, both kept clean
python ci/jenkins/e2e.py                  # the jenkins runner against a real Jenkins (needs docker)
cd ui && npm ci && npm run dev            # UI dev server (proxies to :8100)
npm test && npm run lint                  # UI unit tests; eslint + TypeScript check
npm run build                             # rebuild src/runcomposer/ui_dist
npm run e2e                               # the built UI in a browser against real servers
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the boundaries that matter, and
[SECURITY.md](SECURITY.md) to report a vulnerability.

License: [MIT](LICENSE).
