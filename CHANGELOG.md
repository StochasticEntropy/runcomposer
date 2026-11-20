# Changelog

## 0.2.0 — 2026-11-20

A rework of the backbone. 0.1 proved the product idea; its runner contract,
lifecycle, store, configuration, API and CLI were a proof of concept, and 0.2
replaces them with contracts meant to be built on. **This release breaks
plugins, config files and API clients written against 0.1** — the list below
says where. The run spec document (`runspec/1.0`) is unchanged and a 0.1
sqlite database opens as it is.

### Breaking

**Connectors (plugins)**

- **Import from `runcomposer.sdk`, and only from there.** It is the connector
  surface: protocols, records, errors, helpers, `load_config`, `Service`.
  `runcomposer.service`, `runcomposer.config`, `runcomposer.api` are gone
  (the code lives in `runcomposer.app`, `runcomposer.http`, `runcomposer.cli`,
  none of which is a public import surface).
- **`Runner` is `describe()` + `dispatch(ctx: DispatchContext)`**, returning
  nothing. Gone: `dispatch(spec) -> DispatchHandle`, and the hooks discovered
  by attribute — `bind`, `bind_dispatch`, `deliveries`, `last_plan`. A runner
  now refuses with `ctx.refuse`, hands off with `ctx.declare(shards)`, and
  delivers with `ctx.deliver`; it reads the store through nothing but the
  context. Optional `cancel(ctx)` and `health() -> Health` are called.
- **`RunnerInfo`** gained `title`, `description`, `parameters`,
  `result_format`. Per-run options must be *declared* as `Parameter`s: a
  spec's `runner.<id>` section no longer overrides configured options.
- **`RunStore` is a new port.** `set_run_state`, `clear_live_verdicts` and
  `get_ingest_token_sha256` are gone; `add_dispatch` changed; new members
  cover dispatch state (`update_dispatch`, `get_dispatch`, `delete_dispatch`,
  `owned_dispatches`), the run headline, hand-out bytes, token hashes, live
  status, events, `delete_run`, and `list_runs` now pages (`offset`, `q`) and
  returns a `RunPage` with a total. `record_delivery` is keyed on
  (run, dispatch, shard) and must be atomic.
- **`ResultParser`s raise the one `ParseError`** from the SDK (the three
  per-module classes are gone) and read XML through `forbid_dtd` /
  `parse_xml`.
- **`Verdict.artifacts`, `DispatchHandle`, `DispatchReservation`,
  `Selection.to_data`, `registry.available_entry_points`** are removed.
  `DeliveryRecord.dispatch_id` is never `None`.
- **Every plugin must provide its whole contract.** Each configured plugin
  is checked at startup against its role's protocol and against
  `Configurable`; a missing member is a `ConfigError` naming it. Derive from
  the new base classes — `SourceBase`, `RunnerBase`, `ParserBase`,
  `StoreBase`, `ObserverBase` (all `PluginBase`) — for the optional members
  (`fingerprint`, `duplicate_ids`, `cancel`, `health`, `close`, `OPTIONS`,
  `PATH_OPTIONS`, `CONNECTOR`, `resolve_config_paths`), or define them. A
  plain class with only the required methods no longer loads; nothing is
  discovered by `getattr` any more. `TestSource.fingerprint()` may return
  `None` ("cannot tell"), and `resolve_config_paths` is a classmethod.
- **`RunStore` gained `latest_events(run_id, limit)`** (a bounded read of the
  tail) and **`add_dispatch(..., exclusive=False)`** (refuse with `RunActive`
  when the run's latest dispatch is in flight, atomically).
- **`ctx.refuse()` is only legal before `ctx.declare()`**; called after it,
  it raises `RuntimeError` and the dispatch fails with the reason in its
  error.
- **The `demo-corpus` source moved** to
  `runcomposer.plugins.manifest_source:DemoCorpusSource` (the entry point is
  unchanged); `runcomposer.plugins.demo_source` is gone.
- **The `runcomposer-verdicts` parser refuses a file named directly** that is
  not its format (`ParseError`) instead of returning no verdicts; in a bundle
  directory foreign JSON files are still skipped.
- **`ServiceError` is gone**, replaced by a typed taxonomy
  (`InvalidRequest`, `NotFound` / `UnknownRun`, `Conflict` / `RunActive` /
  `DispatchRefused` / `SpecConflict`, `IngestError`, `ConfigError`, …), each
  with a stable `code`.
- **`runcomposer.plugins.robot_listener` is gone.** The live listener is
  `runcomposer.sdk.robot.LiveStatusListener` (and
  `RemoteLiveStatusListener` for an agent that pushes to the API).

**Changed during the 0.2 line.** For code written against an earlier,
unreleased state of 0.2. Each of these is also a break relative to 0.1.x,
where the member, field or behaviour did not exist or worked differently.

- **`ctx.options` holds the configured options only.** It is the runner's
  config entry as its constructor received it (`${env:…}` resolved); the
  validated per-run values are in `ctx.parameters` and nowhere else — they
  are no longer overlaid on the options. A runner that read a per-run value
  from `ctx.options` reads it from `ctx.parameters`.
- **Only recorded artifacts are served.** A file under a dispatch's
  `workdir` is downloadable only once a runner recorded it with
  `add_artifact` — the file itself, or a directory above it; everything else
  under `/artifacts/` answers `404`. Record what should be served; keep what
  must never be in `ctx.private_dir`.
- **`Runner.plan(ctx) -> PlanPreview` is a member of the runner contract.**
  `RunnerBase` supplies a default ("no preview"); a runner that does not
  derive from it must define `plan`, or startup refuses it naming the
  member.
- **The `RunStore` port grew**: `seed_durations()`;
  `duration_aggregates(statuses=)`; `update_dispatch(shard_groups=)` and
  `DispatchRecord.shard_groups`; `add_artifact_ref(group=)` and
  `ArtifactRef.group`; `record_live_verdict` takes and `live_verdicts`
  returns `LiveStatus` records (not `Verdict`s), `RUNNING` included. A store
  of your own adds them; `check_store` names what is missing.
- **The sqlite schema is version 3**: dispatches gain `shard_groups`,
  artifact references `shard_group`, and there is a `duration_seeds` table.
  A database of 0.1.x or of schema 2 is migrated in place when it is opened;
  an earlier state of 0.2 refuses a migrated database as newer than it
  understands.
- **A `string` parameter is one line.** A value with a line break is a
  `400`; declare the parameter as `text` to take several lines.
- **`ui.quick_filters` are served normalized**, by `GET /api/v1/ui-config`
  and `/meta`: every entry is an object `{label, filter, tag_filter, text,
  kind}`, also one that was written as a bare string.
- **A run's `duration_s` describes its latest dispatch**, like every other
  headline field; each entry of `dispatches` carries its own `duration_s`.
- **A rerun of failures keeps the source run's title** (it was
  `Rerun of failures: <title>`); `--title` / `title` sets another. That it
  is a rerun is data: origin `rerun`, `derived_from`, and `rerun_of` in the
  answer and in the new run's detail.
- **The `gc` report gains `expired_dispatches`** (`--json`; a line in the
  text output when there are any).
- **Kit:** `FakeDispatchContext.delivered_shards` is a method,
  `delivered_shards()` (next to `outstanding_shards()`; `outstanding` stays
  an attribute), and `live_updates` keeps the whole stream in order —
  `RUNNING` included, and kept after the shard delivered; what a store would
  hold is `live_now`.

**Configuration**

- **The key of a plugin entry is its id**, and only ids configured under
  `runners:` can be dispatched to. A config file without `runners:` has no
  dispatchable runner — add `runners: {demo: {}}` to keep the demo runner.
- **`ci-trigger` is replaced by `jenkins`**, with other options: `parameters`
  (a mapping of your job's build-parameter names to templates),
  `artifact_paths`, `run_parameters`, `verify_tls` / `ca_bundle`,
  `request_timeout_s`, `retries`.
- **Unknown plugin options are refused at startup**, with the accepted keys
  in the message. A typo that 0.1 carried until the first dispatch now stops
  `serve`.
- **`store.sqlite.path: "file:…"`** is refused (`:memory:` works).
- **An unknown top-level section** is still an error; a connector package's
  own settings go under the new `connectors:`.
- `robot-pool`: `output_root` is gone (artifacts go to the dispatch's
  directory under `core.artifact_dir`); `log.html` and `report.html` are
  written by default (`log: false` / `report: false` to turn off).

**HTTP API**

- **`POST /api/v1/runs` with a dispatch returns before the run finishes** —
  `DISPATCHED` or `RUNNING`, never `COMPLETE`. Follow the run, its events, or
  `?wait=` on the events. The answer (and those of `/dispatch` and `/rerun`)
  says `handoff: done | pending`; a refusal after a `pending` answer leaves
  the run `COMPOSED` with a `dispatch.refused` event — a run an answer has
  named is never removed implicitly.
- **Error bodies are `{"detail": <string>, "code": <id>}` everywhere**, and
  request validation answers `400` (not `422`) in that shape, with `fields`.
  Status codes changed where 0.1 was wrong: a refused dispatch is `409`, an
  unknown runner or a bad parameter `400` (were `500`); an unreadable bundle
  `422`, a push to an unknown run a plain `404` that writes nothing.
- **Request bodies are closed and typed**: an unknown field, `title: 5` or
  `labels: {a: 1}` is a `400`.
- **The export answer changed**: `spec_url`, `spec_sha256` and `ingest_token`
  instead of `spec_document`. Download the document from `spec_url` — those
  are the exact bytes the marker hash is verified against.
- **`GET /runs/{id}/spec` no longer contains `results.token`.**
- `GET /runs` returns `total` / `limit` / `offset`; `GET /runners` returns
  the configured id (not the plugin's own), parameters and health.

**CLI**

- **Exit codes**: `dispatch` (and the new `run`) exit `1` when the run
  completed but is not `PASS`, and `3` when it ended without a result. 0.1
  exited `0` on `COMPLETE (FAIL)`.
- `spec --runner ID` no longer embeds the runner's configured options; it
  composes for that runner and validates `--param NAME=VALUE` against its
  declared parameters.
- `spec --export` prints the ingest token on stderr (or writes
  `--token-file`); the emitted document carries none.
- `dispatch SPEC` refuses a spec file whose selection, source or results
  contract was edited after its run was stored; an edited `runner` section is
  taken as this dispatch's per-run parameters.
- `runcomposer-exec`: the marker is written only when `--command` exited with
  an accepted code (`--ok-exit-codes`, default `0`), and the template is
  filled by literal replacement of `{ids_file}` `{run_id}` `{out_dir}` — other
  braces are left alone.

**Behaviour**

- **A run with planned items that never reported is `INCOMPLETE`, not
  `PASS`.** The missing items are `MISSING`. Empty results are never `PASS`.
  On upgrade, the completion of stored runs is recomputed under this rule.
- **The manifest source's snapshot hashes ids and tags only.** Specs composed
  by 0.1 against a manifest read as drifted.
- **Verdict status `MISSING` and completion `INCOMPLETE`** are new values a
  client must expect, as are the states `FAILED`, `CANCELLED` and (now really
  set) `STALE`.

### Added

- **`runcomposer.testing`, the conformance kit**: `check_source`,
  `check_runner`, `check_parser`, `check_store`, `check_observer` (each fails
  naming the violated rule), `FakeDispatchContext` (a recording in-memory
  context enforcing the hand-off order with the same code as runcomposer),
  and `ConnectorHarness` (a real `Service` in a temporary directory, driven
  synchronously with a timeout). Every reference plugin is tested with it.
- **`examples/connector-template/`**: an installable connector package for
  the example world — a source, an in-process HTTP-check runner and an
  observer, registered by entry point, tested with the kit, run end to end
  through `runcomposer run` in CI.
- **`docs/connectors.md`**, the connector author's guide, and an example
  config with an end-to-end test for each of four ways to execute
  (`examples/robot-pool`, `examples/jenkins`, `examples/pytest-shop`,
  `examples/remote-agent`).
- **`CONNECTOR = "<name>"`** on a plugin class: built with
  `connectors.<name>` as `connector_settings` (`${env:…}` resolved,
  redacted).
- **`ci/jenkins/e2e.py`**: the `jenkins` runner against a real Jenkins in
  docker — health, callback, poll, cancel in both modes, a plan preview, a
  second job that fans out into one shard per region (results read from the
  console log; results pushed by the job with progress), and a cancel after
  Jenkins forgot the queue item; the image pins Jenkins and every plugin,
  and passes the ingest token as a password parameter.

- **Asynchronous dispatch** through a dispatch manager with a bounded pool
  (`core.dispatch.max_concurrent`), the same code in the foreground for the
  CLI.
- **A lifecycle state machine**, tracked per dispatch; the run's state is
  derived from its latest dispatch. Completion is computed against the
  declared shard *set* and the planned item set.
- **Cancellation** — `POST /runs/{id}/cancel`, `runcomposer cancel` (also
  against a run a server is executing; `--force` for a dispatch whose owner
  cannot be reached) — and **restart recovery**: a dispatch whose process is
  gone is marked `STALE`.
- **An event stream** per run (`GET /runs/{id}/events?after=`, `runcomposer
  watch`) and an **observer port** with `webhook` (HMAC-signed), `jsonl` and
  `command` observers.
- **Per-run runner parameters**, declared by the runner, validated by the
  API, exposed for a UI to render.
- **Runners**: `command` (run any command per shard and parse what it wrote);
  `jenkins`; `robot-pool` options `suite_roots`, `log` / `report`,
  `max_tests_per_chunk`, `rerun_failed`, `partition_variable`,
  `partition_variables`, `executed_tag`, `worker_setup`, `pythonpath`,
  `hook_timeout_s`, `shard_timeout_s`, `format`; exact-id test selection;
  spawned workers that can be stopped.
- **Live status for any store** — a picklable live channel from the context.
- **Configuration**: several `sources:` as one catalog, `parsers:`,
  `observers:`, `connectors:`, `${env:NAME}` secrets, `PATH_OPTIONS`,
  `core.dispatch`, `core.ingestion.settle_s`.

- **Two SDK toolkits.** `runcomposer.sdk` is a package with
  `runcomposer.sdk.robot` (exact-id selection, the live listeners, the shard
  worker and its supervisor with lanes, timeouts and the stop protocol,
  delivery, output merging) and `runcomposer.sdk.jenkins` (`JenkinsClient`:
  crumb and session, bounded retry, trigger → queue item → build, lookup by
  queue id, stop and cancel, `follow` — a build from queue item to its end,
  taken back when the dispatch was cancelled before there was a link to
  stop it by — artifacts and URLs below a build; `encode_spec` /
  `decode_spec`). `corpus_drifted(ctx)` in the SDK asks the drift question
  every runner asks. `robot-pool` and `jenkins` are built on
  them and import nothing else of runcomposer; guard tests hold the names,
  the imports and that Robot Framework is loaded only to execute. New in
  `runcomposer.sdk`: `LIVE_STATUSES`, `LiveStatus`, `PlanContext`,
  `PlanPreview`, `RemoteLiveChannel`, `load_import_path`.
- **Plan previews.** `Runner.plan(ctx: PlanContext) -> PlanPreview` and the
  `plan` capability: which item goes to which shard, groups, estimates and
  notes — nothing executed, stored or triggered. Every reference runner
  previews. `POST /api/v1/plan`, `POST /api/v1/runs/{id}/plan` (a refusal is
  the answer, `refused: true`), `runcomposer plan`.
- **Parameter kinds `text` and `mapping`** (seven kinds in all) and an
  optional `pattern` the whole value must match — of a `mapping`, every
  value; checked when the run is requested. The runner listing carries it.
- **`RUNNING` live status**, worker log lines in the run's event stream
  (`LiveChannel.log`), and **progress endpoints** for an executor
  runcomposer did not start: `POST /api/v1/runs/{id}/live` and `/log`,
  authorized by the dispatch's ingest token. `RemoteLiveChannel` and
  `runcomposer.sdk.robot.RemoteLiveStatusListener` are the clients.
  `GET /runs/{id}/verdicts?status=RUNNING` selects what is executing now.
- **Shard groups**: `ctx.declare(…, groups={shard: label})`,
  `ctx.add_artifact(…, group=…)`; the run detail rolls results up per group
  (`groups`) next to the per-shard roll-up, and counts what is streaming per
  shard (`live_summary`).
- **A runner reads its own dispatch**: `ctx.delivered_shards()`,
  `ctx.outstanding_shards()` (results pushed from outside included),
  `ctx.set_link()` for what is known after the hand-off (a build URL),
  `ctx.private_dir` — a directory that is never served
  (`core.private_dir`, outside `core.artifact_dir`, removed with the run,
  swept by `gc`). A recorded directory makes the files below it servable
  (artifact kind `directory`).
- **Expiry of a dispatch waiting for results**:
  `core.dispatch.awaiting_timeout_s` (default `null`) — such a dispatch
  becomes `FAILED` with a `run.failed` event that says why and names the
  shards that never delivered; every delivery restarts the clock, late
  results still complete it. Applied by `serve` and once by `gc`.
- **Duration history by status, and seeded durations.**
  `ctx.history.durations(statuses=)`; 0 ms means "not known" to the store
  and the planner; `runcomposer history import FILE` (CSV or JSON, names
  resolved through the catalog, scoped by `--label`) seeds durations that
  answer only for items nothing was measured for; `runcomposer history
  durations` prints what a planner sees. Sample:
  `examples/history/durations.csv`.
- **`robot-pool` switches**: `partition_mode: each | split` (every test once,
  partitions as lanes), `chunk_shards` (every `max_tests_per_chunk` piece a
  shard of its own, delivered when done), shards grouped by partition,
  `pre_dispatch` (preparation steps that see the plan and may refuse or add
  variables), `run_parameters` with `parameter_variables`, `rpa`,
  `robot_options`, `history_statuses`; merged outputs without "added from
  merged output" notes; `RUNNING` at test start and worker progress in the
  event stream; each shard's directory recorded as an artifact; health data.
  Example entry `robot-lanes` in `examples/robot-pool/config.yaml`.
- **`jenkins` switches**: `shards` (a list, or a template such as
  `"{params[regions]}"`; `{shards}` and `{shard}` in templates and paths),
  `result_urls` (URLs below the build, e.g. `consoleText`),
  `field_providers` (computed build parameters that may refuse),
  `postprocess` (over downloaded files before parsing), `{spec_b64}` and
  `{spec_url}`, `max_parameter_bytes` / `max_request_bytes` (refused before
  the trigger, naming the way out); a `mapping` parameter renders as
  `name=value` lines; `cancel` through the recorded build link, else the
  queue item, else the build found by its queue id; a cancel during the
  trigger takes the build back; a shard the job pushed is not downloaded
  again; health checks. Example entries `jenkins-fanout` and
  `jenkins-fanout-push` with `examples/jenkins/shop_job.py`, and the job
  `runcomposer-fanout` in `ci/jenkins/casc.yaml`.
- **`command` and `demo`** preview their plans; `command` hands a `mapping`
  parameter to the command as `name=value` lines.
- **Configuration**: `${env:…}` is substituted before a path is anchored
  (plugin `PATH_OPTIONS` and the core's own paths), so a path from the
  environment stays where it points; `ui.quick_filters` take whole filter
  expressions, `ui.default_filter` and `ui.presets` are new — all validated
  at startup (a preset's runner and parameters included) and served
  normalized; `GET /api/v1/presets`.
- **Run detail**: `shard_groups`, `groups`, `live_summary`, `rerun_of`,
  `last_live` on a `MISSING` row (what was last streamed for an item that
  delivered nothing), a `duration_s` per dispatch, artifacts with their
  `group`. Attaching a quarantined delivery that holds nothing the run
  planned is refused (`409`, `nothing-planned`) and changes nothing.
- **Kit**: `FakeDispatchContext.from_source`, `preview`, `live_now`,
  `shard_groups`, `set_link`, `private_dir`, `spec_bytes()`, worker log
  lines in `logs`; `check_runner` rules `runner.plan`,
  `runner.plan-is-read-only`, `runner.plan-matches-dispatch`;
  `check_store` rules `store.shard-groups`, `store.live-running`,
  `store.duration-statuses`, `store.duration-seeds`;
  `ConnectorHarness.start()` (a handle to follow and cancel),
  `ConnectorHarness.plan()`, `ConnectorHarness(serve=True)` serving the
  API on a free port, `ConnectorHarness.artifacts()` (a run's artifacts
  with the path each is served under); `plugin(section, name, **options)`
  builds an installed plugin by its config name; the fake validates and
  defaults per-run parameters against the runner it is run with; and
  `runcomposer.testing.jenkins` — `FakeJenkins`, the protocol stand-in the
  reference `jenkins` runner is tested with, for a connector's own Jenkins
  runner.
- **CLI**: `plan`, `history import`, `history durations`, `doctor -v`
  (every runner's health data; a list of checks reads as a checklist),
  `rerun --title`; `show` prints groups and what is running now.
- **`runcomposer-exec --callback URL [--token T] [--push]`**: log lines and
  (with `--simulate`) live status while it works, and the finished bundle
  pushed to the API — exit `4` when the push is refused or cannot be
  delivered.
- **Catalog reload** when the corpus changes (`fingerprint()` on sources),
  `POST /catalog/reload`.
- **Ingest tokens bound to a dispatch**, stored hashed, returned once,
  rotatable. **Exact, re-downloadable spec bytes** for every hand-out.
- **API**: `/meta`, `/catalog/tags`, `/runs/{id}/dispatch`, `/cancel`,
  `/rerun`, `DELETE /runs/{id}`, `/events`, `/verdicts`, dispatch spec and
  token endpoints; run rows complete for a list; run detail with events and
  the selection.
- **CLI**: `run`, `rerun`, `cancel`, `watch`, `show`, `finalize`, `delete`,
  `export-spec`, `quarantine`, `doctor`; `--json`; `spec --dry-run`;
  `-v/--verbose`.
- **A versioned sqlite schema with in-place migration** from 0.1; WAL, a busy
  timeout, one connection per thread, atomic deliveries.
- **Logging** through the `logging` module (`runcomposer.*`).
- **CI** on Linux (3.10–3.13), macOS and (not yet blocking) Windows, plus
  jobs for the robot extra, the connector template, the export round trip,
  the wheel, the docker image and a real Jenkins; **releases** publish the
  very files the release workflow built, installed and tested.
- **`ruff` and `mypy`** configured and clean; the version single-sourced in
  `runcomposer/__init__.py`.

### Fixed

- A run completed green without its planned items; a refused re-dispatch
  erased a finished run; a late bundle for an old dispatch rewrote the run's
  state; a shard with an unexpected name completed a dispatch; a re-dispatch
  with identical results never completed; `finalize` on a never-dispatched
  run reported `PASS`.
- The ingest token was readable through `GET /runs/{id}/spec`; an
  unauthenticated push to an unknown run was written to quarantine and could
  be attached into a passing run; the quarantine bound was enforced only by
  `gc`.
- A spec document could set `pre_run_hooks` and `listener`, and name a result
  format as an import path.
- Concurrent deliveries to one shard left several deliveries.
- The catalog was frozen at process start: after a corpus edit every dispatch
  was refused until restart.
- One unreadable bundle in the inbox blocked every bundle after it; a slow
  copy could be ingested half-written.
- A DOCTYPE behind 64 KiB of padding, or in a UTF-16 document, got past the
  XML check.
- `retention.max_age_days: null` crashed `gc`; `export --format ctrf` failed
  on a timestamp with an offset; `limit=-1` dropped a row; time filters were
  compared as unvalidated strings; file-drop quarantine entries always
  claimed the reference format.
- `store.sqlite.path: ":memory:"` was documented and unusable.
- `robot-pool` treated ids as glob patterns, so an id containing `[` or `*`
  was not executed (or ran its neighbours); a multi-root catalog met a single
  `suite_root`.
- The Jenkins poll path could ingest another build's results, gave up as a
  "refusal" after the hand-off, and only worked for one result format.
- Two processes could both dispatch one run (the check was per process).
- A cancel requested from another process reached a dispatch executing in
  the foreground (`runcomposer run`) only through `ctx.cancelled`: the
  runner's `cancel(ctx)`, which stops its executor, was never called.
- A cancelled `robot-pool` dispatch waited for a running `pre_run_hooks`
  command to time out.
- `GET /runs/{id}` read the run's whole event stream to show the last 200.
- A `${env:NAME:-default}` default was treated as a secret; a runner's plan
  text was not redacted.
- On Windows, a live process another user owns read as gone (now treated
  like `EPERM` on POSIX); tests that need a POSIX shell skip there.
- The project image ran as root.

### Web UI

Rebuilt against the 0.2 API, in TypeScript; React and React DOM are the only
run-time dependencies.

- Four views with their own URLs — Compose, Runs, a run, Inbox. A selection
  can be shared as a link, a run can be linked to, and a reload or a trip to
  another view loses nothing. An edit of the filter is a step in the
  browser's history.
- One filter editor over the run spec's filter grammar: rules, groups, NOT as
  a labelled switch, and the same expression as text. The third-party filter
  widget is gone.
- The tag picker shows item counts, says what applying will add and remove
  and how many items result; unticking a tag removes it.
- The preview is always the answer to the filter on screen or says it is not,
  and a run can only be started from a current preview. Unticked items
  survive filter edits. Search, sort, group by suite; rows are rendered as
  needed.
- A plan can be read before a run is started, for the selection in the
  builder and for a stored run: shards, groups, estimates and the runner's
  notes — or the refusal a dispatch would meet. Offered for runners with the
  `plan` capability; never the plan of anything but what is on screen.
- Runner options are a form rendered from what the runner declares — string,
  text, integer, boolean, choice, multi-choice and mapping, with a declared
  pattern checked before sending; a refusal by the server lands on its field.
  Unhealthy runners are listed with the reason, and a runner's own health
  checks one by one.
- A deployment's presets fill selection, runner, options, title and labels;
  its default filter is where a new selection starts; a quick filter may be a
  whole expression.
- The Run, Plan and Export actions sit in a bar that stays in reach at any
  scroll position and width.
- A run's page follows it live — progress per shard and, where the runner
  declares groups, per group; what is running right now; cancel — and shows
  failures first with durations, attempts and each shard's log and report.
  After a cancel a missing item says what it was doing when the run stopped.
  Run again, re-run what did not pass (optionally under a new title, with a
  link back to the source run), load the selection back into the builder,
  export, close as it stands, delete.
- Export downloads the server's exact bytes and shows the ingest token once;
  the document can be previewed before and downloaded again later. Results
  can be downloaded as CTRF.
- The inbox names runs by title, asks before discarding and has a count in
  the navigation.
- English and German throughout, including states, results and error codes.
  Keyboard operation of the main flow, phone-sized screens, light and dark.
- Browser end-to-end tests (`npm run e2e`) drive the built UI against real
  servers; unit and component tests run under vitest; `npm run lint` is
  eslint plus the TypeScript check.

API additions that came with it:

- A dispatch reports `planned_verdicts`; each shard of a run's roll-up
  reports `planned` (both null when a runner declared shard names only).
- An `invalid-parameter` error carries `parameter`, the name of the option.
- `GET /api/v1/runs/{id}/export?format=ctrf` — the document `runcomposer
  export` writes, as a download.
- A `text/html` artifact is served with `sandbox allow-scripts` — an origin
  of its own, nothing loadable or reachable — so a framework's log renders.
  Every other type keeps the policy without script.

### Known limits

Each written down with its reason in [docs/connectors.md](docs/connectors.md#10-known-limits):

- Declined: a taxonomy leaf holding a full filter expression; a per-runner
  custom "actions" endpoint; a catalog cache shared across CLI invocations;
  a `source.root` the executor would read from the run spec.
- Values shorter than four characters taken from the environment are not
  redacted; links a runner records are stored as given.
- Restart recovery cannot tell a reused process id from the process that
  owned a dispatch; `cancel --force` closes such a dispatch.
- One instance owns a store: dispatching one run twice is refused across
  processes, but instances are not coordinated (`DESIGN.md` §13).
- The Windows CI job has not been seen green yet and does not block.

## 0.1.7 — 2026-09-03

### Fixed
- **Adding a filter opens the picker.** In 0.1.6 the picker sat *beside* the
  filter builder's own "add" button rather than replacing it, so the most
  obvious control in the panel still opened the builder's inline editor — and
  that editor, handed the catalog's tags (also new in 0.1.6), rendered them as
  a flat, unsorted, unsearchable list of every one of them with a checkbox each, four
  rows visible, **every box ticked by default**. That is the screen the picker
  exists to replace, reached by the most likely click. Every `add-rule` is now
  intercepted and opens the dialog, and the builder is given no tag list at
  all, which removes that list from everywhere it could appear. Choosing tags
  happens in the dialog; the builder shows and edits the resulting expression,
  and its row menu's `Edit` still takes any raw `regex:` / `prefix:` pattern.

  0.1.6 justified leaving that button alone on the grounds that the widget's
  `add-rule` payload does not name the group the rule was headed for, so an
  interception could not honour a per-row add. That is still true and it was
  the wrong trade: where a group lands is now the dialog's own "join with the
  current filter" choice, which is visible and stated.

### Changed
- **The dialog's layout is settled**: four controls in a fixed order
  (include/exclude, how the picked tags combine, how the group joins what is
  there, search last), the multi-select hint above a bordered tree box, a dot
  on leaf rows, a kind badge on rows standing for one concrete tag, and a
  footer of "Selected: N" against Cancel and Apply.

  Dropped with it: the expression preview line and the staged-removal state,
  both added in 0.1.6. A row whose pattern the
  filter already carries still shows a `✓`; it is a mark, not a checkbox, and
  nothing is ever pre-selected. Removing a condition is the filter panel's
  chips, each with a one-click `✕`.

## 0.1.6 — 2026-09-03

### Added
- **The tag tree is a picker dialog, and a whole selection is one filter
  group.** Resolving the taxonomy against the catalog (0.1.4) made the tree
  complete and, in the same move, unusable: the resolved tree has a node for
  every catalog tag, so it grows with the catalog, and it was rendered as an
  always-visible column with no search. `Pick tags…` in the filter panel now
  opens a modal over that tree with

  - a **live search that narrows without flattening** — a matching node keeps
    all of its children, so a hit stays under the heading that explains it,
    and a branch matching nothing is dropped;
  - **multi-select** with checkbox, space, shift-range and ctrl/cmd, full
    arrow-key navigation, and a focus trap that returns focus to the button
    that opened it;
  - **three explicit controls** — include or exclude, how the picked tags
    combine with each other, how the group joins the filter already built —
    shown next to the expression they produce, before it is applied;
  - **one apply**, which is also the usable road to a nested filter:
    `Checkout AND (Payments OR Cart)` is two tags with "match any of them"
    and "AND", not a filter assembled row by row. It produces the same
    `selection.tag_filter` as the builder's own `Add group` — the AST is the
    contract ([docs/taxonomy.md](docs/taxonomy.md#what-a-click-does)).

- **The filter reads back as one sentence.** `Filter as a sentence` in the
  filter panel renders the whole `tag_filter` in the run spec's own grammar —
  `(Payments OR Cart) AND NOT Quarantine-Flaky`. The builder shows
  widgets and the chip row is flat; this is where a nested filter is legible to
  somebody who did not build it.

- **The filter's value field completes against the catalog's real tags.** It
  was handed an empty options map, so typing a tag was blind and you had to
  know its spelling by heart. `GET
  /api/v1/taxonomy?resolve=true` now also serves `tags`, the catalog's whole
  tag universe in the tree's own order. It is **not** derivable from the nodes:
  a written leaf whose pattern resolves to exactly one tag already *is* that
  tag's node and gets no tag child, so a tag reached only through
  `regex:^Cart(V2)?$` is spelled nowhere in the tree. The default
  `GET /api/v1/taxonomy` is unchanged and carries no `tags`.

- **`react-aria-components`** (Apache-2.0) for the dialog's tree and modal.
  Deliberate: a multi-select tree with real keyboard and screen-reader
  semantics is the reason this work exists and a bad thing to hand-roll. It
  costs the bundle **+58 kB gzip** (90.6 → 158.8 kB gzip; 287 → 510 kB raw).
  Its `Virtualizer` is what makes the tree viable at this size — see below.

### Fixed
- **Excluding a selection negates the group, not each row.** The glue flips
  under de Morgan, and getting it backwards is silent and plausible-looking
  rather than an error. On the shipped demo corpus (60 items),
  `NOT (Payments OR Cart)` is 38 items and so is the
  `(NOT Payments) AND (NOT Cart)` the picker writes — while
  `(NOT Payments) OR (NOT Cart)` is 60, the entire corpus: an exclusion that
  excludes nothing. The four numbers are pinned by a test. The picker cannot produce that shape, and
  under "exclude" its combine control offers exactly **one** reading, "match
  none of them", because the other one ("does not carry all of them") is
  almost never meant and cannot be worded apart from the first. The raw
  builder can still express it.

- **A ticked list of tag values no longer disappears from the filter.**
  Offering the catalog's tags as options lets the widget's value editor write
  an `includes` list, which the adapter translated to *nothing* — ticking two
  tags silently dropped the condition, narrowing the preview with nothing
  anywhere to say why. A list is now an OR of literals, and a negated list
  the AND of the negations (the same de Morgan step), listed as
  `is any of` / `is none of`.

- **A one-letter search no longer freezes the dialog.** Searching opens every
  branch it kept, and one letter keeps nearly every node: that put every row in
  the DOM at once. The tree is virtualized (only the visible rows are in the
  DOM), and past a budget of 400 matches the branches stay closed with the
  count shown instead of being thrown open — nothing hidden, nothing
  truncated.

- **Closing the picker returns focus to the button that opened it.** The dialog
  restores focus to whatever was focused when it opened, which on macOS is
  nothing — clicking a button there does not focus it — so a keyboard user was
  dropped on `<body>` and had to tab back through the whole page.

### Changed
- **The taxonomy sidebar is gone.** The compose view is one column, and the
  freed width goes to the filter and the preview. What survives from 0.1.5 is
  the part that was about the *filter* rather than the panel: a pattern the
  filter already carries is marked in the tree, the mark follows the **pattern
  and not the position** (the only reading that holds once the tree is
  resolved), and a closed branch carries the count of distinct marked patterns
  hidden below it. A row now has three visually distinct states — selected for
  this round, already in the filter, staged for removal — and pressing the mark
  stages a removal, so the dialog edits the tag part of the filter in both
  directions and `Cancel` cancels both. `ui/src/components/TaxonomyTree.jsx`,
  its styles and its locale keys are deleted rather than left unused.

### Not done
- **The picker does not take over the filter builder's own "add a filter"
  button.** Intercepting `add-rule` works, but the payload does not say where
  the rule was going: in `@svar-ui/react-filter` 2.6.0 both the toolbar's add
  and a row menu's add send `{rule, edit}` where `rule` is the click's own
  React event object (probed live). The two are therefore indistinguishable and
  neither names a target group, so an interception would always have to append
  at the top level — silently breaking the "add a group, then add a rule inside
  it" path 0.1.5 opened up. The picker sits beside the builder's own add, as
  the panel's primary action; the raw row is no longer blind either, now that
  its value field completes against real tags.
- **`include` / `exclude` are not two builder fields.** The single `tag` field
  keeps its eight operators. The non-technical user no longer meets an operator
  at all — that is what the picker is for — and the readable sentence plus the
  `Tag ≠ x` chips already say a negation in words, so a second field would
  double every field-dependent path in the adapter to restate what is already
  legible. The trap it was proposed to help with (a negated leaf inside an OR
  group) exists identically either way, and what makes it visible is the
  sentence, not the field name.

## 0.1.5 — 2026-09-03

### Fixed
- **Removing a single filter condition was a hidden click.** SVAR's filter rows
  carry their own menu — Edit, Add filter, Add group, Delete — behind an icon
  the widget draws with an icon font that `@svar-ui` does not ship: no
  `@font-face` for `wxi` exists anywhere in the package, so the trigger
  rendered as a blank 20px box. Everything worked; nothing was visible. The
  glyphs are drawn now, so the widget's own per-row controls (including nested
  groups, which `type="list"` supported all along) are reachable, and the
  filter panel lists each condition as a chip with a one-click `✕` above the
  builder. `Clear` is unchanged.
- **A taxonomy node is a switch, not an append-only button.** Clicking a node
  already in the filter removes it instead of adding a second identical rule,
  and every node currently in the filter is marked in the tree. The mark
  follows the *pattern*, not the position — which is the only reading that
  holds on the resolved tree of 0.1.4, where a tag hangs under every node whose
  pattern covers it: all of its rows light up and any of them switches it off.
  A closed branch carries the number of distinct active patterns hidden
  underneath it, so nothing active is invisible. Quick-filter chips toggle the
  same way.
- **Adding a condition no longer throws the filter builder away.** The panel
  remounted the whole widget on every taxonomy click (`key={locale-revision}`),
  and re-seeded its store on every render besides, because `fields` and
  `options` were fresh objects each time. The widget now keeps its state
  through its own edits; only an external change (taxonomy, quick filter, `✕`,
  `Clear`) hands it a new value, and a remount (locale or theme switch) hands
  it the current one.

### Added
- **`npm test` in `ui/`** — Node's own test runner over the filter adapter (no
  new dependency): the toggle, per-condition removal, the operator vocabulary,
  and the nested-group round trip that a click-through cannot pin down. Runs
  in CI alongside the bundle check.
- A locale guard in the Python suite: EN and DE must carry the same keys, no
  message may be empty, and the bundled `ui_dist` locales must match the
  source.
## 0.1.4 — 2026-09-03

### Added
- **The taxonomy tree resolves against the catalog.** The filter of a written
  leaf is a single pattern, so a pattern standing for a family of tags rendered as
  a single opaque node: `regex:^Cart(V2)?$` was one row, and the tags it
  covered had no row of their own and could not be picked. Now every
  node gains one child per concrete catalog tag its own pattern matches, each
  individually selectable; a node whose subtree matches nothing collapses away
  instead of clicking and selecting nothing; and the tags no node anywhere
  claims are gathered under one synthetic node, so nothing in the catalog is
  unreachable. On the shipped demo world that takes tags with a node of their
  own from 9 of 47 to **47 of 47**.

  **The taxonomy file format does not change.** Resolution is derived from the
  live catalog and never written back, and resolved nodes speak the same three
  keys the file is written in — `label`, `filter`, `children` — so the result
  is itself a valid taxonomy document. The additive metadata is `id`,
  `origin`, `tag_count` and `item_count`
  ([docs/taxonomy.md](docs/taxonomy.md#resolution-the-tree-the-ui-renders)).

- **`GET /api/v1/taxonomy?resolve=true`** serves that tree, plus a `resolved`
  summary (`tags_total`, `tags_claimed`, `tags_selectable`,
  `tags_unassigned`, `items_total`, `items_claimed`, `nodes_written`,
  `nodes_dropped`, `nodes_total`). **The published shape is unchanged**: the
  parameter defaults to off and `GET /api/v1/taxonomy` still returns the
  validated file verbatim, so an existing client sees exactly what it saw
  before and pays none of the resolution cost. Resolution happens server-side,
  against the same cached catalog the selection preview compiles over — the
  tree and the preview therefore cannot disagree about which tags exist, and
  the filter grammar stays in one implementation instead of being mirrored in
  JavaScript.

- **`runcomposer taxonomy-check --tree`** prints the resolved tree — every
  node with its pattern and its item/tag counts — which is the answer to "why
  does that node not show what I expected". The drift report itself gains a
  closing summary of what resolution reaches.

### Changed
- **The UI's taxonomy panel renders the resolved tree**, and therefore
  collapses: rows open and close, a branch's children are mounted only while
  it is open (the resolved tree grows with the catalog), each row shows
  its item count, and the first level opens on arrival. A leaf's accessible
  name is now its label — it was the pattern, so a screen reader read out a
  regular expression instead of one word. The pattern is still the tooltip.

## 0.1.3 — 2026-09-03

### Fixed
- **The sdist was 40 MB.** Hatchling's default sweeps in everything the VCS
  does not ignore, so the homepage's 37 MB of videos and a stray agent
  worktree were being packaged as source. The sdist is now an explicit
  include list; the wheel was always 0.2 MB and is unchanged.

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
  against the config file's directory, the plugin sections against the
  *working* directory, and the
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
  back, so the verdicts of a selection fanned out over two partitions could
  not be attributed to their partition through the port or the API.
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
  through an unscoped `latest_completed_run`, so "latest" ranged over every
  completed run in the store. Where several streams of runs shared an
  instance, a rerun could follow the wrong one and execute a different item
  set without any sign of it.
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
