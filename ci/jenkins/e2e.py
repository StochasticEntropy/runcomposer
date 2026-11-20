"""Drive the `jenkins` runner against a REAL Jenkins, end to end.

    python ci/jenkins/e2e.py              # build the image, run everything, clean up
    python ci/jenkins/e2e.py --no-build   # reuse an image built before

What it does, each step waiting on a condition (an HTTP answer, a run
state), never on a fixed sleep:

1. ``docker build`` the instance in this directory and start it on a free
   local port (``--add-host host.docker.internal:host-gateway``, so the job
   can call back on Linux as on Docker Desktop).
2. Copy ``examples/jenkins/`` to a temporary directory and start
   ``runcomposer serve`` on it, reachable from the container.
3. **health** — ``GET /api/v1/runners?refresh=true``: both runner entries
   probe Jenkins and the job.
4. **callback** — ``POST /api/v1/runs`` dispatching to ``jenkins``: the build
   runs ``runcomposer-exec`` and POSTs the bundle back with the dispatch's
   ingest token; the run becomes ``COMPLETE``.
5. **poll** — ``runcomposer run --runner jenkins-poll`` from the command line
   (against the same store): runcomposer follows the queue item to the
   build, downloads the archived results and delivers them.
6. **cancel, poll mode** — a build told to hold (``hold_s``) is cancelled
   while running: the dispatch is ``CANCELLED`` and Jenkins reports the
   build ``ABORTED``.
7. **cancel, callback mode** — a dispatch that waits for its callback is
   cancelled: runcomposer stops the build through a context rebuilt from the
   stored record; ``CANCELLED`` at once, the build ``ABORTED``.
8. **plan** — ``POST /api/v1/plan`` for the fan-out runner: the build
   parameters that would be sent, and nothing sent.
9. **fan-out, poll** — one build of ``runcomposer-fanout``, one shard per
   region chosen for the run: the document travels compressed, a build
   parameter is computed from the selection, the results are read from the
   build's console log (a URL below the build; the job archives nothing)
   and cut out of it per region before parsing.
10. **fan-out, pushing** — the same job pushes each region's bundle itself
    with ``runcomposer-exec --callback --push``, reporting log lines and live
    status through the authenticated progress endpoints while it runs.
11. **cancel after the queue item is forgotten** — Jenkins drops a queue item
    a few minutes after it left the queue. Here the server is told to forget
    it now (its script console empties that cache); the queue item's URL
    answers 404, the build is found by the queue id it records, and the
    cancel stops it.
12. Stop the server and the container (always, also on failure).

What is real: Jenkins, its queue and executors, the job's shell step, the
archived artifacts, the HTTP callback, runcomposer's server and CLI. What is
not: the job's ``runcomposer-exec --simulate`` fabricates verdicts instead
of running tests — the container has no test corpus (see README.md,
"Making it real"). So a pass proves the transport and the runner, not any
test.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
IMAGE = "runcomposer-jenkins:e2e"
CONTAINER = f"runcomposer-jenkins-e2e-{os.getpid()}"
JOB = "runcomposer-consumer"
FANOUT = "runcomposer-fanout"
#: Groovy for the script console: empty the cache in which Jenkins keeps the
#: items that left its queue (it does so by itself after five minutes).
FORGET_LEFT_ITEMS = (
    "def queue = Jenkins.instance.queue; "
    "def field = queue.getClass().getDeclaredField('leftItems'); "
    "field.setAccessible(true); "
    "field.get(queue).invalidateAll(); "
    "println 'forgotten'"
)


def say(message: str) -> None:
    print(f"[e2e {time.strftime('%H:%M:%S')}] {message}", flush=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http(method: str, url: str, body: Any = None, timeout: float = 30) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read(), error.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode("utf-8", "replace")


def wait_for(what: str, probe: Callable[[], Any], timeout: float, interval: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = probe()
        except OSError:
            value = None
        if value:
            return value
        if time.monotonic() > deadline:
            raise SystemExit(f"FAILED: timed out after {timeout:g}s waiting for {what}")
        time.sleep(interval)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAILED: {message}")
    say(f"ok: {message}")


def fan_out_and_forgotten_queue_items(api: str, jenkins: str, settle: Callable[..., dict[str, Any]]) -> None:
    """Steps 8–11: the fan-out job in both modes, and a cancel that has to
    find its build without the queue item."""
    from runcomposer.sdk.jenkins import JenkinsClient, JenkinsError, queue_id_of

    def builds_of(job: str) -> list[dict[str, Any]]:
        listing = http("GET", f"{jenkins}/job/{job}/api/json?tree=builds[number,url,queueId,result]")[1]
        return list(listing.get("builds") or [])

    def build_parameters(build_url: str) -> dict[str, str]:
        actions = http("GET", f"{build_url}/api/json?tree=actions[parameters[name,value]]")[1]["actions"]
        return {p["name"]: p.get("value", "") for a in actions for p in a.get("parameters", [])}

    # 8. plan
    status, plan = http("POST", f"{api}/plan", {
        "selection": {"tag_filter": "Payments-Cards"}, "runner": "jenkins-fanout",
        "parameters": {"regions": ["eu", "apac"]}})
    check(status == 200 and plan["refused"] is False, f"POST /plan answered {status} with a plan")
    sent = plan["plan"]["data"]["build_parameters"]
    say(f"plan: shards {[s['shard'] for s in plan['plan']['shards']]}; would send {json.dumps(sent)}")
    check([s["shard"] for s in plan["plan"]["shards"]] == ["eu", "apac"] and sent["REGIONS"] == "eu,apac"
          and sent["INCLUDE"] == "Payments-Cards" and sent["SPEC_B64"].startswith("‹"),
          "the preview names the shards and the build parameters, the computed INCLUDE among them")
    check(builds_of(FANOUT) == [], "the preview triggered nothing: the fan-out job has no build")

    # 9. fan-out, poll: results from the console log
    status, created = http("POST", f"{api}/runs", {
        "selection": {"tag_filter": "Payments-Cards"}, "title": "Cards in two regions (console log)",
        "dispatch": {"runner": "jenkins-fanout", "parameters": {"regions": ["eu", "apac"]}}})
    check(status == 201, "dispatch to 'jenkins-fanout' accepted" + ("" if status == 201 else f" — {created}"))
    done = settle(created["run"]["id"], ("COMPLETE", "FAILED", "CANCELLED"))
    dispatch = done["dispatches"][-1]
    say(f"fan-out run {done['id']}: {done['state']} ({done['completion']}) {done['verdict_summary']}; "
        f"shards {[(s['shard'], s['delivered'], s['count']) for s in done['shards']]}; error {dispatch['error']!r}")
    check(done["state"] == "COMPLETE" and dispatch["shards"] == ["eu", "apac"]
          and all(s["delivered"] and s["count"] == done["planned_count"] for s in done["shards"]),
          "one build, two shards: each region delivered a verdict for every planned item")
    build_url = dispatch["links"]["build"]
    params = build_parameters(build_url)
    say(f"build {build_url}: REGIONS={params['REGIONS']!r} INCLUDE={params['INCLUDE']!r} "
        f"SPEC_B64 is {len(params['SPEC_B64'])} characters")
    check(params["REGIONS"] == "eu,apac" and params["INCLUDE"] == "Payments-Cards",
          "the build received the shard list and the build parameter computed from the selection")
    import hashlib

    from runcomposer.sdk.jenkins import decode_spec

    check(hashlib.sha256(decode_spec(params["SPEC_B64"])).hexdigest() == dispatch["spec_sha256"],
          "the compressed spec the build received decodes to exactly the bytes whose hash was recorded")
    archived = http("GET", f"{build_url}/api/json?tree=artifacts[relativePath]")[1]["artifacts"]
    check(archived == [], "the build archived nothing — the results came from <build>/consoleText")
    taken = [e["data"]["message"] for e in done["events"] if "taken from the console log" in str(e["data"].get("message"))]
    check(len(taken) == 2, f"the post-download step cut each region's results out of the console log: {taken}")

    # 10. fan-out, pushing: results and progress from the job itself
    status, created = http("POST", f"{api}/runs", {
        "selection": {"tag_filter": "Payments-Wallets"}, "title": "Wallets in two regions (pushed)",
        "dispatch": {"runner": "jenkins-fanout-push"}})
    check(status == 201, "dispatch to 'jenkins-fanout-push' accepted" + ("" if status == 201 else f" — {created}"))
    done = settle(created["run"]["id"], ("COMPLETE", "FAILED", "CANCELLED"))
    pushed = [e["data"]["message"] for e in done["events"] if e["data"].get("source") == "push"]
    say(f"pushed run {done['id']}: {done['state']} ({done['completion']}) {done['verdict_summary']}; "
        f"log lines pushed by the job: {pushed}")
    check(done["state"] == "COMPLETE" and [s["shard"] for s in done["shards"]] == ["eu", "us"]
          and all(s["delivered"] for s in done["shards"]),
          "the job pushed one bundle per region with the dispatch's token and the run is COMPLETE")
    check(sum("item(s) to execute" in line for line in pushed) == 2,
          "the job reported progress through POST /runs/{id}/log while it ran (one line per region)")

    # 11. cancel after Jenkins has forgotten the queue item
    status, created = http("POST", f"{api}/runs", {
        "selection": {"tag_filter": "Auth-SSO"}, "title": "Cancel me, minutes later",
        "dispatch": {"runner": "jenkins", "parameters": {"hold_s": 300}}})
    check(status == 201, "dispatch to 'jenkins' (callback) with hold_s=300 accepted")
    run_id = created["run"]["id"]
    queue_url = created["dispatch"]["links"]["queue"]
    client = JenkinsClient(jenkins)
    build = wait_for("the held build to start", lambda: client.build_of(queue_url), timeout=180)
    say(f"queue item {queue_url} became {build}")
    answer = client.request("POST", f"{jenkins}/scriptText", data=urllib.parse.urlencode(
        {"script": FORGET_LEFT_ITEMS}).encode())
    with answer:
        said = answer.read().decode("utf-8", "replace").strip()
    check(said == "forgotten", f"Jenkins was told to forget the items that left its queue (script console: {said!r})")

    def forgotten() -> bool:
        try:
            client.build_of(queue_url)
        except JenkinsError as error:
            return error.status == 404
        return False

    wait_for("the queue item to answer 404", forgotten, timeout=30)
    check(True, f"GET {queue_url}api/json answers 404 — the queue item is gone")
    found = client.find_build(JOB, queue_id_of(queue_url) or -1)
    check(found == build, f"the build is found by the queue id it records: {found}")
    status, answer_json = http("POST", f"{api}/runs/{run_id}/cancel")
    check(status in (200, 202) and answer_json["dispatch"]["state"] == "CANCELLED",
          f"POST /runs/{run_id}/cancel answered {status} ({answer_json['dispatch']['state']})")
    aborted = wait_for("Jenkins to report the build finished", lambda: (lambda r: r[1] if r[1] and not r[1].get(
        "building") else None)(http("GET", f"{build}/api/json")), timeout=120, interval=1)
    check(aborted["result"] == "ABORTED",
          f"the build was stopped although its queue item no longer exists: result {aborted['result']}")
    events = http("GET", f"{api}/runs/{run_id}")[1]["events"]
    check(any("stop requested" in str(e["data"].get("message")) for e in events)
          and not any("queue item" in str(e["data"].get("message")) and "cancelled" in str(e["data"].get("message"))
                      for e in events),
          "runcomposer logged a stop of the build — not a 'cancel' of a queue item that was not there")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-build", action="store_true", help=f"reuse the image {IMAGE}")
    parser.add_argument("--keep", action="store_true", help="leave the container running at the end")
    args = parser.parse_args()
    # A SIGTERM (a CI timeout, a Ctrl-C'd parent) still runs the cleanup below.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    if not args.no_build:
        say(f"docker build -f ci/jenkins/Dockerfile -t {IMAGE} .")
        subprocess.run(["docker", "build", "-q", "-f", "ci/jenkins/Dockerfile", "-t", IMAGE, "."],
                       cwd=REPO, check=True)
    jenkins_port, serve_port = free_port(), free_port()
    jenkins = f"http://127.0.0.1:{jenkins_port}"
    workdir = Path(tempfile.mkdtemp(prefix="runcomposer-jenkins-e2e-"))
    server: subprocess.Popen[str] | None = None
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    say(f"docker run {IMAGE} on {jenkins}")
    subprocess.run(["docker", "run", "-d", "--rm", "--name", CONTAINER,
                    "--add-host", "host.docker.internal:host-gateway",
                    "-p", f"127.0.0.1:{jenkins_port}:8080", IMAGE], check=True, capture_output=True)
    try:
        version = wait_for(
            "Jenkins and the job to be provisioned",
            lambda: (lambda r: r[0] == 200 and r[1])(http("GET", f"{jenkins}/job/{JOB}/api/json")),
            timeout=300, interval=2,
        )
        say(f"Jenkins is up; job {version['name']!r} provisioned by JCasC + job-dsl")

        example = workdir / "jenkins"
        shutil.copytree(REPO / "examples" / "jenkins", example,
                        ignore=shutil.ignore_patterns("state", "__pycache__"))
        config = str(example / "config.yaml")
        # The example's two callables (shop_job.py) are named in its config.
        env = {**os.environ, "JENKINS_URL": jenkins, "PYTHONPATH": str(example),
               "RUNCOMPOSER_CALLBACK_BASE": f"http://host.docker.internal:{serve_port}"}
        api = f"http://127.0.0.1:{serve_port}/api/v1"
        say(f"runcomposer serve --host 0.0.0.0 --port {serve_port} --config {config}")
        server = subprocess.Popen(
            [sys.executable, "-m", "runcomposer.cli", "serve", "--host", "0.0.0.0", "--port", str(serve_port),
             "--log-level", "warning", "--config", config],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        wait_for("runcomposer serve", lambda: http("GET", f"{api}/health")[0] == 200, timeout=60, interval=0.2)

        # 3. health
        _, runners = http("GET", f"{api}/runners?refresh=true")
        health = {r["id"]: r["health"] for r in runners["runners"]} if isinstance(runners, dict) else {
            r["id"]: r["health"] for r in runners}
        say(f"health: {json.dumps(health)}")
        entries = {"jenkins", "jenkins-poll", "jenkins-fanout", "jenkins-fanout-push"}
        check(all(h["status"] == "ok" for h in health.values()) and set(health) == entries,
              "all four runner entries report ok health from the real Jenkins (server and job checked)")

        def settle(run_id: str, states: tuple[str, ...], timeout: float = 180) -> dict[str, Any]:
            return wait_for(f"run {run_id} to reach {states}",
                            lambda: (lambda d: d if d.get("state") in states else None)(http("GET", f"{api}/runs/{run_id}")[1]),
                            timeout=timeout, interval=1)

        # 4. callback
        status, created = http("POST", f"{api}/runs", {
            "selection": {"tag_filter": "Payments-Cards"}, "title": "Cards via Jenkins (callback)",
            "dispatch": {"runner": "jenkins"}})
        check(status == 201, f"POST /runs dispatching to 'jenkins' answered {status}"
              + ("" if status == 201 else f": {created}"))
        run_id = created["run"]["id"]
        say(f"callback run {run_id}: {created['dispatch']['state']}, handoff {created.get('handoff')}")
        done = settle(run_id, ("COMPLETE", "FAILED", "CANCELLED"))
        say(f"callback run {run_id}: {done['state']} ({done['completion']}) {done['verdict_summary']}")
        check(done["state"] == "COMPLETE" and sum(done["verdict_summary"].values()) == done["planned_count"],
              "callback mode: the job POSTed its bundle back and the run is COMPLETE with every planned item")
        check(any(e["kind"] == "delivery.recorded" for e in done["events"]), "the delivery arrived through the API push")

        # 5. poll, from the command line
        say("runcomposer run Payments-Wallets --runner jenkins-poll --json")
        cli = subprocess.run([sys.executable, "-m", "runcomposer.cli", "run", "Payments-Wallets", "--runner",
                              "jenkins-poll", "--title", "Wallets via Jenkins (poll)", "--json", "--config", config],
                             env=env, capture_output=True, text=True, timeout=300)
        polled = json.loads(cli.stdout)
        say(f"poll run {polled['id']}: exit {cli.returncode}, {polled['state']} ({polled['completion']}) "
            f"{polled['verdict_summary']}; artifacts {[a['name'] for a in polled['artifacts']]}")
        check(polled["state"] == "COMPLETE" and sum(polled["verdict_summary"].values()) == polled["planned_count"],
              "poll mode: runcomposer followed the queue item, downloaded results/* and delivered them")
        check(cli.returncode == (0 if polled["completion"] == "PASS" else 1),
              f"the CLI exit code ({cli.returncode}) matches the completion ({polled['completion']})")

        # 6./7. cancel a running build, in both modes
        for runner in ("jenkins-poll", "jenkins"):
            status, created = http("POST", f"{api}/runs", {
                "selection": {"tag_filter": "Auth-SSO"}, "title": f"Cancel me ({runner})",
                "dispatch": {"runner": runner, "parameters": {"hold_s": 300}}})
            check(status == 201, f"dispatch to {runner!r} with hold_s=300 accepted"
                  + ("" if status == 201 else f" — {status}: {created}"))
            run_id = created["run"]["id"]
            started = wait_for(f"the {runner} build to be running", lambda: (lambda r: r[1] if r[0] == 200 and r[1].get("building") else None)(
                http("GET", f"{jenkins}/job/{JOB}/lastBuild/api/json")), timeout=180, interval=1)
            say(f"{runner}: build #{started['number']} is running (holding); cancelling run {run_id}")
            status, answer = http("POST", f"{api}/runs/{run_id}/cancel")
            check(status in (200, 202), f"POST /runs/{run_id}/cancel answered {status} ({answer['dispatch']['state']})")
            cancelled = settle(run_id, ("CANCELLED", "COMPLETE", "FAILED"), timeout=120)
            check(cancelled["state"] == "CANCELLED", f"{runner}: the dispatch is CANCELLED")
            aborted = wait_for("Jenkins to report the build finished", lambda: (lambda r: r[1] if r[1] and not r[1].get("building") else None)(
                http("GET", f"{jenkins}/job/{JOB}/{started['number']}/api/json")), timeout=120, interval=1)
            check(aborted["result"] == "ABORTED", f"{runner}: Jenkins build #{started['number']} result is {aborted['result']}")
        fan_out_and_forgotten_queue_items(api, jenkins, settle)
        say("ALL CHECKS PASSED against a real Jenkins")
        return 0
    finally:
        if server is not None:
            server.terminate()
            try:
                output, _ = server.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                server.kill()
                output, _ = server.communicate()
            if output.strip():
                say("runcomposer serve said:\n" + output.strip())
        if not args.keep:
            subprocess.run(["docker", "stop", CONTAINER], capture_output=True)
            say(f"container {CONTAINER} stopped (started with --rm: removed)")
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
