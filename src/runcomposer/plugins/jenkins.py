"""`jenkins` Runner (DESIGN.md §6.2b): triggers an existing parameterized
Jenkins job and takes its results back. Built on ``runcomposer.sdk.jenkins``
— the client a connector composes when this runner's switches are not enough.

**What travels to the job** is configured, not fixed: ``parameters`` maps each
build-parameter name of *your* job to a template. Available fields:

- the document: ``{spec_b64}`` (the exact document bytes, gzip + base64 — one
  line of ASCII that survives any form or shell; decode with
  ``base64 -d | gunzip``), ``{spec_url}`` (where the job can download the
  exact bytes; needs ``callback_base``) and ``{spec_json}`` (the document as
  text — for small specs only: a build parameter is one form field and one
  environment variable of the build, both limited in size);
- identity: ``{run_id}``, ``{dispatch_id}``, ``{title}``, ``{labels[key]}``;
- the way back: ``{callback_url}`` and ``{token}`` (the ingest token for this
  dispatch — bind it to a password parameter);
- the work: ``{item_ids}`` (one per line), ``{shards}`` (comma-separated) and
  ``{params[name]}`` (per-run parameters declared under ``run_parameters``);
- your own: every name under ``field_providers`` — a configured callable
  called with the context and the fields above, returning the text of the
  field. It may ``ctx.refuse`` a run its job has no way to carry out. And
  every name listed under ``plan_fields``, which ``plan_provider`` yields.

A configured callable — ``field_providers`` values, ``postprocess``,
``plan_provider`` — is ``"pkg.mod:callable"``, or ``{call: "pkg.mod:callable",
with: {…}}`` to hand it settings: the ``with:`` mapping arrives as the keyword
argument ``settings``, so one function serves several entries.

A trigger whose parameters exceed ``max_parameter_bytes`` (one value) or
``max_request_bytes`` (all of them) is refused before anything is sent.

**Shards.** ``shards`` names what the build will report separately — a list,
or a template rendering to a comma- or newline-separated one
(``"{params[regions]}"``). The job pushes each with ``shard=<name>``, or
leaves each one's files where ``artifact_paths`` / ``result_urls`` point —
``{shard}`` in those is replaced by the shard's name.

**A plan of your own.** ``plan_provider`` is a configured callable that works
out the plan for a job that cannot: called ``(ctx, fields)`` with the
built-in fields (``{shards}`` as the ``shards`` option renders), it returns a
mapping with any of

- ``"shards": {name: [item ids]}`` — the shards, and which planned items each
  will report. They replace what the ``shards`` option says (the provider
  wins when both are configured), are declared with their items, and show in
  the plan preview with them; ``{shards}`` is rendered from their names;
- ``"groups": {shard: label}`` — shards that belong together, declared as
  groups and shown in the preview (for shards the provider returned);
- ``"fields": {name: text}`` — template fields. Their names are listed under
  ``plan_fields`` so the templates can be checked at startup; one the
  provider does not yield renders as empty text.

It is called by the plan preview and by the dispatch alike, so it must not
change anything; it may ``ctx.refuse``. What it returns is checked before
anything is sent — a shape it should not have, or an item that is not
planned, fails the dispatch (or the preview) naming the provider.
``field_providers`` run after it and see its fields.

**Is the job given what is configured?** Jenkins ignores a build parameter
its job does not declare — silently: the build runs, the value never arrives.
``check_parameters: true`` makes the health check compare the names under
``parameters`` with the job's declared parameters (a degraded runner names
the ones the job would drop).

**How results come back** is ``completion``:

- ``callback`` (default): the job POSTs its bundle to ``{callback_url}`` with
  the token. ``dispatch`` returns once the build is queued; the run waits in
  ``AWAITING_RESULTS`` until the delivery lands.
- ``poll``: for a Jenkins that cannot call out. runcomposer follows the queue
  item to the build, waits for it to finish, downloads the archived files
  matching ``artifact_paths`` and the build-relative URLs listed under
  ``result_urls`` (what a publisher serves without archiving it), runs
  ``postprocess`` over them if one is configured, and delivers them.
  ``outage_tolerance_s`` lets the following ride out a Jenkins that is away
  for up to that many seconds (a restart): without it, a request that still
  fails after the client's bounded retries fails the dispatch.

The build is identified through the queue item the trigger returns — never by
"the latest build of the job", which may be somebody else's — and, after
Jenkins has forgotten the queue item, by the queue id the build records.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import string
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runcomposer.sdk import (
    ConfigError,
    ConfiguredCall,
    DispatchCancelled,
    DispatchContext,
    Health,
    PlanContext,
    PlanPreview,
    PluginError,
    RunnerBase,
    RunnerInfo,
    callback_urls,
    child_path,
    name_collision_key,
    parameters_from_config,
)
from runcomposer.sdk.jenkins import (
    JenkinsClient,
    JenkinsError,
    JenkinsQueueCancelled,
    JenkinsTimeout,
    encode_spec,
)

__all__ = ["JenkinsRunner"]

DEFAULT_PARAMETERS = {
    "SPEC_JSON": "{spec_json}",
    "DISPATCH_ID": "{dispatch_id}",
    "CALLBACK_URL": "{callback_url}",
    "INGEST_TOKEN": "{token}",
}
TEMPLATE_FIELDS = (
    "spec_json", "spec_b64", "spec_url", "run_id", "dispatch_id", "callback_url", "token",
    "item_ids", "title", "shards", "labels", "params",
)
_INDEXED = ("labels", "params")
_AVAILABLE = (
    "{spec_b64} {spec_url} {spec_json} {run_id} {dispatch_id} {callback_url} {token} "
    "{item_ids} {title} {shards} {labels[key]} {params[name]}"
)
#: Shown in a plan preview for what only exists once a dispatch does.
_AT_DISPATCH = "‹at dispatch›"


class _Lenient(dict[str, Any]):
    """A missing label or parameter renders as nothing, not as a KeyError."""

    def __missing__(self, key: str) -> Any:
        return ""


def _fields_of(template: str, where: str) -> list[str]:
    try:
        return [field for _, field, _, _ in string.Formatter().parse(template) if field]
    except ValueError as exc:
        raise ConfigError(f"jenkins: {where}: {exc}") from None


def _check_template(where: str, template: Any, extra: tuple[str, ...] = ()) -> str:
    if not isinstance(template, str):
        raise ConfigError(f"jenkins: {where} must be a template string")
    for field in _fields_of(template, where):
        root = field.split("[", 1)[0].split(".", 1)[0]
        known = root in TEMPLATE_FIELDS or root in extra
        if not known or (root in _INDEXED) != ("[" in field):
            more = "".join(f" {{{name}}}" for name in extra)
            raise ConfigError(
                f"jenkins: {where} uses {{{field}}} — available: {_AVAILABLE}{more}"
            )
    return template


def _configured(entry: Any, option: str) -> ConfiguredCall:
    """A configured callable option, its shape checked (the import happens
    at the first call)."""
    if isinstance(entry, str):
        module, sep, attr = entry.partition(":")
        if not sep or not module or not attr:
            raise ConfigError(
                f"jenkins: {option} is an import path 'package.module:callable' (or "
                f"{{call: 'package.module:callable', with: {{…}}}}), got {entry!r}"
            )
    return ConfiguredCall.parse(entry, f"jenkins: {option}")


def _call(call: ConfiguredCall, *args: Any) -> Any:
    """Call a configured callable; one that cannot be imported is a
    configuration error naming its option."""
    try:
        call.load()
    except PluginError as exc:
        raise ConfigError(str(exc)) from None
    return call(*args)


def _field_name(name: str, option: str) -> str:
    if not name.isidentifier() or name in TEMPLATE_FIELDS or name == "shard":
        raise ConfigError(
            f"jenkins: {option}: a field name is an identifier that is "
            f"not one of the built-in fields {list(TEMPLATE_FIELDS)}"
        )
    return name


@dataclass
class _Plan:
    """What ``plan_provider`` returned, checked."""

    shards: dict[str, list[str]] | None = None
    groups: dict[str, str] = dataclasses.field(default_factory=dict)
    fields: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclass
class _Rendered:
    """A run as it would be triggered."""

    shards: list[str]
    #: The items of each shard, when ``plan_provider`` said which.
    shard_items: dict[str, list[str]] | None
    groups: dict[str, str]
    build_parameters: dict[str, str]


class JenkinsRunner(RunnerBase):
    PATH_OPTIONS = ("ca_bundle",)

    def __init__(
        self,
        base_url: str,
        job: str,
        parameters: Mapping[str, str] | None = None,
        callback_base: str | None = None,
        completion: str = "callback",
        shards: list[str] | str | None = None,
        artifact_paths: list[str] | None = None,
        result_urls: list[str] | None = None,
        field_providers: Mapping[str, Any] | None = None,
        postprocess: str | Mapping[str, Any] | None = None,
        plan_provider: str | Mapping[str, Any] | None = None,
        plan_fields: list[str] | None = None,
        check_parameters: bool = False,
        outage_tolerance_s: float | None = None,
        format: str | None = None,
        user: str | None = None,
        api_token: str | None = None,
        verify_tls: bool = True,
        ca_bundle: str | None = None,
        request_timeout_s: float = 30.0,
        retries: int = 3,
        retry_backoff_s: float = 1.0,
        poll_interval_s: float = 5.0,
        timeout_s: float = 3600.0,
        run_parameters: list[Mapping[str, Any]] | None = None,
        max_parameter_bytes: int | None = 128_000,
        max_request_bytes: int | None = 1_000_000,
    ):
        if completion not in ("callback", "poll"):
            raise ConfigError(f"jenkins: completion must be 'callback' or 'poll', got {completion!r}")
        if not job or not isinstance(job, str):
            raise ConfigError("jenkins: 'job' is required")
        try:
            self.client = JenkinsClient(
                str(base_url),
                user=user,
                api_token=api_token,
                verify_tls=verify_tls,
                ca_bundle=ca_bundle,
                request_timeout_s=request_timeout_s,
                retries=retries,
                retry_backoff_s=retry_backoff_s,
            )
        except ValueError as exc:
            raise ConfigError(f"jenkins: {exc}") from None
        self.base_url = self.client.base_url
        self.job = job
        self.job_url = self.client.job_url(job)
        if field_providers is not None and not isinstance(field_providers, Mapping):
            raise ConfigError("jenkins: field_providers maps field names to configured callables")
        self.field_providers: dict[str, ConfiguredCall] = {
            _field_name(str(name), f"field_providers.{name}"):
                _configured(ref, f"field_providers.{name}")
            for name, ref in (field_providers or {}).items()
        }
        self.plan_provider = (
            _configured(plan_provider, "plan_provider") if plan_provider else None
        )
        if plan_fields is not None and not isinstance(plan_fields, (list, tuple)):
            raise ConfigError("jenkins: plan_fields is a list of field names")
        self.plan_fields = [_field_name(str(name), f"plan_fields.{name}") for name in plan_fields or ()]
        if self.plan_fields and self.plan_provider is None:
            raise ConfigError(
                "jenkins: plan_fields names fields that plan_provider yields — configure a "
                "plan_provider, or drop plan_fields"
            )
        both = sorted(set(self.plan_fields) & set(self.field_providers))
        if both:
            raise ConfigError(
                f"jenkins: {both} named under plan_fields and field_providers — a field comes "
                "from one of them"
            )
        provided = (*self.field_providers, *self.plan_fields)
        self.parameters = {
            str(name): _check_template(f"parameters.{name}", template, provided)
            for name, template in (parameters or DEFAULT_PARAMETERS).items()
        }
        self.callback_base = (callback_base or "").rstrip("/") or None
        self.completion = completion
        if shards is None:
            self.shards: list[str] | str = ["1"]
        elif isinstance(shards, str):
            self.shards = _check_template("shards", shards)
            if "shards" in _fields_of(shards, "shards"):
                raise ConfigError("jenkins: shards cannot be computed from {shards}")
        elif isinstance(shards, (list, tuple)) and shards and all(str(s).strip() for s in shards):
            self.shards = [str(shard).strip() for shard in shards]
            if len(set(self.shards)) != len(self.shards):
                raise ConfigError(f"jenkins: shards must be distinct, got {self.shards}")
        else:
            raise ConfigError(
                "jenkins: shards is a non-empty list of names, or a template rendering to a "
                "comma-separated list (\"{params[regions]}\")"
            )
        self.artifact_paths = [
            str(p) for p in (artifact_paths if artifact_paths is not None else
                             ([] if result_urls else ["results/*"]))
        ]
        self.result_urls = [str(p).lstrip("/") for p in (result_urls or [])]
        for url in self.result_urls:
            if "://" in url or ".." in url.split("/"):
                raise ConfigError(
                    f"jenkins: result_urls entries are paths below the build's URL, got {url!r}"
                )
        self.postprocess = _configured(postprocess, "postprocess") if postprocess else None
        self.check_parameters = bool(check_parameters)
        if outage_tolerance_s is not None and (
            isinstance(outage_tolerance_s, bool)
            or not isinstance(outage_tolerance_s, (int, float))
            or outage_tolerance_s < 0
        ):
            raise ConfigError(
                "jenkins: outage_tolerance_s is a number of seconds (0 or null: no tolerance)"
            )
        self.outage_tolerance_s = float(outage_tolerance_s) if outage_tolerance_s else None
        self.format = format
        self.user = user
        self.poll_interval_s = float(poll_interval_s)
        self.timeout_s = float(timeout_s)
        self.run_parameters = parameters_from_config(run_parameters, "jenkins: run_parameters")
        for name, limit in (("max_parameter_bytes", max_parameter_bytes),
                            ("max_request_bytes", max_request_bytes)):
            if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
                raise ConfigError(f"jenkins: {name} must be a positive integer or null (no limit)")
        self.max_parameter_bytes = max_parameter_bytes
        self.max_request_bytes = max_request_bytes

    # -- self-description -----------------------------------------------------------

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="jenkins",
            title="Jenkins",
            description=f"Triggers the Jenkins job {self.job!r}; results return by "
            + ("callback." if self.completion == "callback" else "polling the build."),
            capabilities=("external_delivery", "cancel", "plan", self.completion),
            parameters=self.run_parameters,
            result_format=self.format,
        )

    def health(self) -> Health:
        data: dict[str, Any] = {"job_url": self.job_url, "completion": self.completion}
        try:
            data["version"] = self.client.version()
        except Exception as exc:
            return Health(status="down", detail=f"Jenkins unreachable: {exc}", data=data)
        try:
            readable = self.client.job_exists(self.job)
            problem = None if readable else "404 Not Found"
        except Exception as exc:
            readable, problem = False, str(exc)
        checks: list[dict[str, Any]] = [
            {"check": "server reachable", "ok": True, "detail": f"Jenkins {data['version']}".strip()},
            {"check": f"job {self.job!r} readable", "ok": readable, "detail": problem or self.job_url},
        ]
        data["checks"] = checks
        if self.check_parameters:
            checks.append(self._parameter_check(readable))
        if not readable:
            return Health(
                status="degraded", detail=f"job {self.job!r} not readable: {problem}", data=data
            )
        if not checks[-1]["ok"]:
            return Health(status="degraded", detail=f"job {self.job!r}: {checks[-1]['detail']}", data=data)
        return Health(status="ok", detail=f"Jenkins {data['version']}".strip(), data=data)

    def _parameter_check(self, readable: bool) -> dict[str, Any]:
        """Does the job declare every build parameter configured under
        ``parameters``? Jenkins drops the others without a word."""
        row: dict[str, Any] = {"check": "job parameters", "ok": False}
        if not readable:
            return {**row, "detail": "not checked — the job is not readable"}
        try:
            declared = self.client.job_parameters(self.job)
        except Exception as exc:
            return {**row, "detail": f"the job's parameters could not be read: {exc}"}
        configured = list(self.parameters)
        if declared is None:
            return {
                **row,
                "detail": "the job declares no parameters — every configured one would be "
                f"ignored ({', '.join(configured)}); make it a parameterized job",
            }
        unknown = [name for name in configured if name not in declared]
        if unknown:
            return {
                **row,
                "detail": f"the job does not declare {', '.join(unknown)} — Jenkins would "
                "ignore them silently; add them to the job, or rename them under parameters "
                f"(the job declares: {', '.join(declared)})",
            }
        return {
            **row, "ok": True,
            "detail": f"the job declares all {len(configured)} configured parameter(s)",
        }

    # -- what would be sent ---------------------------------------------------------------

    def _callback_url(self, ctx: PlanContext) -> str:
        if self.completion != "callback":
            return ""
        if not self.callback_base:
            ctx.refuse(
                "jenkins completion=callback needs the callback_base option (the URL under "
                "which the job can reach this runcomposer's API)"
            )
        return callback_urls(self.callback_base, ctx.run_id).results

    def _uses(self, field: str) -> bool:
        """Does any template (a parameter, the shard list) use ``field``?"""
        templates = [*self.parameters.values()]
        if isinstance(self.shards, str):
            templates.append(self.shards)
        return any(field in _fields_of(template, "template") for template in templates)

    def _render(self, ctx: PlanContext, *, dispatch_id: str, token: str) -> _Rendered:
        """The shards, their items and groups, and the build parameters for
        this run. Everything a trigger can be refused for without talking to
        Jenkins is refused here."""
        run = ctx.spec.get("run") or {}
        spec_url = ""
        if self._uses("spec_url"):
            if not self.callback_base:
                ctx.refuse(
                    "jenkins: a parameter uses {spec_url}, which needs the callback_base option "
                    "(the URL under which the job can reach this runcomposer's API)"
                )
            spec_url = str(callback_urls(self.callback_base, ctx.run_id, dispatch_id).spec)
        document = ctx.export_spec() if self._uses_document() else b""
        fields = _Lenient(
            spec_json=document.decode("utf-8") if self._uses("spec_json") else "",
            spec_b64=encode_spec(document) if self._uses("spec_b64") else "",
            spec_url=spec_url,
            run_id=ctx.run_id,
            dispatch_id=dispatch_id,
            callback_url=self._callback_url(ctx),
            token=token,
            item_ids="\n".join(ctx.item_ids),
            title=str(run.get("title") or ""),
            labels=_Lenient({str(k): str(v) for k, v in (run.get("labels") or {}).items()}),
            params=_Lenient({str(k): _text(v) for k, v in ctx.parameters.items()}),
        )
        formatter = string.Formatter()
        if isinstance(self.shards, str):
            rendered = formatter.vformat(self.shards, (), fields)
            shards = [part.strip() for part in rendered.replace("\n", ",").split(",") if part.strip()]
        else:
            shards = list(self.shards)
        fields["shards"] = ",".join(shards)
        planned = self._ask_plan_provider(ctx, dict(fields)) if self.plan_provider else _Plan()
        if planned.shards is not None:
            shards = list(planned.shards)
        elif isinstance(self.shards, str):
            if not shards:
                ctx.refuse(f"jenkins: shards ({self.shards!r}) rendered to nothing for this run")
            if len(set(shards)) != len(shards):
                ctx.refuse(f"jenkins: shards ({self.shards!r}) rendered to duplicates: {shards}")
        fields["shards"] = ",".join(shards)
        for name in self.plan_fields:
            fields[name] = ""
        fields.update(planned.fields)
        for name, call in self.field_providers.items():
            value = _call(call, ctx, dict(fields))
            if not isinstance(value, str):
                raise TypeError(
                    f"jenkins: field provider {call.ref} returned {type(value).__name__}, not text"
                )
            fields[name] = value
        build_parameters = {
            name: formatter.vformat(template, (), fields)
            for name, template in self.parameters.items()
        }
        self._check_sizes(ctx, build_parameters)
        if self.completion == "poll" and len(shards) > 1 and not self.postprocess and not any(
            "{shard}" in path for path in (*self.artifact_paths, *self.result_urls)
        ):
            ctx.refuse(
                f"jenkins: {len(shards)} shards with completion: poll need a way to tell their "
                "results apart — put {shard} into artifact_paths / result_urls, or configure a "
                "postprocess that splits what was downloaded"
            )
        return _Rendered(
            shards=shards, shard_items=planned.shards, groups=planned.groups,
            build_parameters=build_parameters,
        )

    def _ask_plan_provider(self, ctx: PlanContext, fields: dict[str, Any]) -> _Plan:
        """Call ``plan_provider`` and check what it returned: a mistake there
        is caught before anything is sent, naming the provider."""
        assert self.plan_provider is not None
        ref = self.plan_provider.ref
        answer = _call(self.plan_provider, ctx, fields)

        def wrong(what: str) -> RuntimeError:
            return RuntimeError(f"jenkins: plan_provider {ref} returned {what}")

        if answer is None:
            return _Plan()
        if not isinstance(answer, Mapping):
            raise wrong(f"{type(answer).__name__}, not a mapping of shards / groups / fields")
        unknown = sorted(str(key) for key in answer if key not in ("shards", "groups", "fields"))
        if unknown:
            raise wrong(f"the key(s) {unknown} — it may return 'shards', 'groups' and 'fields'")
        plan = _Plan()
        if answer.get("shards") is not None:
            given = answer["shards"]
            if not isinstance(given, Mapping) or not given:
                raise wrong("shards that are not a non-empty mapping of shard name → item ids")
            planned = set(ctx.item_ids)
            plan.shards = {}
            for name, items in given.items():
                if not isinstance(name, str) or not name.strip() or name != name.strip() or any(
                    sep in name for sep in (",", "\n")
                ):
                    raise wrong(
                        f"the shard name {name!r} — a shard name is non-empty text without commas, "
                        "line breaks or surrounding blanks"
                    )
                if isinstance(items, (str, bytes)) or not isinstance(items, (list, tuple)) or not all(
                    isinstance(item, str) for item in items
                ):
                    raise wrong(f"{items!r} for shard {name!r}, not a list of item ids")
                strangers = [item for item in items if item not in planned]
                if strangers:
                    raise wrong(f"item(s) {strangers} for shard {name!r} that this run does not plan")
                plan.shards[name] = list(items)
        if answer.get("groups"):
            groups = answer["groups"]
            if not isinstance(groups, Mapping):
                raise wrong("groups that are not a mapping of shard name → label")
            if plan.shards is None:
                raise wrong("groups without shards — groups label shards the provider returns")
            strays = sorted(str(shard) for shard in groups if shard not in plan.shards)
            if strays:
                raise wrong(f"groups for shard(s) {strays} it did not return")
            if not all(isinstance(label, str) for label in groups.values()):
                raise wrong("a group label that is not text")
            plan.groups = {str(shard): label for shard, label in groups.items() if label}
        if answer.get("fields"):
            given_fields = answer["fields"]
            if not isinstance(given_fields, Mapping):
                raise wrong("fields that are not a mapping of field name → text")
            for name, value in given_fields.items():
                if not isinstance(name, str) or not name.isidentifier() or name in TEMPLATE_FIELDS \
                        or name == "shard" or name in self.field_providers:
                    raise wrong(
                        f"the field {name!r} — a field it yields is an identifier that is neither a "
                        "built-in field nor one of field_providers"
                    )
                if not isinstance(value, str):
                    raise wrong(f"{type(value).__name__} for the field {name!r}, not text")
                plan.fields[name] = value
        return plan

    def _uses_document(self) -> bool:
        return self._uses("spec_json") or self._uses("spec_b64") or self._uses("spec_url")

    def _check_sizes(self, ctx: PlanContext, build_parameters: Mapping[str, str]) -> None:
        hint = (
            " — pass the document as {spec_b64} (compressed) or {spec_url} (downloaded by the "
            "job) instead of {spec_json}, or raise the limit if your server and agents take more"
        )
        if self.max_parameter_bytes is not None:
            for name, value in build_parameters.items():
                size = len(value.encode("utf-8"))
                if size > self.max_parameter_bytes:
                    ctx.refuse(
                        f"jenkins: build parameter {name} would be {size} bytes, more than "
                        f"max_parameter_bytes ({self.max_parameter_bytes}): Jenkins hands every build "
                        "parameter to the build as an environment variable too" + hint
                    )
        if self.max_request_bytes is not None:
            total = len(urllib.parse.urlencode(dict(build_parameters)).encode("utf-8"))
            if total > self.max_request_bytes:
                ctx.refuse(
                    f"jenkins: the trigger request would be {total} bytes, more than "
                    f"max_request_bytes ({self.max_request_bytes})" + hint
                )

    def plan(self, ctx: PlanContext) -> PlanPreview:
        rendered = self._render(ctx, dispatch_id=_AT_DISPATCH, token=_AT_DISPATCH)
        shards, build_parameters = rendered.shards, rendered.build_parameters
        shown = {
            name: value if len(value) <= 200 else f"‹{len(value.encode('utf-8'))} bytes›"
            for name, value in build_parameters.items()
        }
        lines = [self._plan_text(shards)]
        lines.extend(f"  {name} = {value}" for name, value in shown.items())
        items = rendered.shard_items or {}
        notes = [] if rendered.shard_items is not None else [
            "the job distributes the items over its shards itself — a preview cannot say "
            "which item goes where"
        ]
        notes.append(
            "nothing was sent to Jenkins; the token and the dispatch id exist only once "
            "the run is dispatched"
        )
        return PlanPreview(
            text="\n".join(lines),
            shards={shard: list(items.get(shard, [])) for shard in shards},
            groups=dict(rendered.groups),
            notes=tuple(notes),
            data={
                "job_url": self.job_url,
                "completion": self.completion,
                "build_parameters": shown,
                "request_bytes": len(urllib.parse.urlencode(build_parameters).encode("utf-8")),
            },
        )

    def _plan_text(self, shards: list[str], *, triggered: bool = False) -> str:
        return (
            f"jenkins: job {self.job_url} {'triggered' if triggered else 'would be triggered'}, "
            f"{len(shards)} shard(s) {shards}; completion by "
            + ("callback POST" if self.completion == "callback" else "polling the build")
        )

    # -- dispatch ---------------------------------------------------------------------

    def dispatch(self, ctx: DispatchContext) -> None:
        token = (ctx.ingest_token() or "") if self._uses("token") else ""
        rendered = self._render(ctx, dispatch_id=ctx.dispatch_id, token=token)
        shards, build_parameters = rendered.shards, rendered.build_parameters
        ctx.raise_if_cancelled()
        try:
            queue_url = self.client.trigger(self.job, build_parameters)
        except Exception as exc:
            ctx.refuse(f"Jenkins trigger failed for {self.job_url}: {exc}")
        if ctx.cancelled:
            # Cancelled while the trigger was under way: nothing was handed
            # off, so nobody would stop this build later — take it back now.
            if queue_url:
                try:
                    ctx.log("jenkins: cancelled during the trigger — "
                            + self.client.cancel(self.job, queue_url))
                except Exception as exc:
                    ctx.log(f"jenkins: the triggered build could not be taken back: {exc}",
                            level="warning")
            raise DispatchCancelled("cancelled while the Jenkins build was being triggered")
        # The trigger was accepted: that is the hand-off.
        links = {"job": self.job_url}
        if queue_url:
            links["queue"] = queue_url
        ctx.declare(
            rendered.shard_items if rendered.shard_items is not None else shards,
            plan=self._plan_text(shards, triggered=True), links=links,
            groups=rendered.groups or None,
        )
        if self.completion == "poll":
            self._poll_and_deliver(ctx, queue_url, shards)

    # -- polling --------------------------------------------------------------------------

    def _poll_and_deliver(
        self, ctx: DispatchContext, queue_url: str | None, shards: list[str]
    ) -> None:
        if not queue_url:
            raise RuntimeError(
                "Jenkins accepted the trigger but returned no queue location, so the build "
                "cannot be identified — use completion: callback with this server"
            )
        def started(url: str) -> None:
            # Known from here on: a cancel — also one made after a restart,
            # with a context rebuilt from the record — goes straight to it.
            ctx.set_link("build", url)
            ctx.add_artifact("Jenkins build", url, media_type="text/html")
            ctx.log(f"jenkins: build started — {url}")

        def outage(answering: bool, seconds: float) -> None:
            # One line at each change, so the log says that Jenkins was away.
            if answering:
                ctx.log(f"jenkins: the server answers again after {seconds:.0f}s without an answer")
            else:
                ctx.log(
                    f"jenkins: the server does not answer — waiting up to "
                    f"outage_tolerance_s={self.outage_tolerance_s:g}s for it",
                    level="warning",
                )

        try:
            build = self.client.follow(
                self.job, queue_url, timeout_s=self.timeout_s,
                poll_interval_s=self.poll_interval_s, check=ctx.raise_if_cancelled,
                on_build=started, outage_tolerance_s=self.outage_tolerance_s, on_outage=outage,
            )
            build_url = str(build["url"])
        except JenkinsQueueCancelled:
            raise RuntimeError(
                f"the queued Jenkins build was cancelled before it started: {queue_url}"
            ) from None
        except JenkinsTimeout as exc:
            raise RuntimeError(
                f"the Jenkins build did not {exc.what} within timeout_s={self.timeout_s:g}"
            ) from None
        except JenkinsError as exc:
            raise RuntimeError(str(exc)) from None
        if build.get("result") == "ABORTED":
            raise RuntimeError(f"the Jenkins build was aborted: {build_url}")
        archived = [str(artifact["relativePath"]) for artifact in build.get("artifacts") or []]
        # A job may push results itself while it is polled: what has
        # delivered needs no download.
        outstanding = [shard for shard in shards if shard in set(ctx.outstanding_shards())]
        problems: list[str] = []
        for shard in outstanding:
            bundle = ctx.workdir / "results" if len(shards) == 1 else ctx.workdir / "results" / shard
            try:
                fetched = self._download(ctx, build_url, archived, shard, bundle)
            except _BundleNameError as exc:
                problems.append(f"shard {shard}: {exc}" if len(shards) > 1 else str(exc))
                continue
            if not fetched:
                problems.append(
                    f"shard {shard}: the build ({build.get('result')}) left nothing matching "
                    f"{self._wanted(shard)}: {build_url}/"
                    if len(shards) > 1
                    else f"the build ({build.get('result')}) archived nothing matching "
                    f"{self._wanted(shard)}: {build_url}/artifact/"
                )
                continue
            delivered: Path = bundle
            if self.postprocess:
                changed = _call(self.postprocess, ctx, shard, bundle)
                if changed is not None:
                    delivered = Path(changed)
            for file in fetched:
                if file.is_file():
                    ctx.add_artifact(
                        file.name if len(shards) == 1 else f"{file.name} ({shard})", file,
                        shard=shard if len(shards) > 1 else None,
                    )
            ctx.deliver(shard, bundle=delivered, format=self.format)
        if problems:
            raise RuntimeError("; ".join(problems))

    def _wanted(self, shard: str) -> list[str]:
        return [
            path.replace("{shard}", shard) for path in (*self.artifact_paths, *self.result_urls)
        ]

    def _download(
        self, ctx: DispatchContext, build_url: str, archived: list[str], shard: str, bundle: Path
    ) -> list[Path]:
        """Everything of one shard: archived artifacts matching
        ``artifact_paths``, and the ``result_urls`` below the build."""
        patterns = [path.replace("{shard}", shard) for path in self.artifact_paths]
        matching = [
            relative for relative in archived
            if any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns)
        ]
        wanted = [relative.replace("{shard}", shard) for relative in self.result_urls]
        targets = _bundle_targets(
            bundle,
            [(relative, relative) for relative in matching]
            + [(relative, _result_path(relative)) for relative in wanted],
        )
        fetched: list[Path] = []
        try:
            for relative in matching:
                fetched.append(self.client.download_artifact(build_url, relative, targets[relative]))
            for relative in wanted:
                try:
                    fetched.append(self.client.download_build_file(build_url, relative, targets[relative]))
                except JenkinsError as exc:
                    if exc.status != 404:
                        raise
                    targets[relative].unlink(missing_ok=True)  # the place held for it
                    ctx.log(f"jenkins: {build_url}/{relative} does not exist", level="warning")
        except BaseException:
            # A download failed: the places held for what did not arrive —
            # empty, or half written — are given back.
            for target in targets.values():
                if target not in fetched:
                    target.unlink(missing_ok=True)
            raise
        return fetched

    # -- cancel -----------------------------------------------------------------------------

    def cancel(self, ctx: DispatchContext) -> None:
        """Stop the build behind this dispatch: the build itself when its URL
        is known, else whatever the queue item is by now — a running build
        (also when Jenkins has forgotten the queue item) or a queued one."""
        build_url, queue_url = ctx.links.get("build"), ctx.links.get("queue")
        if build_url:
            self.client.stop_build(build_url)
            ctx.log(f"jenkins: stop requested for {build_url}")
            return
        if not queue_url:
            ctx.log("jenkins: no queue location recorded — the build cannot be stopped from here",
                    level="warning")
            return
        ctx.log("jenkins: " + self.client.cancel(self.job, queue_url))


class _BundleNameError(Exception):
    """Files of a build that cannot be kept in one flat bundle as named."""


def _result_path(relative: str) -> str:
    """The path a ``result_urls`` entry is kept under: its URL path without
    a query, and without a closing ``/`` — ``robot/`` is kept as ``robot``."""
    return urllib.parse.urlsplit(relative).path.rstrip("/") or "result"


def _bundle_targets(bundle: Path, files: list[tuple[str, str]]) -> dict[str, Path]:
    """``{build path: file in the bundle}`` for ``(build path, path whose last
    segment names the file)`` pairs, each place created empty and
    exclusively before anything is downloaded. The bundle is flat: a file is
    kept under the last ``/`` segment of its path. ``_BundleNameError``,
    rather than a file written somewhere else or one file replacing
    another, when a path holds a backslash (on Windows a separator: a build
    that archives such a name is not trusted to name a file), when this
    machine cannot keep the name, or when two files are one file on this
    machine's file system (``eu/junit.xml`` and ``us/junit.xml``
    everywhere; ``Junit.xml`` and ``junit.xml`` on Windows and macOS)."""
    bundle.mkdir(parents=True, exist_ok=True)
    targets: dict[str, Path] = {}
    try:
        for relative, path in dict(files).items():
            if "\\" in path:
                raise _BundleNameError(
                    f"the build's file {relative!r} holds a backslash, a path separator on Windows — "
                    "not a file name this runner keeps"
                )
            name = path.rsplit("/", 1)[-1]
            try:
                target = child_path(bundle, name)
            except ValueError as exc:
                raise _BundleNameError(f"the build's file {relative!r} cannot be kept in the bundle: {exc}") from None
            try:
                target.touch(exist_ok=False)
            except FileExistsError:
                key = name_collision_key(name)
                earlier = [kept for kept, place in targets.items() if name_collision_key(place.name) == key]
                raise _BundleNameError(
                    f"the build's files {earlier[0] if earlier else 'before'!r} and {relative!r} would be "
                    f"one file {name!r} in the bundle, which is flat — narrow artifact_paths or "
                    "result_urls to one of them"
                ) from None
            targets[relative] = target
    except _BundleNameError:
        for target in targets.values():
            target.unlink(missing_ok=True)
        raise
    return targets


def _text(value: Any) -> str:
    """A per-run parameter as the text of a template field: a list joined by
    commas, a mapping as ``name=value`` lines."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(entry) for entry in value)
    if isinstance(value, Mapping):
        return "\n".join(f"{key}={entry}" for key, entry in value.items())
    return str(value)
