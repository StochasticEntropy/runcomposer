# Jenkins demo instance — the §6.2b consumer stage

A reproducible local Jenkins that carries the **thin CI-side consumer**
runcomposer's `ci-trigger` runner drives (DESIGN.md §6.2b): a parameterized
job whose build step runs the vendored single-file `runcomposer-exec` on the
passed spec, and whose post step POSTs the results bundle (marker + per-run
ingest token) back to `/api/v1/runs/{id}/results`.

> ## ⚠ This job runs no tests
>
> The shipped build step calls `runcomposer_exec.py … --simulate`. **`--simulate`
> executes nothing.** It fabricates deterministic verdicts from the item ids in
> the spec — roughly 82% `PASS`, 15% `FAIL`, 3% `SKIP` — and the bundle it
> returns is indistinguishable, to runcomposer, from one produced by a real
> test run.
>
> That is deliberate here: this container has no test corpus, so simulating is
> the only way the loop can prove itself on any machine. It also means a build
> going green proves the **transport** works — trigger → spec → bundle → marker
> → token → ingest → `COMPLETE` — and proves nothing whatsoever about any test.
>
> If you copy this stage into a real job, **change `--simulate` to `--command`
> before you trust a single result.** See [below](#making-it-real).

```bash
# from the repo root
docker build -f ci/jenkins/Dockerfile -t runcomposer-jenkins .
docker run --rm -d -p 8080:8080 --name runcomposer-jenkins runcomposer-jenkins
# job is provisioned by JCasC + job-dsl; ready when this answers:
curl -sf localhost:8080/job/runcomposer-consumer/api/json
```

runcomposer side (`config.yaml`):

```yaml
runners:
  ci-trigger:
    base_url: http://localhost:8080
    job: runcomposer-consumer
    callback_base: http://host.docker.internal:8100   # how the container reaches your serve
    completion: callback        # or "poll" for CI systems that can't call out
```

Then: `runcomposer serve --host 0.0.0.0` (so the container can call back),
compose a spec, and `runcomposer dispatch --runner ci-trigger spec.json`.
Completion arrives via the job's webhook-out POST; with `completion: poll`
runcomposer instead polls the build API and ingests the archived artifacts.

The instance is **unsecured by design** (local demo). Never expose it.

---

## Making it real

The consumer has exactly two execution modes, and swapping them is a one-line
change to the build step. In [`casc.yaml`](casc.yaml) the shipped line is:

```bash
python3 /opt/runcomposer/runcomposer_exec.py spec.json --out results --simulate --dispatch "$DISPATCH_ID"
```

Replace `--simulate` with `--command`, pointing at whatever actually runs your
tests:

```bash
python3 /opt/runcomposer/runcomposer_exec.py spec.json --out results \
    --command "./run-tests.sh {ids_file} {out_dir}" --dispatch "$DISPATCH_ID"
```

Everything else in the stage — the marker, the token header, the multipart
POST, the artifact archiving — stays exactly as it is. The consumer expands
three placeholders into your command:

| Placeholder | What it becomes |
|---|---|
| `{ids_file}` | A file it writes, holding one item id per line — the authoritative set to execute. |
| `{out_dir}` | The output directory. Your command writes its native result files here. |
| `{run_id}` | The `run.id` from the spec. |

Your script is handed the ids and the directory, and is responsible for
nothing else. A working one ships in this repository —
[`examples/pytest-shop/run_pytest.py`](../../examples/pytest-shop/run_pytest.py),
which reads the ids as pytest nodeids, runs exactly those, and writes
`junit.xml` into the output directory. Here is that adapter driven by the same
consumer, outside Jenkins:

```console
$ runcomposer-exec spec.json --out results \
      --command "python examples/pytest-shop/run_pytest.py {ids_file} {out_dir}"
runcomposer-exec: running: python examples/pytest-shop/run_pytest.py results/item_ids.txt results
....F                                                                    [100%]
=================================== FAILURES ===================================
__________________________ test_total_never_negative ___________________________
    def test_total_never_negative():
        # Deliberately red: the example corpus ships one failing test.
        total = 10 - 25
>       assert total >= 0, "simulated defect: negative cart total"
E       AssertionError: simulated defect: negative cart total
…
1 failed, 4 passed in 0.02s
runcomposer-exec: bundle ready at results (marker: runcomposer_run.json)

$ ls results
item_ids.txt  junit.xml  runcomposer_run.json

$ runcomposer ingest results
01M151XCGNSBVQN52Z83J4QPEY shard 1: delivery recorded (5 verdict(s))
run state: COMPLETE (FAIL)
```

That is the difference the swap makes: a real failing test produces a real
`FAIL`, where `--simulate` would have produced whatever its seed dictated.

Two things to get right on the runcomposer side when you switch:

- **Name the format you will return.** `--simulate` writes the reference
  `runcomposer-verdicts` JSON, which is the default. A real command writes its
  framework's artifacts instead, so compose with
  `runcomposer spec … --expect-format junit-xml` (or `robot-output-xml`) and
  adjust the `-F "files=@results/…"` lines in the POST to match the files your
  command actually produced.
- **The exit code is yours.** `runcomposer-exec` returns whatever your command
  returned, so the Jenkins build goes red when the tests did — which
  `--simulate` never does.

The full flag reference for both executables is in [docs/cli.md](../../docs/cli.md).
