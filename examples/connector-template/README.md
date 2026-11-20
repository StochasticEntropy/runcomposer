# A runcomposer connector — copy this to start yours

This directory is a complete, installable connector package. It connects
runcomposer to a (made-up) web shop: the catalog is a YAML file of HTTP
checks, the runner requests each planned check's URL and compares the answer,
an observer writes one line per finished run. Copy the directory, rename the
package, replace the three plugins with yours — the packaging, the tests and
the import rule carry over unchanged.

Read the [connector guide](../../docs/connectors.md) alongside; this README
only says what is where.

## What is in it

| Path | What it is |
|---|---|
| `pyproject.toml` | the package: depends on `runcomposer`, registers its plugins under the `runcomposer.sources` / `.runners` / `.observers` entry-point groups |
| `src/shop_connector/checks_source.py` | the `http-checks` **source** — items, snapshot, `resolve`, `fingerprint` |
| `src/shop_connector/http_runner.py` | the `http-checks` **runner** — `describe`, `health`, `plan` (a preview built by the same function `dispatch` starts with), `dispatch` (refuse → declare → execute → deliver), a per-run parameter read from `ctx.parameters`, live status (`RUNNING`, then the verdict), cancellation |
| `src/shop_connector/summary_observer.py` | the `run-summary` **observer** |
| `tests/test_connector.py` | tests written with `runcomposer.testing`: the contract checks (the plan rules included), unit tests against `FakeDispatchContext` (its `preview`, the whole live stream in `live_updates`), a real deployment through `ConnectorHarness` — a run to the end, and one met while a check is running (`harness.start(…).wait_until(…)`) and cancelled |
| `tests/test_guard.py` | the import rule: `runcomposer.sdk` and `runcomposer.testing`, nothing else |
| `example/config.yaml` | a deployment that names the plugins by entry-point id, with the package's shared settings under `connectors:` |
| `example/checks.yaml` | the example catalog |
| `example/shop_server.py` | a tiny local web shop to point the checks at — a stand-in *target*, not part of the connector (tests can hold one endpoint to meet a check in flight) |

The runner is deliberately unlike runcomposer's bundled ones: it executes in
this process on a thread pool, needs no test framework and no result parser,
and delivers verdicts it built itself (`ctx.deliver(shard, verdicts=…)`).

## Try it

```bash
python -m venv .venv && . .venv/bin/activate
pip install runcomposer          # or: pip install -e /path/to/a/runcomposer/checkout
pip install -e ".[test]"
pytest -q                        # the kit's checks, unit tests, an end-to-end run

python example/shop_server.py --port 18765 &      # something to check
runcomposer doctor --config example/config.yaml   # plugins, distribution, health
runcomposer plan Smoke --runner shop --config example/config.yaml   # what would run; nothing happens
runcomposer run Smoke --runner shop --config example/config.yaml
cat example/state/summary.txt                     # what the observer wrote
kill %1
```

`runcomposer run` exits `0` only when the run is `PASS`. Start the shop with
`--broken checkout` and run `Checkout` to see a `FAIL` with its reason.

## Making it yours

1. Rename: the distribution name in `pyproject.toml`, the package directory
   `src/shop_connector`, the entry-point names (they are what a config file
   writes after `plugin:`), and `CONNECTOR = "shop-checks"` (the key under
   `connectors:` your plugins share).
2. Replace the plugins. Keep deriving from the SDK base classes
   (`SourceBase`, `RunnerBase`, `ParserBase`, `StoreBase`, `ObserverBase`):
   they supply every optional member, and runcomposer refuses at startup a
   plugin that lacks one.
3. Keep `tests/test_guard.py`. Import from runcomposer only `runcomposer.sdk`
   in your plugins and `runcomposer.testing` in your tests — those two keep
   their meaning within a minor line (`runcomposer>=0.2,<0.3`); everything
   else may change in any release.
4. Keep the kit's checks in your suite (`check_source`, `check_runner`,
   `check_parser`, `check_store`, `check_observer`) — when a later runcomposer
   adds a rule, they tell you before your users do.

Constructors must be free of side effects: runcomposer builds every
configured plugin once at startup to validate the configuration. Check and
store options there; connect to things in `dispatch` and `health` — not in
`plan`, which previews a dispatch and must not act on anything.

Read what the person starting a run chose from `ctx.parameters`;
`ctx.options` is the runner's config entry as written and nothing else.
