# runcomposer

**runcomposer** is an open-source, tag-based test run composer & orchestrator:
see your test corpus through a curated tag taxonomy, compose precise
selections with a real filter language, and turn a selection into a
reproducible, portable **run spec** that any executor can fulfill — with
results flowing back from any transport into a run history that feeds new
selections ("rerun what failed").

Status: P0 (skeleton). Read [DESIGN.md](DESIGN.md) for the architecture.

## Quickstart

```bash
pipx run --spec . runcomposer demo        # boot the neutral web-shop demo end-to-end
pipx run --spec . runcomposer validate --for-dispatch examples/webshop-regression.runspec.yaml
```

What P0 ships: the core/plugins skeleton (tag-filter AST, selection compile,
runspec build/validate, plugin registry), the published
[runspec 1.0 JSON Schema](src/runcomposer/schemas/runspec-1.0.json), the
`runcomposer validate | demo | catalog` CLI, the neutral demo corpus
(DESIGN.md §12), and CI. Compose/export APIs, the store, and real runners
arrive with P1–P3 (DESIGN.md §14).

License: [MIT](LICENSE).
