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

Version 0.1.0. Read [ADOPTING.md](ADOPTING.md) to connect it to your own corpus
and machines, [DESIGN.md](DESIGN.md) for the architecture and the reasoning
behind it, or [CONTRIBUTING.md](CONTRIBUTING.md) to work on it.

## Quickstart

There is **no published package yet** — runcomposer installs from a clone, and
`pip install runcomposer` will not find anything.

```bash
git clone https://github.com/StochasticEntropy/runcomposer && cd runcomposer

pipx run --spec . runcomposer demo    # boot the neutral web-shop demo end-to-end
pipx run --spec . runcomposer serve   # web UI (EN/DE) + API at http://127.0.0.1:8100
# or: docker build -t runcomposer . && docker run -p 8100:8100 runcomposer
```

`demo` seeds a real store — five completed runs with per-item verdicts — into
`./runcomposer-demo/`, and prints the commands that read it back, including
"rerun what failed". Everything it writes is in that one directory, so
`rm -rf runcomposer-demo` undoes it.

## What it does

**Catalog.** A *test source* enumerates your tests as items — an opaque stable
id plus tags — and content-hashes the catalog. Two ship: `manifest` (a plain
JSON or YAML list, zero dependencies, the adoption path for any framework) and
`robotframework` (walks `.robot` files, ids are longnames).

**Compose.** Navigate a curated taxonomy, build a filter, watch the preview
recompile. The filter language is small and lossless: a bare word is a literal
tag, `prefix:Checkout-` is sugar for an anchored regex, `regex:` is the escape
hatch, and the operators are `AND`, `OR`, `NOT`. Available in the web UI and
from the CLI.

**Freeze.** The filter is compiled against the catalog snapshot and the
resulting item list is written *into* a versioned run spec document — so a
dispatched spec is self-sufficient, and the snapshot makes corpus drift
detectable. Core sections are generic and closed; exactly one section,
`runner`, is open, and the core never looks inside it. `runcomposer validate`
checks a document against the published JSON Schema.

**Execute — three ways, one document.** In-process on a `robot-pool` (partition
fan-out, duration-balanced chunking, listener-streamed live verdicts, drift
refusal); on *your own agent anywhere* via `runcomposer-exec`; or by triggering
an existing parameterized CI job with `ci-trigger`.

**Ingest.** Results return over a token-guarded HTTP push, a watched file-drop
directory, or `runcomposer ingest` on the command line. Redelivery rules are
explicit — a byte-identical bundle is a no-op, a different bundle replaces that
shard. A bundle whose marker matches no dispatched run lands in a visible
quarantine inbox rather than quietly entering history.

**Reuse.** Once runs accrue, history becomes a selection source:
`runcomposer runs --failed-in latest --label suite=nightly`, `spec
--from-history 'failed@latest?suite=nightly'`, and a UI quick-pick. The label
scope keeps "latest" from meaning somebody else's run, and the resolved
reference run is recorded in `selection.derived_from`.

**Hand off.** `runcomposer export <run> --format ctrf` for tools that speak
CTRF; `robot-output-xml` and `junit-xml` parse results coming the other way
(both refuse documents carrying entity or DTD declarations).

The web UI ships pre-built inside the wheel in English and German, so
evaluating it needs no Node toolchain. Persistence is sqlite by default, and
`runcomposer gc` keeps runs, quarantine and artifacts bounded.

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
| [examples/pytest-shop](examples/pytest-shop) | The same world as pytest, catalogued through `manifest` with node ids — the framework-agnosticism proof. |
| [examples/remote-agent](examples/remote-agent) | The full remote round trip on one machine. |
| [ci/jenkins](ci/jenkins) | A reproducible Jenkins-in-Docker setup whose job runs the vendored consumer and posts results back. |
| [examples/webshop-regression.runspec.yaml](examples/webshop-regression.runspec.yaml) | A complete run spec you can read. |

Executing Robot Framework in-process needs one extra, still from the clone:

```bash
pip install ".[robot]"
```

## Developing

```bash
pip install -e ".[dev]" && pytest         # Python 3.10–3.13
cd ui && npm ci && npm run dev            # UI dev server (proxies to :8100)
npm run build                             # rebuild src/runcomposer/ui_dist
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the boundaries that matter, and
[SECURITY.md](SECURITY.md) to report a vulnerability.

License: [MIT](LICENSE).
