# runcomposer

**runcomposer** is an open-source, tag-based test run composer & orchestrator:
see your test corpus through a curated tag taxonomy, compose precise
selections with a real filter language, and turn a selection into a
reproducible, portable **run spec** that any executor can fulfill — with
results flowing back from any transport into a run history that feeds new
selections ("rerun what failed").

Status: P1 (compose & export). Read [DESIGN.md](DESIGN.md) for the architecture.

## Quickstart

```bash
pipx run --spec . runcomposer demo    # boot the neutral web-shop demo end-to-end
pipx run --spec . runcomposer serve   # web UI (EN/DE) + API at http://127.0.0.1:8100
# or: docker build -t runcomposer . && docker run -p 8100:8100 runcomposer
```

## The export round-trip (P1's core workflow)

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
neutral template a private adopter package (DESIGN.md §14 P4) copies.

Shipped so far (P0–P2): the sqlite run store with the full run lifecycle
(COMPOSED → RUNNING → AWAITING_RESULTS → COMPLETE), `runcomposer compile |
spec | dispatch | runs | ingest | gc | serve`, the compose/preview HTTP API
with the token-guarded results push, the file-drop inbox, the quarantine
inbox (attach/promote), the localized React UI (taxonomy tree, SVAR filter
builder, auto-compiled preview, runner-aware compose footer, live run status,
quarantine view) pre-bundled in the wheel, and the export round-trip above.

With the `robot` extra (`pip install "runcomposer[robot]"`): the
`robotframework` test source (ids = Robot longnames), the `robot-pool` runner
(process pool, partition fan-out, duration-balanced chunking with a documented
round-robin cold start, listener-streamed live verdicts, §3.3 drift refusal),
and the defused `robot-output-xml` result parser — demonstrated against the
neutral suite in [examples/robot-shop](examples/robot-shop).

P3 (reach): the defused `junit-xml` parser with the pytest example corpus in
[examples/pytest-shop](examples/pytest-shop) (nodeid ids via manifest aliases —
the framework-agnosticism proof), history-based selection (`runcomposer runs
--failed-in latest`, `spec --from-history 'failed@latest'`, and the UI
quick-pick — provenance recorded in `selection.derived_from`), CTRF export
(`runcomposer export <run> --format ctrf`), and the `ci-trigger` runner with
the thin CI-side consumer stage: a reproducible Jenkins-in-docker setup in
[ci/jenkins](ci/jenkins) whose job runs the vendored single-file
`runcomposer-exec` and POSTs results back (webhook-out completion), with a
build-API polling fallback for CI systems that can't call out.

## Developing

```bash
pip install -e ".[dev]" && pytest         # Python 3.10–3.13
cd ui && npm ci && npm run dev            # UI dev server (proxies to :8100)
npm run build                             # rebuild src/runcomposer/ui_dist
```

License: [MIT](LICENSE).
