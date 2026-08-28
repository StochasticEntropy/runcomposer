# Bring your own run agent — the remote round trip

A complete, runnable adopter kit for DESIGN.md §6.2c: runcomposer composes a
run spec **here**, the spec travels to a machine runcomposer never talks to,
a small agent **there** fulfills it with the vendored single-file
`runcomposer-exec`, the results bundle travels back, and runcomposer ingests
it. No API between the two sides, no shared installation — the document is
the interface.

This is the reference pattern §14 P4 names for company-internal adoption. A
private adopter copies these four files, points them at their own corpus and
their own transport, and keeps the result in their own repository; nothing of
theirs has to reach this one.

```bash
cd examples/remote-agent
./sync.sh
```

That runs the whole loop against a local directory transport, so the example
works on one machine, in CI, and in the test suite (`tests/test_remote_agent.py`).
It needs `runcomposer` with the robot extra on the composing side
(`pip install ".[robot]"` from a clone of this repository) and `python3` +
`robot` on the executing side — deliberately *not* runcomposer, which is what the vendored consumer
buys.

## What travels

| Direction | Payload | Why |
|---|---|---|
| out | `spec.json` | the run spec — identity, the materialized item list, the results contract (§3.1) |
| out | `run_agent.sh` + `robot_command.py` | the agent itself: one small shell script and one small Python adapter |
| out | `runcomposer_exec.py` | the vendored single-file consumer (§6.2c) — a release download in real life, copied from `src/` here |
| back | `output.xml` | the executing machine's own native artifact, parsed by `robot-output-xml` |
| back | `runcomposer_run.json` | the marker that carries correlation home (§5) |
| back | `item_ids.txt`, `drift.json` | what the consumer and the agent decided, for the human reading the bundle |

The tests are **not** in the payload. The executing machine already has its
checkout (`RC_SUITE_ROOT`); delivering code is the transport's other job, not
this loop's. The two sides are correlated by item id alone — never by path.

## The three executor obligations (§3.3), and where this kit meets them

1. **Execute exactly `selection.materialized.item_ids`.** `runcomposer-exec`
   writes that list to `item_ids.txt` and passes it as `{ids_file}`;
   `robot_command.py` turns it into `robot --test <id> …` and never
   re-compiles a filter. The `tag_filter` in the spec is provenance and is
   ignored over there.
2. **Check drift before executing.** See below — `robot_command.py` refuses
   by default.
3. **Deliver results that reference `run.id`.** The consumer writes the
   marker; `sync.sh` passes the dispatch id and a shard label so the delivery
   lands on the right dispatch of the right run.

Everything else in the document, including the whole `runner` section (which
this pattern does not even emit), may be ignored. That is the contract.

## Correlation: the marker and `spec_sha256`

`runcomposer spec --export` mints an **export dispatch** and records the
SHA-256 of the exact spec bytes it handed out. `runcomposer-exec` hashes the
spec file it read and writes both facts into the bundle:

```json
{
  "run_id": "01M14TNCF61XPESK544B9D86Q2",
  "spec_sha256": "47cf195aeb64c935b122d9cc2ab48225e98c2345fe93c7b76faab981bfe79669",
  "dispatch_id": "01M14TNCFDW4XC6605YAB0GTDC",
  "shard": "1"
}
```

Ingestion matches on `run_id` **and** verifies `spec_sha256` against the
dispatch. A bundle that executed a different spec than the one this run
exported is refused (`sha-mismatch`) and quarantined rather than folded into
a watched run — on a shared return directory, "this bundle is somebody else's
run" is routine, not exotic. Re-delivering byte-identical bytes is a no-op; a
*new* bundle for the same `(run, dispatch, shard)` replaces that shard's
verdicts, so a corrected re-run can flip FAIL→PASS (§5).

## Drift

The agent has no catalog and no `TestSource`, so it cannot recompute
`source.snapshot` — that algorithm belongs to the source plugin, and putting
a copy of it on every executing machine is exactly the coupling this pattern
avoids. It checks the property that actually governs *this* run instead:
every requested item id must exist in the live suite.

- **Default: refuse.** `robot_command.py` exits non-zero, writes `drift.json`
  naming the missing ids, and `sync.sh` stops there. Nothing travels back and
  the run stays `AWAITING_RESULTS` — visibly unfinished, which is the point.
- **`RC_ALLOW_DRIFT=1`:** the intersection is executed, `drift.json` rides
  home in the bundle, and the run completes with fewer verdicts than
  `materialized.count`. §3.3's *"report the difference as SKIP verdicts with
  reason drift"* rendering is what the in-process `robot-pool` runner does,
  because it writes to the store directly; an `output.xml` cannot express a
  verdict for a test that does not exist, so a returning agent names the
  difference in the bundle instead of inventing it.

One id-space trap worth knowing before it bites: a Robot longname begins at
the **suite root's directory name**, so a checkout that presents a different
top-level suite name drifts on every single id. The agent says so explicitly
when nothing matches at all.

## Swapping the transport

`sync.sh` has exactly one seam — `transport_send`, `transport_receive`, and
`remote_run`. The default `local` case is a directory copy; rsync, scp and
git variants sit commented next to it, each one line. Nothing else in the kit
knows how the bytes move, and no host, user, or path of anybody's
infrastructure appears anywhere in this directory: set `RC_REMOTE_HOST`
yourself when you uncomment an ssh-based case.

The git case is the one that shows the decoupling best: `remote_run` does
nothing at all, because that side runs on its own schedule and the next pull
finds the bundle waiting. Compose and result-return are decoupled by design
(§5) — the loop does not care whether execution took a second or a day.

On the return leg the bundle lands in the **file-drop inbox** declared in
`config.yaml`. `sync.sh` then calls `runcomposer ingest` so that the whole
loop is one script; with `runcomposer serve` running, the inbox watcher picks
up the very same drop by itself and that call is only an idempotent
re-delivery.

## Turning this template into a private adopter package

Copy the directory into your own repository and change four things:

- **`config.yaml`** — your corpus root, your store, your inbox. A private
  `TestSource` or `Runner` loads from the same file by import path
  (`module: "mypkg.sources:MySource"`) or entry point; no env vars, no forks
  (§6, §8).
- **`agent/robot_command.py`** — your framework's invocation. It is the only
  file that knows what a test *is*: swap `robot.run` for pytest, a Makefile,
  or a vendor CLI, and keep the contract (read the ids file, write native
  results into the out dir). `../pytest-shop/run_pytest.py` is the same
  adapter for pytest, in the same shape.
- **`sync.sh`** — your transport, and whatever your scheduler wraps around it.
- **`agent/run_agent.sh`** — usually nothing.

Your deployment specifics live in your package; the public core stays generic
(§11). If you find yourself wanting to add a field to a core spec section to
make this work, that is the signal to put it in `runner.*` or in your own
config instead.

## Paths and state

Every runcomposer command runs with `config.yaml`'s directory as the working
directory, because plugin options (the sqlite path, the source root) resolve
against the working directory while the core's ingestion and artifact dirs
resolve against the config file. That single `cd` in `sync.sh` makes both
bases agree, so a config of relative paths keeps all state in one place:
`state/`, which is gitignored and which `runcomposer gc` bounds.

To put the state somewhere else — a CI workspace, a shared checkout — copy
`config.yaml`, make its paths absolute, and point `RC_CONFIG` at the copy.
`sync.sh` follows the config, not the repository.
