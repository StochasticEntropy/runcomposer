# Changelog

## 0.1.2 — 2026-09-03

### Fixed
- **Piping a listing into `head` no longer ends in a traceback.** The reader
  closing the pipe is it saying "enough", but the unhandled `BrokenPipeError`
  surfaced twice — once from the print loop and again from the interpreter
  flushing stdout at exit. `runcomposer catalog | head -1` now exits quietly
  with the conventional 141. Only visible on a catalog large enough to
  overflow the pipe buffer, which is why the 60-item demo corpus never showed
  it and the regression test builds a 4000-item one.

## 0.1.1 — 2026-09-03

### Added
- **`runcomposer taxonomy-check`** — compares the configured taxonomy with the
  catalog in both directions: tags no leaf claims (invisible in the tree) and
  leaves that match nothing (clickable, selects nothing). Neither drift was
  visible anywhere before — the tree renders, the filter parses, the answer is
  just empty. Exits `1` on drift (`--warn-only` to always exit `0`), so it can
  gate a build.
- **Several suite roots in one catalog** — `sources.robotframework` takes
  `roots: [...]` alongside `root:`. A corpus split over sibling trees is still
  one corpus, and a tag filter is asked of the corpus, not of a directory.
  Each root is parsed as its own top-level suite, which is what keeps the ids
  equal to the ones results come back under: pointing Robot at the trees'
  common parent instead prepends that directory's name to every longname.
  Both forms are anchored to the config file by `resolve_config_paths`.
- **`runcomposer catalog --tags`** — every tag in the catalog with the number
  of items carrying it. This is the question you have *before* you can write a
  filter, and nothing answered it. `catalog` now also reads **the configured
  source** rather than only a manifest file: `--manifest` still reads one file
  directly, and with neither a config nor `--manifest` the bundled demo corpus
  is still the fallback, so the zero-config path is unchanged.
- **Duplicate catalog ids are reported.** When two items share one id — two
  Robot tests with the same name in the same suite, say — `catalog` names them.
  A selection cannot tell such items apart, and neither can the results that
  come back. Reported, never raised: a corpus is not unusable because two of
  its tests are named alike.
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
- **`resolve_config_paths(options, resolve)`** — the opt-in hook a plugin
  defines to say which of its options are filesystem paths, so the core can
  anchor them to the config file without interpreting a section it does not
  own (§8, ADOPTING.md §5). All four bundled plugins that take a path
  implement it; a plugin that does not is constructed with its options
  verbatim, exactly as in 0.1.0.
- **`runcomposer demo --workspace DIR`** — where the demo seeds its config and
  store (default `./runcomposer-demo`). The directory must be empty, absent,
  or a previous demo workspace; anything else is refused rather than
  overwritten.
- **A per-run seed for the `demo` runner**, read from the spec's one open
  section (`runner: {demo: {seed: …}}`, §3) and falling back to the configured
  one. That is what lets `runcomposer demo` seed several *different* completed
  runs through a single configured runner.

### Fixed
- **`runcomposer demo` seeds a real store, as §12 always promised.** It printed
  `Seeded history: 3 completed runs over 'Regression'` and persisted nothing:
  the runs, verdicts and durations lived in memory for the length of the
  command, so the very next thing a reader tried — `runcomposer runs
  --failed-in latest`, the flagship loop the demo had just narrated — answered
  *"history features are dark on a fresh store"*. The demo now runs through the
  real machinery (`compose_run` → `dispatch_runner` → the store) instead of an
  in-process imitation of it, reads its own summaries back out of the store,
  and resolves the rerun with a real scoped history query
  (`failed@latest?suite=nightly`) whose provenance is recorded in the stored
  spec.

  It writes into **one directory it names and prints** — `./runcomposer-demo`,
  holding a generated `config.yaml` and the sqlite file that config points at.
  Deliberately *not* `./runcomposer.db`: that is the zero-config default store,
  and a demo writing there would leave fake `Shop.…` runs for an adopter's
  first real command to trip over. A directory holding a `config.yaml` the
  demo did not write is refused, not overwritten; an unwritable working
  directory falls back to a temp directory and says so; re-running re-seeds
  from scratch, so the output stays deterministic. Every command in the
  printed "Next steps" carries that workspace's `--config` and works when
  pasted, from any directory — `rm -rf runcomposer-demo` is the whole
  uninstall.
- **One path base for the whole config file** (§8). `core:` paths resolved
  against the config file's directory while everything under `store:`,
  `sources:` and `runners:` resolved against the *working* directory, and the
  gap was expensive in practice: the same `--config` invoked from two
  directories silently created a second, empty sqlite database; a source root
  that worked from one directory failed from another with `robotframework
  source root not found`; and `examples/remote-agent/sync.sh` had to `cd` into
  the config's directory, with the trap written up as documentation in three
  places. Every relative path in a config file now resolves against that
  file's own directory; absolute paths are used exactly as written.

  The core still does not interpret plugin config to do it. Which options are
  paths is the plugin's call — `listener: MyListener:arg`, `pre_run_hooks`,
  `base_url` and sqlite's `:memory:` all look path-shaped and are not — so a
  plugin opts in with `resolve_config_paths` (above) and decides for itself.
  **Nothing written against 0.1.0 changes behaviour**: a plugin without the
  hook still gets its options verbatim, and a config that spelled its store
  path out absolutely as the old workaround is unaffected. Paths inside a
  *runspec document* are untouched — specs travel, and the executor contract
  (§3.3) is between the document and whoever fulfills it. `sync.sh` lost its
  `cd`.
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

### Documentation
- **`docs/cli.md` — the CLI reference**, covering every subcommand and every
  flag of both `runcomposer` and `runcomposer-exec`, each with a worked
  example. Several implemented flags were documented nowhere until now, most
  importantly `--config` (on eight subcommands, and the only way to select a
  config file — there is no environment-variable fallback), plus `--id`,
  `runs --state/--since/--until/--label/--limit`, `spec --expect-format`, and
  `serve --host/--port`. It also records that `core.*` paths resolve relative
  to the config file's directory while `store.sqlite.path` resolves relative
  to the working directory, which silently creates a second database when one
  config is invoked from two directories.
- **DESIGN.md now marks design intent as such.** A clause marked `[planned]`
  has no implementation in the repository; everything unmarked describes what
  the code does today (`grep -n '\[planned\]' DESIGN.md` lists them). Seventeen
  passages were reconciled against the source — among them the `postgres`
  store, the `STALE` expiry policy, the optional `Runner` `status`/`cancel`/
  `health` capabilities, per-verdict `artifacts`, `results.callback`, UI
  result upload, the full history picker, and
  `POST /api/v1/runners/{id}/actions/<action>`, none of which exist. Where the
  document and the code differed on a detail rather than on existence, the
  document was corrected instead: the drift override is a runner option
  (`allow_drift` / `RC_ALLOW_DRIFT`), not a `--allow-drift` CLI flag; there is
  no core `materialize(spec)` helper, since every runner reads
  `selection.materialized.item_ids` from the document; `ci-trigger` builds its
  job URL from config rather than templating it from the spec; and
  `runcomposer demo` demonstrates history in memory without persisting it, so
  it does not light up `--failed-in` afterwards. §9's endpoint and CLI lists
  gained the quarantine actions and `runcomposer export` respectively.
- **Every documented install now works from a clone.** `pip install
  "runcomposer[robot]"` was printed in the README, the homepage, the
  remote-agent example and two plugin error messages, but nothing is published
  to PyPI (`pypi.org/pypi/runcomposer/json` → 404), so the one documented way
  to get the Robot extra failed. All occurrences are now `pip install
  ".[robot]"`, and the README, homepage and DESIGN.md say plainly that there is
  no published package yet.
- **The homepage links its own artefacts.** The page named ADOPTING.md,
  DESIGN.md, docs/taxonomy.md, the examples, ci/jenkins, the schema and the
  licence without linking any of them; a reader had one exit, in the footer.
  They are now linked inline, plus a "where next" block of six destinations.
- **The homepage colophon attributed its examples to the wrong corpus.** It
  claimed they "boot with `runcomposer demo`". The screenshot and both plates
  use `Tests.Payments.…` longnames from `examples/robot-shop` (58 Robot tests);
  `runcomposer demo` boots the 60-item *manifest* corpus with `Shop.…` ids.
  Both plates are now captioned with the corpus that produced them.
- **Light-theme contrast meets WCAG AA.** `--signal` was 3.92:1 on `--bg` and
  4.30:1 on `--surface` at 0.82rem (the plugin table's first column, `.lane-id`,
  `.marker .num`), `.chip-skip` 4.15:1 and the `.plate-head` meta 4.43:1.
  `--signal` is now `#8A5208` and `--ink-3`/`--skip` `#525F73`; every light
  token pair clears 4.5:1. The dark theme already passed and is unchanged.
- **Community health files**: `CONTRIBUTING.md`, a `SECURITY.md` written around
  the real surface (defused XML from result bundles, same-origin artifact bytes,
  the per-run ingest token, quarantine) with an explicit not-a-vulnerability
  list, and `.github/ISSUE_TEMPLATE/` bug-report and feature-request forms.
- **Social preview**: a 1200×630 PNG card (`docs/og-card.png`) replaces the
  1760×1100 WebP `og:image`, which X and LinkedIn do not reliably render, plus
  `og:url` and `twitter:card`. The page also has a favicon and captions on both
  videos, and the README has CI/licence/Python badges and links to the videos.
- **Release notes stop containing the whole changelog.** `release.yml` used
  `--notes-file CHANGELOG.md`; it now extracts just the tagged version's
  section and fails the release if that section is missing.

- **`ci/jenkins/README.md` says that the demo job runs no tests.** The shipped
  build step uses `runcomposer_exec.py --simulate`, which fabricates verdicts —
  so a green build proves the transport, not any test. The README now leads
  with that and shows the one-line change (`--command`) that makes the stage
  real, with the placeholder contract and the two things to adjust on the
  runcomposer side. The demo job keeps simulating on purpose: the container
  has no test corpus.

### Removed
- **`reserve_name/`** — the PyPI/npm placeholder packages and their `RESERVE.md`
  owner checklist, which told the reader to delete the directory after the first
  release. Reserving a name with a squat advertises that the name is unclaimed,
  and every documented path installs from a clone anyway. DESIGN.md §1 and the
  decision log record the reversal.

## 0.1.0 — 2026-07-07

First release. The whole loop works end to end: catalogue a tagged corpus,
compose a selection, freeze it into a portable run spec, execute it in-process
/ on your own remote agent / in a CI job, and ingest the results back from any
transport into a history that feeds the next selection.

### Core & spec
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

### Ingestion transports
- Token-guarded results push API (`POST /api/v1/runs/{id}/results`),
  file-drop inbox watcher, quarantine inbox with attach/promote, content-hash
  idempotency (byte-identical = no-op, same-shard redelivery = last-writer-
  wins), marker `spec_sha256` verification, `runcomposer gc` retention.

### Execution
- `robotframework` TestSource (`id = longname`) and `robot-pool` runner:
  shared process pool, partition fan-out, duration-balanced chunking with
  documented round-robin cold start, live verdicts via the injected Robot
  listener, per-dispatch artifact isolation, §3.3 drift refusal /
  `allow_drift` intersection with SKIP-reason-drift. Defused
  `robot-output-xml` parser. All behind the `runcomposer[robot]` extra.

### Reach — CI, pytest, history, CTRF
- Defused `junit-xml` parser + pytest example corpus (manifest `aliases`
  map native junit names onto nodeids — the framework-agnosticism proof).
- History-based selection (`failed@latest`, `run:<id>`, `before:<time>`)
  with `derived_from` provenance; `runs --failed-in latest`,
  `spec --from-history`, UI quick-pick.
- CTRF export (`runcomposer export <run> --format ctrf`).
- `ci-trigger` runner + the thin CI-side consumer stage (reproducible
  Jenkins-in-docker under `ci/jenkins/`): webhook-out completion and a
  build-API polling fallback; session-bound CSRF crumb handling.

### Polish
- ci-trigger dispatches record the SPEC_JSON hash so §5 marker verification
  works on the CI path.
- robot-pool: user `listener` pass-through and `pre_run_hooks` (completing
  the §6.2a option list).
- Clearer error when a history selection matches nothing; push requests with
  a mismatched declared format are rejected; `gc` no longer leaves empty
  artifact directories.
