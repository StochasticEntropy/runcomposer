# Changelog

## Unreleased

### Added
- **`RunStore` read paths** (DESIGN.md §6.3). The port grows by addition only —
  new members, and keyword-only parameters with defaults on existing ones:
  `artifact_refs(run_id, dispatch_id=None)`, the `shard=` keyword on
  `verdicts_for`, and the `labels=` keyword on `latest_completed_run`.
  Returned `Verdict`s carry a new `shard` field. Third-party stores written
  against 0.1.0 need exactly those four additions and no rewrites
  (ADOPTING.md §4).
- **`GET /artifacts/{run_id}/{dispatch_id}/…`** — the built-in local artifact
  route DESIGN.md §6.4 has always promised. Paths are resolved and refused
  unless strictly inside `core.artifact_dir` (one rule covering `..`,
  absolute paths and symlinks; a refusal is a `404`, identical to a miss), and
  bytes are served with `sandbox` / `default-src 'none'` / `nosniff` because
  they are attacker-influenced content on the app's own origin.
- **A label scope for history selection** (§7):
  `<verdict>@<selector>?key=value&…`, `runcomposer runs --failed-in latest
  --label suite=nightly`, and `labels=` on `Service.resolve_history`. A scope
  on `run:<id>` is refused rather than ignored. `derived_from` now records the
  scope alongside the run it resolved to, its `completed_at`, and the labels
  that run carried.
- **Taxonomy validation** (§8, docs/taxonomy.md): shape, node keys, and each
  leaf's pattern are checked at startup *and* per request, with messages
  naming the offending node by its path in the document.

### Fixed
- **Verdicts carry their shard.** `verdicts.shard` was stored but never read
  back, so one selection fanned out over two partitions produced two verdict
  rows per item with nothing to tell them apart — the single question fan-out
  exists to answer, and the only way to get at it was to open the sqlite file.
  `GET /api/v1/runs/{id}` now labels every verdict with its `shard` and
  `attempt` (the flat list stays flat; existing readers are unaffected) and
  adds a `shards` roll-up with per-shard counts and computed completion.
- **Artifact references are readable.** `add_artifact_ref` had no reader
  anywhere except `gc`'s deletion loop: `robot-pool` faithfully recorded an
  `output.xml` per shard that nothing could name, let alone fetch. They now
  appear in the run-detail payload, each resolved to a followable href — the
  new route for a local file, the URL itself for a remote CI link, and no
  href (but still visible) for a local path outside the artifact directory.
  Only `http`/`https` count as remote; `file:`/`data:`/`javascript:` are never
  handed out as links.
- **`failed@latest` had no scope.** DESIGN.md §7's flagship feature resolved
  through an unscoped `latest_completed_run`, so "latest" meant the latest
  completed run of *anything* in the store. On any shared deployment the next
  ad-hoc selection silently became the reference for the nightly rerun, which
  then executed the wrong item set and looked entirely healthy doing it.
- **A malformed taxonomy failed silently.** `service.taxonomy()` served
  whatever the YAML parsed to: a wrong-shaped file was a `200` with an empty
  UI panel and no error anywhere, and a missing or empty file was an uncaught
  exception behind a bare `500`. Both now fail with a message naming the file
  and the node — at startup like an unknown plugin id, and per request because
  the file is re-read per request.
- A run that is executing now carries its dispatch record. `dispatch_runner`
  recorded the dispatch only from the returned `DispatchHandle`, so a runner
  that executes inside `dispatch()` (`robot-pool`) left the run RUNNING with
  "no dispatches" for the whole execution, contradicting DESIGN.md §4.
  runcomposer now mints the dispatch id and offers it to the runner as a
  `DispatchReservation` (new optional `bind_dispatch` hook — `describe` +
  `dispatch` remain the whole required `Runner` contract); the runner records
  the hand-off when it makes it. `ci-trigger` records after the trigger POST
  is accepted, so a polled build is visible while it runs. A refused dispatch
  still leaves no dispatch row and returns the run to COMPOSED.
- `RunStore.add_dispatch` re-declares an existing dispatch id (same row, same
  `created_at`) instead of failing, so a dispatch recorded at hand-off time
  can be refined from the handle the runner returns.

## 0.1.0 — 2026-07-07

First release: DESIGN.md phases P0–P3 complete, each independently
judge-verified against the design.

### Core & spec (P0/P1)
- runspec 1.0: versioned, JSON-isomorphic run spec with published JSON
  Schema; `runcomposer validate` (incl. the `--for-dispatch` profile) with
  the §3 versioning policy (strict known fields, MINOR-forward tolerance,
  refuse higher MAJOR).
- Lossless tag-filter AST (literal / `prefix:` / `regex:`, AND/OR/NOT),
  selection compile with fixed intersection semantics, catalog snapshots.
- sqlite RunStore (normative schema), full run lifecycle
  (COMPOSED → RUNNING → AWAITING_RESULTS → COMPLETE, computed completion),
  export dispatches, `runcomposer-exec` — the vendorable single-file
  stdlib-only spec consumer writing the `runcomposer_run.json` marker.
- CLI: validate · demo · catalog · compile · spec · dispatch · runs ·
  ingest · gc · export · serve. Compose/preview HTTP API.
- React UI (en + de, all literals in locale files), taxonomy tree, SVAR
  filter builder behind an adapter, auto-compiled preview, runner-aware
  compose footer — pre-bundled in the wheel, no Node needed to evaluate.

### Ingestion transports (P2a)
- Token-guarded results push API (`POST /api/v1/runs/{id}/results`),
  file-drop inbox watcher, quarantine inbox with attach/promote, content-hash
  idempotency (byte-identical = no-op, same-shard redelivery = last-writer-
  wins), marker `spec_sha256` verification, `runcomposer gc` retention.

### Execution (P2b)
- `robotframework` TestSource (`id = longname`) and `robot-pool` runner:
  shared process pool, partition fan-out, duration-balanced chunking with
  documented round-robin cold start, live verdicts via the injected Robot
  listener, per-dispatch artifact isolation, §3.3 drift refusal /
  `allow_drift` intersection with SKIP-reason-drift. Defused
  `robot-output-xml` parser. All behind the `runcomposer[robot]` extra.

### Reach (P3)
- Defused `junit-xml` parser + pytest example corpus (manifest `aliases`
  map native junit names onto nodeids — the framework-agnosticism proof).
- History-based selection (`failed@latest`, `run:<id>`, `before:<time>`)
  with `derived_from` provenance; `runs --failed-in latest`,
  `spec --from-history`, UI quick-pick.
- CTRF export (`runcomposer export <run> --format ctrf`).
- `ci-trigger` runner + the thin CI-side consumer stage (reproducible
  Jenkins-in-docker under `ci/jenkins/`): webhook-out completion and a
  build-API polling fallback; session-bound CSRF crumb handling.

### Post-P3 polish
- ci-trigger dispatches record the SPEC_JSON hash so §5 marker verification
  works on the CI path.
- robot-pool: user `listener` pass-through and `pre_run_hooks` (completing
  the §6.2a option list).
- Clearer error when a history selection matches nothing; push requests with
  a mismatched declared format are rejected; `gc` no longer leaves empty
  artifact directories.
