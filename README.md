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

What P1 ships: the sqlite run store with the full run lifecycle
(COMPOSED → AWAITING_RESULTS → COMPLETE), `runcomposer compile | spec | runs |
ingest | serve`, the compose/preview HTTP API, the localized React UI
(taxonomy tree, SVAR filter builder, auto-compiled preview, runner-aware
compose footer) pre-bundled in the wheel, and the export round-trip above.
Framework runners (`robot-pool`), the ingestion push API, and the file-drop
inbox arrive with P2 (DESIGN.md §14).

## Developing

```bash
pip install -e ".[dev]" && pytest         # Python 3.10–3.13
cd ui && npm ci && npm run dev            # UI dev server (proxies to :8100)
npm run build                             # rebuild src/runcomposer/ui_dist
```

License: [MIT](LICENSE).
