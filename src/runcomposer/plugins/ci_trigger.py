"""`ci-trigger` Runner (DESIGN.md §6.2b): drives an existing parameterized CI
job (Jenkins first). It templates the job URL from config, passes the rendered
spec + run id as build parameters, and receives results in one of two ways:

- ``completion: callback`` (default): the job's post step POSTs the results
  bundle (marker + per-run token) back to ``/api/v1/runs/{id}/results`` —
  that webhook-out POST *is* the completion signal. ``dispatch`` returns
  immediately; the run stays AWAITING_RESULTS until the delivery lands.
- ``completion: poll``: for CI systems that can't call out, runcomposer polls
  the CI build API until the build finishes, downloads the archived results
  artifacts, and records the delivery itself.

The CI side needs the thin consumer stage — a job step running
``runcomposer-exec spec.json`` plus the POST — shipped as a reproducible
Jenkins-in-docker setup under ci/jenkins/ (the §6.2b named deliverable).

HTTP is stdlib urllib on purpose: this plugin adds no dependencies.
"""

from __future__ import annotations

import base64
import http.cookiejar
import json
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from runcomposer.core.ids import new_ulid
from runcomposer.core.model import Verdict
from runcomposer.core.ports import DispatchHandle, DispatchRefused, RunnerInfo
from runcomposer.core.registry import PARSER_GROUP, resolve_plugin

__all__ = ["CiTriggerRunner"]

ARTIFACT_FILES = ("results/results.json", "results/runcomposer_run.json")


class CiTriggerRunner:
    runner_id = "ci-trigger"

    def __init__(
        self,
        base_url: str,
        job: str,
        callback_base: str | None = None,
        completion: str = "callback",  # callback | poll
        user: str | None = None,
        api_token: str | None = None,
        poll_interval_s: float = 2.0,
        timeout_s: float = 300.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.job = job
        self.callback_base = (callback_base or "").rstrip("/") or None
        self.completion = completion
        self.user = user
        self.api_token = api_token
        self.poll_interval_s = poll_interval_s
        self.timeout_s = timeout_s
        self.last_plan = ""
        self._store = None
        self._source = None
        # Jenkins binds CSRF crumbs to the web session — the crumb fetch and
        # the trigger POST must share cookies, hence one opener per runner.
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def _open(self, request: urllib.request.Request, timeout: float = 30):
        return self._opener.open(request, timeout=timeout)

    def bind(self, *, store=None, source=None, artifact_root=None) -> None:
        self._store = store
        self._source = source

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id=self.runner_id, capabilities=("ci-trigger", self.completion))

    # -- dispatch -----------------------------------------------------------------

    def dispatch(self, spec: Mapping[str, Any]) -> DispatchHandle:
        run_id = spec["run"]["id"]
        dispatch_id = new_ulid()
        callback = ""
        if self.completion == "callback":
            if not self.callback_base:
                raise DispatchRefused(
                    "ci-trigger completion=callback needs a callback_base runner option "
                    "(the URL under which the CI job can reach this runcomposer's API)"
                )
            callback = f"{self.callback_base}/api/v1/runs/{run_id}/results"
        spec_json = json.dumps(spec)
        # The consumer stage writes SPEC_JSON verbatim to spec.json, so this
        # hash equals the file hash runcomposer-exec puts in the marker —
        # enabling §5 marker verification on the CI path.
        import hashlib

        spec_sha256 = hashlib.sha256(spec_json.encode("utf-8")).hexdigest()
        params = {
            "SPEC_JSON": spec_json,
            "DISPATCH_ID": dispatch_id,
            "CALLBACK_URL": callback,
            "INGEST_TOKEN": spec.get("results", {}).get("token") or "",
        }
        job_url = f"{self.base_url}/job/{urllib.parse.quote(self.job)}"
        queue_url = self._trigger(job_url, params)
        self.last_plan = (
            f"ci-trigger: job {job_url} triggered (dispatch {dispatch_id}, "
            f"completion via {'webhook-out POST' if callback else 'build polling'})"
        )
        if self.completion == "poll":
            self._poll_and_ingest(spec, run_id, dispatch_id, queue_url, job_url)
        return DispatchHandle(
            dispatch_id=dispatch_id,
            shards=1,
            links={"job": job_url, "queue": queue_url or ""},
            spec_sha256=spec_sha256,
        )

    # -- HTTP plumbing --------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {}
        if self.user and self.api_token:
            credentials = base64.b64encode(f"{self.user}:{self.api_token}".encode()).decode()
            headers["Authorization"] = f"Basic {credentials}"
        crumb = self._crumb()
        if crumb:
            headers[crumb[0]] = crumb[1]
        return headers

    def _crumb(self) -> tuple[str, str] | None:
        try:
            body = self._get_json(f"{self.base_url}/crumbIssuer/api/json", headers={})
            return body["crumbRequestField"], body["crumb"]
        except Exception:
            return None  # no crumb issuer configured

    def _trigger(self, job_url: str, params: Mapping[str, str]) -> str | None:
        request = urllib.request.Request(
            f"{job_url}/buildWithParameters",
            data=urllib.parse.urlencode(params).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with self._open(request) as response:
                return response.headers.get("Location")
        except urllib.error.HTTPError as exc:
            raise DispatchRefused(
                f"CI trigger failed: {exc.code} {exc.reason} for {job_url}/buildWithParameters"
            ) from None
        except urllib.error.URLError as exc:
            raise DispatchRefused(f"CI instance unreachable: {exc.reason} ({job_url})") from None

    def _get_json(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=self._headers() if headers is None else headers)
        with self._open(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def _download(self, url: str, target: Path) -> bool:
        request = urllib.request.Request(url, headers=self._headers())
        try:
            with self._open(request) as response:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(response.read())
            return True
        except urllib.error.HTTPError:
            return False

    # -- polling fallback (§6.2b) ------------------------------------------------------

    def _poll_and_ingest(self, spec, run_id, dispatch_id, queue_url, job_url) -> None:
        if self._store is None:
            raise DispatchRefused("ci-trigger completion=poll needs a bound store (in-process dispatch)")
        deadline = time.time() + self.timeout_s
        build_url = self._wait_for_build(queue_url, deadline)
        while time.time() < deadline:
            build = self._get_json(f"{build_url}/api/json")
            if build.get("result") is not None:
                break
            time.sleep(self.poll_interval_s)
        else:
            raise DispatchRefused(f"CI build did not finish within {self.timeout_s}s: {build_url}")

        bundle = Path(tempfile.mkdtemp(prefix="runcomposer-ci-"))
        fetched = [
            name for name in ARTIFACT_FILES
            if self._download(f"{build_url}/artifact/{name}", bundle / Path(name).name)
        ]
        if not fetched:
            raise DispatchRefused(f"CI build archived no results artifacts: {build_url}/artifact/")

        expected = (spec.get("results", {}).get("expect") or [{}])[0].get("format", "runcomposer-verdicts")
        parser = resolve_plugin(expected, PARSER_GROUP)()
        verdicts = []
        for parsed in parser.parse(bundle):
            item_id = self._source.resolve(parsed.native_name) if self._source else parsed.native_name
            if item_id is None:
                continue
            verdicts.append(Verdict(item_id, parsed.status, parsed.duration_ms, parsed.message))
        import hashlib

        payload = b"".join(sorted(p.read_bytes() for p in bundle.iterdir()))
        self._store.record_delivery(
            run_id,
            dispatch_id=dispatch_id,
            shard="1",
            content_hash="sha256:" + hashlib.sha256(payload).hexdigest(),
            format=expected,
            verdicts=verdicts,
        )

    def _wait_for_build(self, queue_url: str | None, deadline: float) -> str:
        if queue_url:
            queue_api = queue_url.rstrip("/") + "/api/json"
            while time.time() < deadline:
                item = self._get_json(queue_api)
                executable = item.get("executable")
                if executable and executable.get("url"):
                    return executable["url"].rstrip("/")
                time.sleep(self.poll_interval_s)
            raise DispatchRefused(f"queued CI build never started: {queue_url}")
        # No queue URL (older Jenkins): fall back to lastBuild once it exists.
        while time.time() < deadline:
            job = self._get_json(f"{self.base_url}/job/{urllib.parse.quote(self.job)}/api/json")
            last = job.get("lastBuild")
            if last and last.get("url"):
                return last["url"].rstrip("/")
            time.sleep(self.poll_interval_s)
        raise DispatchRefused("CI build never appeared on the job")
