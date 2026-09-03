# Changelog

## 0.1.7 — 2026-09-03

### Fixed
- **Adding a filter opens the picker.** In 0.1.6 the picker sat *beside* the
  filter builder's own "add" button rather than replacing it, so the most
  obvious control in the panel still opened the builder's inline editor — and
  that editor, handed the catalog's tags (also new in 0.1.6), rendered them as
  a flat, unsorted, unsearchable list of all 1609 with a checkbox each, four
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
- **The dialog follows the predecessor's layout rather than a rearrangement of
  it**: the four controls in its order (include/exclude, how the picked tags
  combine, how the group joins what is there, search last), the multi-select
  hint above a bordered tree box, a dot on leaf rows, a kind badge on rows
  standing for one concrete tag, and a footer of "Selected: N" against Cancel
  and Apply. Sized as it was: `min(980px, 96vw)`, 85vh, 52vh of tree.

  Dropped with it: the expression preview line and the staged-removal state,
  both added in 0.1.6 and neither part of that design. A row whose pattern the
  filter already carries still shows a `✓`; it is a mark, not a checkbox, and
  nothing is ever pre-selected. Removing a condition is the filter panel's
  chips, each with a one-click `✕`.

## 0.1.6 — 2026-09-03

### Added
- **The tag tree is a picker dialog, and a whole selection is one filter
  group.** Resolving the taxonomy against the catalog (0.1.4) made the tree
  complete and, in the same move, unusable: on a real corpus it is **2452
  nodes over 1609 tags** (up from ~150 written nodes), rendered as an
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
  know its spelling by heart — on 1609 tags, nobody does. `GET
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
  branch it kept, and one letter keeps all 2452 nodes: that put every row in
  the DOM and took **17 seconds** to settle. The tree is virtualized (21 rows
  in the DOM for the whole corpus), and past a budget of 400 matches the
  branches stay closed with the count shown instead of being thrown open —
  nothing hidden, nothing truncated. Every query now costs ~140 ms of
  main-thread work.

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
- **The taxonomy tree resolves against the catalog.** A written leaf carries
  exactly one pattern, so a pattern standing for a family of tags rendered as
  a single opaque node: `regex:^Cart(V2)?$` was one row, and the tags it
  covered had no row of their own and could not be picked. Over a real corpus
  that is most of the tag space hidden behind a handful of patterns. Now every
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
  it is open (the real corpora resolve to thousands of nodes), each row shows
  its item count, and the first level opens on arrival. A leaf's accessible
  name is now its label — it was the pattern, which for a composed alternation
  regex meant a screen reader read hundreds of characters of expression
  instead of one word. The pattern is still the tooltip.

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
