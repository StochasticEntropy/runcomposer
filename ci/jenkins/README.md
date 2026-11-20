# Jenkins demo instance — the §6.2b consumer stage

A reproducible local Jenkins that carries the **thin CI-side consumer**
runcomposer's `jenkins` runner drives (DESIGN.md §6.2b): a parameterized job
whose build step runs the vendored single-file `runcomposer-exec` on the
passed spec, and whose post step POSTs the results bundle (marker + the
dispatch's ingest token) back to `/api/v1/runs/{id}/results`.

**What is tested where.** The automated test suite exercises the `jenkins`
runner against an in-process, protocol-faithful HTTP stand-in
(`tests/test_jenkins.py`): the crumb issuer and its session cookie,
`buildWithParameters` → queue item → build, the build API, artifact download,
stop and queue cancellation, Basic auth, retries, and both completion modes
end to end. This docker setup is the **manual end-to-end check** against a
real Jenkins — it is not run by CI, so run it when you change the runner or
the consumer stage.

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
  jenkins:
    base_url: http://localhost:8080
    job: runcomposer-consumer
    callback_base: http://host.docker.internal:8100   # how the container reaches your serve
    completion: callback        # or "poll" for a Jenkins that cannot call out
```

That is the whole config for this job, because the job's parameters carry the
runner's default names. They are configurable — `parameters` maps each
build-parameter name of *your* job to a template — and the default is:

```yaml
    parameters:
      SPEC_JSON: "{spec_json}"         # the exact document bytes; their hash is recorded
      DISPATCH_ID: "{dispatch_id}"
      CALLBACK_URL: "{callback_url}"   # empty with completion: poll
      INGEST_TOKEN: "{token}"          # the ingest token of this dispatch
```

Other fields a template may use: `{run_id}`, `{item_ids}` (one per line),
`{title}`, `{labels[key]}`, and `{params[name]}` for per-run parameters
declared under `run_parameters`. The demo job ([`casc.yaml`](casc.yaml))
declares `INGEST_TOKEN` as a plain string parameter, which keeps this
throwaway instance simple and means the token is readable on the build's
parameters page. On a real Jenkins bind `{token}` to a **password parameter**
(or hand it over through a credentials binding) — the runner only fills the
parameter you name, so nothing else changes. The spec document itself carries
no credential. Against a secured Jenkins, add
`user:` and `api_token: ${env:JENKINS_API_TOKEN}`; for a private CA,
`ca_bundle:`.

Then: `runcomposer serve --host 0.0.0.0 --config config.yaml` (so the
container can call back) and dispatch a run to the runner — from the UI, with
`POST /api/v1/runs`, or from a second shell:

```bash
runcomposer run Checkout --runner jenkins --config config.yaml
```

The runner triggers the build and returns; the run waits in
`AWAITING_RESULTS` until the job's POST arrives at the server, and the command
waits with it (`--no-wait` returns right after the trigger). With
`completion: poll` runcomposer instead follows the queue item to the build,
waits for it, downloads the archived files matching `artifact_paths` (default
`results/*`) and delivers them itself. Either way the build is identified
through the queue item the trigger returned — never by "the latest build of
the job" — and `runcomposer cancel <run-id>` stops it.

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
1 failed, 4 passed in 0.01s
runcomposer-exec: bundle ready at results (marker: runcomposer_run.json)

$ ls results
item_ids.txt
junit.xml
runcomposer_run.json

$ runcomposer ingest results
01M7WTA0TM18WY9NT8ZWXRQFNY shard 1: delivery recorded (5 verdict(s))
run state: COMPLETE (FAIL)
```

That is the difference the swap makes: a real failing test produces a real
`FAIL`, where `--simulate` would have produced whatever its seed dictated.

Two things to get right on the runcomposer side when you switch:

- **Name the format you will return.** `--simulate` writes the reference
  `runcomposer-verdicts` JSON, which is the default. A real command writes its
  framework's artifacts instead, so say so on the runner entry —
  `format: junit-xml` (or `robot-output-xml`) — and adjust the
  `-F "files=@results/…"` lines in the POST to match the files your command
  actually produced. Runs composed for the runner then expect that format.
- **Say which exit codes still mean "the results are valid".** The marker is
  the bundle's completion signal, so `runcomposer-exec` writes it only when
  your command exited with an accepted code — `0` by default — and otherwise
  fails the stage without one. A framework that exits non-zero when *tests*
  fail needs `--ok-exit-codes` (for example `0-249`), or an adapter that maps
  "tests failed" to `0` the way `run_pytest.py` does. With the marker written
  the stage is green and the verdict is in runcomposer, where it belongs; a
  red stage means the execution itself broke and nothing was delivered.

The full flag reference for both executables is in [docs/cli.md](../../docs/cli.md).
