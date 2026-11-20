"""The ``http-checks`` runner: executes the planned HTTP checks against a web
shop, in this process, with a small pool of threads.

The whole runner contract in one file (``runcomposer.sdk.Runner``):

- ``describe()`` — who it is, what it can do (``live_status``, ``cancel``,
  ``plan``), and the per-run parameter it accepts: which environment to check.
- ``health()`` — can it take a dispatch right now? It asks the shop.
- ``plan(ctx)`` — what a dispatch would do, without doing it: the same
  planning function ``dispatch`` starts with, and nothing after it.
- ``dispatch(ctx)`` — *prepare* (drift check, readiness probe — either may
  ``ctx.refuse``), then ``ctx.declare`` the shard set exactly once, execute,
  stream ``ctx.live`` status (``RUNNING`` when a check starts, its verdict
  when it ends), record an artifact, ``ctx.deliver`` the shard.
- ``cancel(ctx)`` — not overridden: every thread looks at ``ctx.cancelled``
  before it starts a check, so the ``RunnerBase`` default (nothing more to
  stop) is the truth. A request already in flight finishes within
  ``timeout_s``.

Its settings shared with the rest of the package — the environments and a
user agent — come from ``connectors.shop-checks`` in the config file
(``CONNECTOR`` below); its own options from its ``runners:`` entry. What the
person starting a run chose is in ``ctx.parameters`` — never in
``ctx.options``, which holds the runner's config entry and nothing else.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from runcomposer.sdk import (
    ConfigError,
    DispatchContext,
    Health,
    Item,
    Parameter,
    PlanContext,
    PlanPreview,
    RunnerBase,
    RunnerInfo,
    Verdict,
)

__all__ = ["HttpCheckRunner"]


class HttpCheckRunner(RunnerBase):
    #: Build this runner with ``connectors.shop-checks`` as ``connector_settings``.
    CONNECTOR = "shop-checks"

    def __init__(
        self,
        environment: str = "local",
        workers: int = 4,
        timeout_s: float = 10.0,
        connector_settings: dict[str, Any] | None = None,
    ):
        # Construction is side-effect free (runcomposer builds every plugin
        # once at startup to validate the config): check, store, nothing else.
        settings = dict(connector_settings or {})
        environments = settings.get("environments")
        if not isinstance(environments, dict) or not environments:
            raise ConfigError(
                "http-checks: connectors.shop-checks.environments must map environment names to "
                "base URLs"
            )
        for name, url in environments.items():
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                raise ConfigError(f"http-checks: environment {name!r} needs an http(s) base URL")
        if environment not in environments:
            raise ConfigError(
                f"http-checks: environment {environment!r} is not one of {sorted(environments)}"
            )
        if int(workers) < 1:
            raise ConfigError("http-checks: workers must be at least 1")
        self.environments = {str(name): str(url).rstrip("/") for name, url in environments.items()}
        self.environment = environment
        self.workers = int(workers)
        self.timeout_s = float(timeout_s)
        self.user_agent = str(settings.get("user_agent") or "runcomposer-shop-connector")

    # -- what it says about itself -----------------------------------------------------

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="http-checks",
            title="Shop HTTP checks",
            description="Requests each planned check's URL and compares status and body.",
            capabilities=("live_status", "cancel", "plan"),
            parameters=(
                Parameter(
                    "environment",
                    kind="choice",
                    label="Environment",
                    help="Which shop to check.",
                    choices=tuple(self.environments),
                    default=self.environment,
                ),
            ),
        )

    def health(self) -> Health:
        base = self.environments[self.environment]
        try:
            status, _body = self._request("GET", base + "/")
        except OSError as exc:
            return Health(status="down", detail=f"{base} is not reachable: {exc}")
        return Health(
            status="ok" if status < 500 else "degraded",
            detail=f"{base} answered {status}",
            data={"environment": self.environment},
        )

    # -- planning: shared by the preview and the dispatch ---------------------------------

    def _prepare(self, ctx: PlanContext) -> tuple[str, str, dict[str, Item]]:
        """Everything decided before anything runs — ``(environment, base URL,
        the planned items)``. It reads and refuses, and touches nothing, so
        ``plan`` can call it as safely as ``dispatch`` does."""
        environment = str(ctx.parameters.get("environment") or self.environment)
        base = self.environments[environment]
        planned_snapshot = (ctx.spec.get("source") or {}).get("snapshot")
        if planned_snapshot and planned_snapshot != ctx.source.snapshot():
            ctx.refuse(
                "the checks file changed since this run was composed (drift) — compose it again"
            )
        catalog = {item.id: item for item in ctx.source.items()}
        unknown = [item_id for item_id in ctx.item_ids if item_id not in catalog]
        if unknown:
            ctx.refuse(f"{len(unknown)} planned check(s) are not in the catalog: {unknown[:3]}")
        return environment, base, catalog

    def _plan_text(self, ctx: PlanContext, base: str) -> str:
        return f"{len(ctx.item_ids)} HTTP check(s) against {base}, {self.workers} at a time"

    def plan(self, ctx: PlanContext) -> PlanPreview:
        environment, base, _catalog = self._prepare(ctx)
        return PlanPreview(
            text=self._plan_text(ctx, base),
            shards={environment: list(ctx.item_ids)},
            notes=("the shop is probed when the run is dispatched — a preview does not contact it",),
            data={"base_url": base, "workers": self.workers},
        )

    # -- one dispatch ---------------------------------------------------------------------

    def dispatch(self, ctx: DispatchContext) -> None:
        # 1. Prepare. Everything up to declare() may refuse; a refusal leaves
        #    no dispatch behind and its reason is the answer the person sees.
        environment, base, catalog = self._prepare(ctx)
        try:
            self._request("GET", base + "/")
        except OSError as exc:
            ctx.refuse(f"the {environment} shop at {base} is not reachable: {exc}")

        # 2. The hand-off: one shard, named after the environment, carrying
        #    every planned item.
        ctx.declare(
            {environment: list(ctx.item_ids)},
            plan=self._plan_text(ctx, base),
            links={"shop": base},
        )

        # 3. Execute. From here on an exception is a FAILED dispatch whose
        #    delivered results are kept.
        def run_one(item_id: str) -> Verdict | None:
            if ctx.cancelled:
                return None
            ctx.live(environment, item_id, "RUNNING")
            verdict = self._check(base, catalog[item_id])
            ctx.live(environment, item_id, verdict.status, duration_ms=verdict.duration_ms,
                     message=verdict.message)
            return verdict

        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="http-check") as pool:
            results = list(pool.map(run_one, ctx.item_ids))
        ctx.raise_if_cancelled()  # a cancelled dispatch delivers nothing
        verdicts = [verdict for verdict in results if verdict is not None]

        # 4. Record and deliver.
        report = ctx.workdir / "report.json"
        report.write_text(
            json.dumps(
                {"environment": environment, "base_url": base,
                 "checks": [{"id": v.item_id, "status": v.status, "duration_ms": v.duration_ms,
                             "message": v.message} for v in verdicts]},
                indent=2,
            ),
            encoding="utf-8",
        )
        ctx.add_artifact("report.json", report, media_type="application/json", shard=environment)
        ctx.log(f"{len(verdicts)} check(s) executed against {base}")
        ctx.deliver(environment, verdicts=verdicts)

    # -- the checks themselves ------------------------------------------------------------------

    def _check(self, base: str, item: Item) -> Verdict:
        meta = item.meta
        started = time.monotonic()
        try:
            status, body = self._request(str(meta["method"]), base + str(meta["path"]))
        except OSError as exc:
            return Verdict(item.id, "ERROR", _ms(started), f"request failed: {exc}")
        duration = _ms(started)
        expected = int(meta["expect_status"])
        if status != expected:
            return Verdict(item.id, "FAIL", duration, f"expected HTTP {expected}, got {status}")
        text = str(meta.get("expect_text") or "")
        if text and text not in body:
            return Verdict(item.id, "FAIL", duration, f"the response does not contain {text!r}")
        return Verdict(item.id, "PASS", duration)

    def _request(self, method: str, url: str) -> tuple[int, str]:
        request = urllib.request.Request(
            url, method=method, headers={"User-Agent": self.user_agent},
            data=b"" if method in ("POST", "PUT") else None,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return int(response.status), response.read(65536).decode("utf-8", "replace")
        except urllib.error.HTTPError as error:  # a status code is an answer, not a failure
            return int(error.code), error.read(65536).decode("utf-8", "replace")


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
