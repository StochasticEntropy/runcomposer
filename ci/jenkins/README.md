# Jenkins demo instance — the §6.2b consumer stage

A reproducible local Jenkins that carries the **thin CI-side consumer**
runcomposer's `jenkins` runner drives (DESIGN.md §6.2b). Two parameterized
jobs, both provisioned by [`casc.yaml`](casc.yaml):

- **`runcomposer-consumer`** — the build step runs the vendored single-file
  `runcomposer-exec` on the passed spec, and the post step POSTs the results
  bundle (marker + the dispatch's ingest token) back to
  `/api/v1/runs/{id}/results` when it was given a callback URL. It archives
  `results/**` either way — the way back for polling.
- **`runcomposer-fanout`** — a job that fans out: it runs the passed spec
  once per region in `REGIONS` and reports each region as a shard of its
  own. It receives the document compressed (`SPEC_B64`, decoded in the shell
  step with `printf '%s' "$SPEC_B64" | base64 -d | gunzip > spec.json`), the
  feature tags of the selection as `INCLUDE`, and archives nothing. Without
  a callback URL it prints each region's results into its console log
  between `=== results <region> ===` and `=== end ===`; with one, every
  region's `runcomposer-exec` reports its progress and pushes its own bundle
  (`--callback "$CALLBACK_URL" --push`, the token in
  `RUNCOMPOSER_INGEST_TOKEN`).

**What is tested where.** The automated test suite exercises the `jenkins`
runner against an in-process, protocol-faithful HTTP stand-in
(`tests/test_jenkins.py`, `tests/test_jenkins_toolkit.py`): the crumb issuer
and its session cookie, `buildWithParameters` → queue item → build, a queue
item that expired, the build API, artifact download and URLs below a build,
stop and queue cancellation, Basic auth, retries, both completion modes end
to end, and the fan-out entries of the example config
(`tests/test_four_ways.py`, `test_2b_…`). Against a **real Jenkins**, [`e2e.py`](e2e.py) does the same with
this docker setup — one command, repeatable, cleaning up after itself:

```bash
python ci/jenkins/e2e.py            # needs docker; ~1 minute after the first image build
```

It builds and starts the instance on a free local port, copies
[`examples/jenkins/`](../../examples/jenkins) to a temporary directory and
serves its `config.yaml` with `runcomposer serve` (with that directory on
`PYTHONPATH`, for the two callables the fan-out entries name), and checks,
each step waiting on a condition rather than a timer (numbered as in the
script, whose steps 1, 2 and 12 start and stop the containers):

3. **health** — all four runner entries report `ok` from the real server,
   the server and the job checked;
4. **callback** — a run completes through the job's POST with its ingest
   token;
5. **poll** — a run started with `runcomposer run` completes through the
   downloaded `results/*`, and the CLI's exit code matches the completion;
6. **cancel, poll mode** and 7. **cancel, callback mode** — a running build
   is cancelled, the dispatch ending `CANCELLED` and Jenkins reporting the
   build `ABORTED`;
8. **plan** — `POST /api/v1/plan` for `jenkins-fanout` with the regions
   `eu` and `apac`: the preview names the two shards and the build
   parameters that would be sent (`REGIONS=eu,apac`, the `INCLUDE` computed
   from the selection, the compressed spec as `‹N bytes›`), and the fan-out
   job has no build afterwards — nothing was triggered;
9. **fan-out, polling** — a run on `jenkins-fanout` is one build with two
   shards, each region delivering a verdict for every planned item. The
   build received the shard list and the computed `INCLUDE`; its `SPEC_B64`
   decodes to exactly the bytes whose hash the dispatch recorded; it
   archived nothing, so the results came from `<build>/consoleText`, and the
   run's events show the post-download step cutting each region's block out
   of the console log;
10. **fan-out, pushing** — a run on `jenkins-fanout-push`: the job pushes one
   bundle per region with the dispatch's token, the run is `COMPLETE`, and
   its events hold one log line per region that the job pushed through
   `POST /api/v1/runs/{id}/log` while it ran;
11. **cancel after the queue item is forgotten** — Jenkins forgets a queue
   item a few minutes after it left the queue; from then on its URL answers
   `404`, and only the queue id recorded in the build leads to it. The
   script does not wait minutes: with a build of `runcomposer-consumer`
   holding, it tells Jenkins through its script console to drop the items
   that left the queue, waits for the queue item's URL to answer `404`,
   checks that the build is found by its queue id, and cancels the run. The
   dispatch is `CANCELLED`, Jenkins reports the build `ABORTED`, and
   runcomposer's events record a stop of the build — not a cancel of a queue
   item that was no longer there.

It is not part of CI (it needs docker and a minute of Jenkins start-up); run
it when you change the runner, the jobs or the image.

**Pinned.** The base image is pinned by version and digest and every plugin
by version ([`plugins.txt`](plugins.txt)), so a rebuild months from now is
the same Jenkins. Moving on is a deliberate edit of both.

> ## ⚠ These jobs run no tests
>
> The shipped build steps call `runcomposer_exec.py … --simulate`. **`--simulate`
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
docker run --rm -d -p 127.0.0.1:8080:8080 --name runcomposer-jenkins runcomposer-jenkins
# the jobs are provisioned by JCasC + job-dsl; ready when this answers:
curl -sf localhost:8080/job/runcomposer-consumer/api/json
```

runcomposer side: [`examples/jenkins/config.yaml`](../../examples/jenkins/config.yaml)
configures the one plugin four times. For `runcomposer-consumer`: `jenkins`
with `completion: callback` (`callback_base` is how the container reaches
your `runcomposer serve`; with docker on Linux start the container with
`--add-host host.docker.internal:host-gateway`) and `jenkins-poll` with
`completion: poll`, for a Jenkins that cannot call out. Its `parameters` map
each build-parameter name of *your* job to a template; the runner's default
mapping is:

```yaml
    parameters:
      SPEC_JSON: "{spec_json}"         # the exact document bytes; their hash is recorded
      DISPATCH_ID: "{dispatch_id}"
      CALLBACK_URL: "{callback_url}"   # empty with completion: poll
      INGEST_TOKEN: "{token}"          # the ingest token of this dispatch
```

Other fields a template may use: `{run_id}`, `{item_ids}` (one per line),
`{title}`, `{labels[key]}`, and `{params[name]}` for per-run parameters
declared under `run_parameters` — the example config passes
`{params[hold_s]}` as the job's `HOLD_SECONDS`, which is how `e2e.py` gets a
build that is still running when it cancels it. The job
([`casc.yaml`](casc.yaml)) declares `INGEST_TOKEN` as a Jenkins **password
parameter**: masked on the build's parameters page, never a visible plain
string. (Configuration-as-Code interpolates `${…}` in that file; the shell
step writes `^${…}` where the shell must see it.) The spec document itself
carries no credential. Against a secured Jenkins, add `user:` and
`api_token: ${env:JENKINS_API_TOKEN}`; for a private CA, `ca_bundle:`.

For `runcomposer-fanout`, two more entries show what a job of its own shape
needs, configured rather than coded:

- **`jenkins-fanout`** (`completion: poll`) — `shards: "{params[regions]}"`
  takes the shard list from a per-run `multi_choice` parameter (`regions`:
  `eu`, `us`, `apac`), and `REGIONS: "{shards}"` hands it to the job. The
  document travels as `SPEC_B64: "{spec_b64}"` — the exact bytes, gzip +
  base64, one line of ASCII that survives any form field and shell; the job
  decodes it with `base64 -d | gunzip`. (`{spec_json}`, the default, puts the
  whole document into one build parameter, which is also an environment
  variable of the build and limited in size; `max_parameter_bytes` refuses
  such a trigger before it is sent and names this way out.)
  `INCLUDE: "{include}"` is computed by a field provider,
  `field_providers: { include: "shop_job:include_tags" }`: the feature tags
  of the selected tests — and a refusal, before anything is triggered, for a
  selection holding a test of a feature the job is not set up for. The job
  archives nothing, so `result_urls: [consoleText]` downloads the build's
  console log, and `postprocess: "shop_job:results_from_console"` cuts each
  region's block out of it into the results file that is parsed.
- **`jenkins-fanout-push`** (`completion: callback`) — the same job, with
  `CALLBACK_URL` and `INGEST_TOKEN` added to its parameters: each region's
  `runcomposer-exec` pushes its own bundle and reports progress (log lines,
  live status) with the dispatch's token, so nothing is downloaded.

Both callables live in [`examples/jenkins/shop_job.py`](../../examples/jenkins/shop_job.py),
written against `runcomposer.sdk` alone; the server must be able to import
it — start it with `PYTHONPATH=examples/jenkins` (as `e2e.py` does with its
copy). To see what a fan-out run would send without triggering anything:

```bash
PYTHONPATH=examples/jenkins runcomposer plan Payments-Cards --runner jenkins-fanout \
    --param 'regions=[eu,apac]' --config examples/jenkins/config.yaml
```

Then: `runcomposer serve --host 0.0.0.0 --config examples/jenkins/config.yaml` (so the
container can call back) and dispatch a run to the runner — from the UI, with
`POST /api/v1/runs`, or from a second shell:

```bash
runcomposer run Checkout --runner jenkins --config examples/jenkins/config.yaml
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
