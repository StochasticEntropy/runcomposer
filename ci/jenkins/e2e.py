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
8. Stop the server and the container (always, also on failure).

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
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
IMAGE = "runcomposer-jenkins:e2e"
CONTAINER = "runcomposer-jenkins-e2e"
JOB = "runcomposer-consumer"


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
        shutil.copytree(REPO / "examples" / "jenkins", example, ignore=shutil.ignore_patterns("state"))
        config = str(example / "config.yaml")
        env = {**os.environ, "JENKINS_URL": jenkins,
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
        check(all(h["status"] == "ok" for h in health.values()) and set(health) == {"jenkins", "jenkins-poll"},
              "both runner entries report ok health from the real Jenkins")

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
