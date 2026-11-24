"""Running and reading runs: plan · run · dispatch · cancel · rerun · watch ·
show · runs · finalize · delete · export · export-spec."""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING, Any

from .common import (
    EXIT_NO,
    EXIT_NO_RESULT,
    EXIT_OK,
    EXIT_USAGE,
    add_config_arg,
    add_json_arg,
    add_param_args,
    add_selection_args,
    add_wait_args,
    counts_text,
    emit_json,
    parse_labels,
    parse_params,
    run_exit_code,
    selection_data,
    service_for,
)

if TYPE_CHECKING:
    from runcomposer.app.dispatching import Outcome
    from runcomposer.app.service import Service
    from runcomposer.core.model import DispatchRecord, RunRecord


def register(sub: Any) -> dict[str, Any]:
    p_run = sub.add_parser(
        "run",
        help="compose a selection, dispatch it to a runner, wait, and summarise "
        "(exit 0 only if the run is PASS)",
    )
    add_selection_args(p_run)
    p_run.add_argument("--runner", required=True, help="configured runner id")
    add_param_args(p_run)
    p_run.add_argument("--title", default="Untitled run", help="run title")
    p_run.add_argument("--label", action="append", default=[], metavar="KEY=VALUE",
                       help="free-form provenance label (repeatable)")
    add_wait_args(p_run)
    add_json_arg(p_run)
    add_config_arg(p_run)

    p_plan = sub.add_parser(
        "plan",
        help="show the plan a dispatch would follow — every item with the shard it would "
        "land in — and do nothing else: no execution, nothing stored, nothing triggered",
    )
    add_selection_args(p_plan)
    p_plan.add_argument("--run", dest="run_id", metavar="RUN_ID",
                        help="plan this stored run instead of a selection")
    p_plan.add_argument("--runner", required=True, help="configured runner id")
    add_param_args(p_plan)
    p_plan.add_argument("--items", action="store_true",
                        help="list every item under its shard (default: counts only)")
    add_json_arg(p_plan)
    add_config_arg(p_plan)

    p_dispatch = sub.add_parser(
        "dispatch", help="dispatch a runspec file, or a stored run, to a configured runner"
    )
    p_dispatch.add_argument("spec", nargs="?", help="path to a runspec file (YAML or JSON)")
    p_dispatch.add_argument("--run", dest="run_id", metavar="RUN_ID",
                            help="dispatch this stored run instead of a spec file")
    p_dispatch.add_argument("--runner", required=True, help="configured runner id")
    add_param_args(p_dispatch)
    add_wait_args(p_dispatch)
    add_json_arg(p_dispatch)
    add_config_arg(p_dispatch)

    p_rerun = sub.add_parser("rerun", help="run a stored run again — all of it, or what failed")
    p_rerun.add_argument("run_id")
    p_rerun.add_argument("--scope", choices=("all", "failed"), default="all",
                         help="all: a new dispatch of the same run. failed: a new run composed "
                         "from what did not pass (FAIL, ERROR, MISSING)")
    p_rerun.add_argument("--runner", help="runner id (default: the one the run last used)")
    p_rerun.add_argument("--title", help="title of the run a --scope failed rerun composes "
                         "(default: the source run's title)")
    add_param_args(p_rerun)
    add_wait_args(p_rerun)
    add_json_arg(p_rerun)
    add_config_arg(p_rerun)

    p_cancel = sub.add_parser("cancel", help="cancel a run's dispatch in flight")
    p_cancel.add_argument("run_id")
    p_cancel.add_argument("--force", action="store_true",
                          help="close the dispatch as CANCELLED now, even if the process that "
                          "owns it cannot be reached to stop it (a way out, not the normal path)")
    add_json_arg(p_cancel)
    add_config_arg(p_cancel)

    p_watch = sub.add_parser("watch", help="follow a run's events until it is over")
    p_watch.add_argument("run_id")
    p_watch.add_argument("--after", type=int, default=0, metavar="SEQ",
                         help="only events after this sequence number")
    p_watch.add_argument("--no-follow", action="store_true",
                         help="print what is there and return")
    p_watch.add_argument("--timeout", type=float, metavar="SECONDS",
                         help="stop following after this long (exit 3)")
    add_json_arg(p_watch)
    add_config_arg(p_watch)

    p_show = sub.add_parser("show", help="one run in full: dispatches, shards, artifacts")
    p_show.add_argument("run_id")
    add_json_arg(p_show)
    add_config_arg(p_show)

    p_runs = sub.add_parser("runs", help="list stored runs")
    p_runs.add_argument("--failed-in", metavar="SELECTOR",
                        help="print item ids that FAILED in the selected run "
                        "(latest | run:<id> | before:<ISO time>, each optionally scoped "
                        "with '?key=value&…' or with --label) — DESIGN.md §7")
    p_runs.add_argument("--state", help="filter by lifecycle state (e.g. COMPLETE)")
    p_runs.add_argument("--label", action="append", default=[], metavar="KEY=VALUE",
                        help="restrict to runs carrying this label (repeatable; all must "
                        "match). With --failed-in it scopes which runs 'latest' may choose from")
    p_runs.add_argument("--since", help="only runs created at/after this ISO-8601 time")
    p_runs.add_argument("--until", help="only runs created at/before this ISO-8601 time")
    p_runs.add_argument("-q", "--query", help="case-insensitive substring of the title")
    p_runs.add_argument("--limit", type=int, default=20)
    p_runs.add_argument("--offset", type=int, default=0)
    add_json_arg(p_runs)
    add_config_arg(p_runs)

    p_finalize = sub.add_parser(
        "finalize", help="close a run as it stands: outstanding shards are given up, "
        "unreported items count as MISSING"
    )
    p_finalize.add_argument("run_id")
    add_json_arg(p_finalize)
    add_config_arg(p_finalize)

    p_delete = sub.add_parser("delete", help="delete a run, its results and its artifacts")
    p_delete.add_argument("run_id")
    add_config_arg(p_delete)

    p_export = sub.add_parser("export", help="export a run's results as a normalized document")
    p_export.add_argument("run_id", help="run id to export")
    p_export.add_argument("--format", required=True, help="export format (supported: ctrf)")
    p_export.add_argument("-o", "--out", help="write to this file instead of stdout")
    add_config_arg(p_export)

    p_export_spec = sub.add_parser(
        "export-spec",
        help="hand a stored run's document out for execution elsewhere: mints an export "
        "dispatch and its ingest token",
    )
    p_export_spec.add_argument("run_id")
    p_export_spec.add_argument("--format", choices=("yaml", "json"), default="json")
    p_export_spec.add_argument("-o", "--out", help="write the spec to this file instead of stdout")
    p_export_spec.add_argument("--token-file", metavar="FILE",
                               help="write the ingest token to this file instead of printing it")
    add_config_arg(p_export_spec)

    return {
        "plan": cmd_plan,
        "run": cmd_run,
        "dispatch": cmd_dispatch,
        "rerun": cmd_rerun,
        "cancel": cmd_cancel,
        "watch": cmd_watch,
        "show": cmd_show,
        "runs": cmd_runs,
        "finalize": cmd_finalize,
        "delete": cmd_delete,
        "export": cmd_export,
        "export-spec": cmd_export_spec,
    }


# -- dispatching, in the foreground ------------------------------------------------


def _finish(service: Service, run_id: str, outcome: Outcome, args: argparse.Namespace) -> int:
    """After the runner returned: wait for results that arrive later (unless
    told not to), print the summary, and turn the outcome into an exit code."""
    from runcomposer.app.views import run_detail

    if outcome.refusal is not None:
        if args.as_json:
            emit_json({"run_id": run_id, "refused": outcome.refusal})
        print(f"refused: {outcome.refusal}", file=sys.stderr)
        return EXIT_NO_RESULT
    # What was recorded as a warning on this dispatch is said here: it is the
    # command's own output, not something to go looking for in the events.
    for event in service.store.events(run_id):
        if event.kind == "warning" and event.dispatch_id == outcome.dispatch_id:
            print(f"warning: {event.data.get('message', '')}", file=sys.stderr)
    run = service.require_run(run_id)
    if run.state == "AWAITING_RESULTS" and not args.no_wait:
        print(
            f"run {run_id}: handed off, waiting for results "
            "(they arrive by push or file drop; --no-wait returns instead)",
            file=sys.stderr,
        )
        run = service.wait(run_id, timeout=args.timeout, dispatch_id=outcome.dispatch_id)
    detail = run_detail(service, run, dispatch_id=outcome.dispatch_id, server_paths=True)
    dispatch = next(d for d in detail["dispatches"] if d["dispatch_id"] == outcome.dispatch_id)
    if args.as_json:
        emit_json(detail)
    else:
        if dispatch["plan"]:
            print(dispatch["plan"])
        print(
            f"run {run_id}: dispatch {dispatch['dispatch_id']} via '{dispatch['mode']}' "
            f"({dispatch['declared_shards']} shard(s))"
        )
        completion = f" ({dispatch['completion']})" if dispatch["completion"] else ""
        print(f"state: {dispatch['state']}{completion} — {counts_text(dispatch['summary'])}")
        if dispatch["error"]:
            print(f"error: {dispatch['error']}")
        _print_shards(detail)
    if dispatch["state"] == "AWAITING_RESULTS":
        if args.no_wait:
            return EXIT_OK
        print("timed out waiting for results", file=sys.stderr)
        return EXIT_NO_RESULT
    if dispatch["state"] == "COMPLETE":
        return EXIT_OK if dispatch["completion"] == "PASS" else EXIT_NO
    return EXIT_NO_RESULT


def _print_shards(detail: dict[str, Any]) -> None:
    """The per-shard roll-up, plus what belongs to no shard: items nobody
    reported (when the runner declared no per-shard plan) and live status."""
    for group in detail.get("groups") or ():
        status = group["completion"] or (
            f"{group['delivered_shards']}/{group['declared_shards']} shard(s) delivered"
        )
        print(f"  group {group['group']}: {status} — {counts_text(group['summary'])}")
    groups = detail.get("shard_groups") or {}
    for shard in detail["shards"]:
        if shard["shard"]:
            status = shard["completion"] or ("delivered" if shard["delivered"] else "outstanding")
            where = f" [{groups[shard['shard']]}]" if shard["shard"] in groups else ""
            print(f"  shard {shard['shard']}{where}: {status} — {counts_text(shard['summary'])}")
        elif shard["summary"]:
            print(f"  reported by no shard: {counts_text(shard['summary'])}")
    running = sum(counts.get("RUNNING", 0) for counts in (detail.get("live_summary") or {}).values())
    if running and detail["state"] == "RUNNING":
        print(f"  running now: {running} item(s)")
    live = sum(1 for verdict in detail["verdicts"] if verdict["live"])
    # Once the dispatch is over, an item last seen RUNNING is no live row any
    # more — what was seen is its MISSING row's `last_live`; both count.
    seen = {(verdict["shard"], verdict["item_id"]) for verdict in detail["verdicts"] if verdict["live"]}
    seen |= {
        (verdict["last_live"]["shard"], verdict["item_id"])
        for verdict in detail["verdicts"]
        if verdict.get("last_live")
    }
    if live and detail["state"] == "RUNNING":
        print(f"  live: {live} item(s) reported so far by shards still executing")
    elif seen:
        print(f"  last live status of shards that never delivered: {len(seen)} item(s)")


def _owner_gone(service: Service, dispatch: DispatchRecord | None) -> bool:
    """A dispatch a runner was executing (``DISPATCHED``/``RUNNING``) whose
    owning process is gone — by the rule restart recovery applies
    (``owner_alive``: its pid is dead, it is an earlier life of this pid, or
    it ran on another host). Only read: marking it ``STALE`` is recovery's
    job (``serve``, ``doctor``, the dispatching commands), never a reader's,
    so a reader cannot race the process that owns the state."""
    from runcomposer.app.liveness import owner_alive

    return (
        dispatch is not None
        and dispatch.state in ("DISPATCHED", "RUNNING")
        and bool(dispatch.owner)
        and not owner_alive(dispatch.owner or "", service.dispatcher.owner)
    )


def _state_text(state: str, gone: bool) -> str:
    return f"{state} (owner gone)" if gone else state


def _owner_gone_hint(run_id: str, dispatch: DispatchRecord) -> str:
    return (
        f"# {run_id}: the process executing dispatch {dispatch.dispatch_id} "
        f"({dispatch.owner}) is gone — nothing will finish it. `runcomposer doctor` (or "
        f"starting `serve`) marks it STALE; `runcomposer cancel --force {run_id}` closes it"
    )


def _mark_owner_gone(service: Service, run: RunRecord, row: dict[str, Any]) -> list[DispatchRecord]:
    """Add ``owner_gone`` to a run row or detail (and to each of its
    dispatches) — additive fields of the CLI's JSON; returns the flagged
    dispatches."""
    flagged = []
    for record, entry in zip(run.dispatches, row.get("dispatches") or (), strict=False):
        entry["owner_gone"] = gone = _owner_gone(service, record)
        if gone:
            flagged.append(record)
    row["owner_gone"] = _owner_gone(service, run.latest_dispatch)
    return flagged


def cmd_plan(args: argparse.Namespace) -> int:
    """Exit 0 with a plan, 1 when the dispatch would be refused."""
    from runcomposer.app.views import plan_json

    if args.run_id and (args.filter or args.item_ids or args.from_history):
        print("error: give a selection or --run, not both", file=sys.stderr)
        return EXIT_USAGE
    with service_for(args) as service:
        if args.run_id:
            outcome = service.plan(args.runner, run_id=args.run_id, parameters=parse_params(args.param))
        else:
            outcome = service.plan(
                args.runner, selection=selection_data(args), parameters=parse_params(args.param)
            )
    answer = plan_json(outcome)
    if args.as_json:
        emit_json(answer)
        return EXIT_NO if answer["refused"] else EXIT_OK
    for warning in answer["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    target = f"run {answer['run_id']}" if answer["run_id"] else "this selection"
    print(f"plan for {target} on runner {answer['runner']}: {answer['item_count']} item(s); "
          f"parameters {answer['parameters'] or '{}'}")
    if answer["refused"]:
        print(f"would be refused: {answer['refusal']}")
        return EXIT_NO
    plan = answer["plan"]
    for line in plan["text"].splitlines():
        print(f"  {line}")
    for group in plan["groups"]:
        estimate = f", est {group['estimate_ms'] / 1000:.1f}s" if group["estimate_ms"] else ""
        print(f"group {group['group']}: {len(group['shards'])} shard(s), {group['count']} item(s){estimate}")
    for shard in plan["shards"]:
        where = f" [{shard['group']}]" if shard["group"] else ""
        estimate = f", est {shard['estimate_ms'] / 1000:.1f}s" if shard["estimate_ms"] else ""
        count = f"{shard['count']} item(s)" if shard["count"] else "items decided by the executor"
        print(f"shard {shard['shard']}{where}: {count}{estimate}")
        if args.items:
            for item_id in shard["item_ids"]:
                print(f"    {item_id}")
    for note in plan["notes"]:
        print(f"note: {note}")
    print("nothing was executed, stored or triggered")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    with service_for(args) as service:
        service.recover()
        params = parse_params(args.param)
        composed = service.compose_run(
            selection_data(args),
            title=args.title,
            labels=parse_labels(args.label),
            origin="cli",
            runner_id=args.runner,
            parameters=params,
        )
        assert composed.run is not None
        for warning in composed.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        try:
            outcome = service.dispatch(composed.run.id, args.runner, params, foreground=True)
        except Exception:
            service.delete_run(composed.run.id)  # nothing was dispatched: leave no orphan
            raise
        if outcome.refusal is not None:
            service.delete_run(composed.run.id)
            print(f"refused: {outcome.refusal}", file=sys.stderr)
            if args.as_json:
                emit_json({"run_id": None, "refused": outcome.refusal})
            return EXIT_NO_RESULT
        return _finish(service, composed.run.id, outcome, args)


def cmd_dispatch(args: argparse.Namespace) -> int:
    from runcomposer.core.spec import SpecLoadError, load_document

    if bool(args.spec) == bool(args.run_id):
        print("error: give a spec file or --run RUN_ID (exactly one)", file=sys.stderr)
        return EXIT_USAGE
    with service_for(args) as service:
        service.recover()
        if args.spec:
            try:
                doc = load_document(args.spec)
            except (OSError, SpecLoadError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_USAGE
            run_id = service.import_spec(doc, origin="cli")
        else:
            run_id, doc = args.run_id, None
        outcome = service.dispatch(
            run_id, args.runner, parse_params(args.param), foreground=True, document=doc
        )
        return _finish(service, run_id, outcome, args)


def cmd_rerun(args: argparse.Namespace) -> int:
    with service_for(args) as service:
        service.recover()
        target, outcome = service.rerun(
            args.run_id,
            scope=args.scope,
            runner_id=args.runner,
            parameters=parse_params(args.param),
            foreground=True,
            title=args.title,
        )
        if target != args.run_id:
            print(f"rerun of {args.run_id}: composed run {target}", file=sys.stderr)
            if outcome.refusal is not None:
                service.delete_run(target)
        return _finish(service, target, outcome, args)


def cmd_cancel(args: argparse.Namespace) -> int:
    """Works against a run another process (the server) is executing: the
    request is recorded in the store and that process honours it."""
    from runcomposer.app.views import dispatch_json

    with service_for(args) as service:
        dispatch = service.cancel(args.run_id, force=args.force)
        if args.as_json:
            emit_json(dispatch_json(dispatch))
        elif dispatch.state == "CANCELLED":
            print(f"run {args.run_id}: dispatch {dispatch.dispatch_id} CANCELLED")
        else:
            print(
                f"run {args.run_id}: cancellation requested for dispatch {dispatch.dispatch_id} "
                f"({dispatch.state}) — the process executing it will stop it; "
                f"follow with `runcomposer watch {args.run_id}`"
            )
    return EXIT_OK


def _event_line(event: Any) -> str:
    data = event.data
    parts = [f"{event.at}  #{event.seq:<4} {event.kind}"]
    for key in ("state", "completion", "shard", "outcome", "reason", "message", "error"):
        if data.get(key):
            parts.append(f"{key}={data[key]}")
    if data.get("summary"):
        parts.append(counts_text(data["summary"]))
    return "  ".join(parts)


def _has_news(service: Service, run_id: str, cursor: int) -> Any:
    return lambda: bool(service.store.events(run_id, after=cursor, limit=1))


def cmd_watch(args: argparse.Namespace) -> int:
    from runcomposer.app.views import event_json
    from runcomposer.core import lifecycle

    with service_for(args) as service:
        service.require_run(args.run_id)
        cursor = args.after
        import time

        deadline = None if args.timeout is None else time.monotonic() + args.timeout
        warned: set[str] = set()
        while True:
            events = service.store.events(args.run_id, after=cursor)
            for event in events:
                cursor = event.seq
                if args.as_json:
                    sys.stdout.write(json.dumps(event_json(event), ensure_ascii=False) + "\n")
                else:
                    print(_event_line(event))
            sys.stdout.flush()
            run = service.require_run(args.run_id)
            latest = run.latest_dispatch
            if latest is not None and latest.dispatch_id not in warned and _owner_gone(service, latest):
                # Said once, on stderr; following goes on — recovery in a
                # server or `doctor` ends the dispatch, and this watch with it.
                warned.add(latest.dispatch_id)
                print(f"warning: {_owner_gone_hint(run.id, latest)[2:]}", file=sys.stderr)
            over = run.state in lifecycle.TERMINAL_STATES or run.state == "COMPOSED"
            if args.no_follow or (over and not events):
                return EXIT_OK if args.no_follow else run_exit_code(run)
            if deadline is not None and time.monotonic() >= deadline:
                print("timed out", file=sys.stderr)
                return EXIT_NO_RESULT
            if not events:
                service.bus.wait(_has_news(service, args.run_id, cursor), 1.0)


def cmd_show(args: argparse.Namespace) -> int:
    from runcomposer.app.views import run_detail

    with service_for(args) as service:
        run = service.require_run(args.run_id)
        detail = run_detail(service, run, server_paths=True)
        flagged = _mark_owner_gone(service, run, detail)
    if args.as_json:
        emit_json(detail)
        return EXIT_OK
    completion = f" ({detail['completion']})" if detail["completion"] else ""
    print(f"run {detail['id']}  {detail['title']}")
    print(f"state: {_state_text(detail['state'], detail['owner_gone'])}{completion} — "
          f"{counts_text(detail['verdict_summary'])}")
    print(f"planned: {detail['planned_count']} item(s); created {detail['created_at']}; "
          f"origin {detail['origin']}; labels {detail['labels'] or '{}'}")
    for dispatch in detail["dispatches"]:
        done = f" ({dispatch['completion']})" if dispatch["completion"] else ""
        print(f"dispatch {dispatch['dispatch_id']}  {dispatch['mode']}  "
              f"{_state_text(dispatch['state'], dispatch['owner_gone'])}{done}  "
              f"{counts_text(dispatch['summary'])}")
        if dispatch["error"]:
            print(f"  error: {dispatch['error']}")
    _print_shards(detail)
    for record in flagged:
        print(_owner_gone_hint(run.id, record))
    for artifact in detail["artifacts"]:
        print(f"  artifact {artifact['name']}: {artifact['href'] or artifact['url_or_path']}")
    return EXIT_OK


def cmd_runs(args: argparse.Namespace) -> int:
    from runcomposer.app.views import run_row

    with service_for(args) as service:
        labels = parse_labels(args.label)
        if args.failed_in:
            # --label scopes which runs 'latest' may pick from — without it,
            # "latest" ranges over every completed run in the store (§7).
            item_ids, provenance = service.resolve_history(
                f"failed@{args.failed_in}", labels=labels or None
            )
            if args.as_json:
                emit_json({"item_ids": item_ids, "derived_from": provenance})
                return EXIT_OK
            scope = provenance["query"].get("labels")
            print(
                f"# {len(item_ids)} item(s) FAILED in run {provenance['resolved_run_id']}"
                + (f" (scope: {scope})" if scope else "")
            )
            for item_id in item_ids:
                print(item_id)
            return EXIT_OK
        page = service.list_runs(
            state=args.state, labels=labels or None, since=args.since, until=args.until,
            q=args.query, limit=args.limit, offset=args.offset,
        )
        rows = [run_row(run, listing=True) for run in page.runs]
        flagged = [_mark_owner_gone(service, run, row) for run, row in zip(page.runs, rows, strict=True)]
    if args.as_json:
        emit_json({"runs": rows, "total": page.total, "limit": args.limit, "offset": args.offset})
        return EXIT_OK
    if not page.runs:
        print("no runs stored" + (f" in state {args.state}" if args.state else ""))
        return EXIT_OK
    states = [_state_text(run.state, row["owner_gone"]) for run, row in zip(page.runs, rows, strict=True)]
    width = max(16, *(len(state) for state in states))
    print(f"{'RUN ID':<26}  {'STATE':<{width}}  {'RESULT':<10}  {'MODE':<12}  {'CREATED':<20}  TITLE")
    for run, state in zip(page.runs, states, strict=True):
        mode = run.latest_dispatch.mode if run.latest_dispatch else "-"
        print(
            f"{run.id:<26}  {state:<{width}}  {run.completion or '-':<10}  {mode:<12}  "
            f"{run.created_at:<20}  {run.title}"
        )
    if page.total > len(page.runs):
        print(f"# {len(page.runs)} of {page.total} run(s) — page with --limit / --offset")
    for run, records in zip(page.runs, flagged, strict=True):
        for record in records:
            print(_owner_gone_hint(run.id, record))
    return EXIT_OK


def cmd_finalize(args: argparse.Namespace) -> int:
    from runcomposer.app.views import run_row

    with service_for(args) as service:
        run = service.finalize(args.run_id)
    if args.as_json:
        emit_json(run_row(run))
    else:
        latest = run.latest_dispatch
        print(f"run {run.id}: {run.state} ({run.completion}) — "
              f"{counts_text(dict(latest.summary) if latest else {})}")
    return run_exit_code(run)


def cmd_delete(args: argparse.Namespace) -> int:
    with service_for(args) as service:
        service.delete_run(args.run_id)
    print(f"run {args.run_id} deleted")
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    if args.format != "ctrf":
        print(f"error: unknown export format {args.format!r} (supported: ctrf)", file=sys.stderr)
        return EXIT_USAGE
    with service_for(args) as service:
        document = service.export_ctrf(args.run_id)
    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
        print(f"CTRF report written to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(payload)
    return EXIT_OK


def cmd_export_spec(args: argparse.Namespace) -> int:
    from .compose import write_token

    with service_for(args) as service:
        exported = service.export(args.run_id, format=args.format)
    if args.out:
        with open(args.out, "wb") as handle:
            handle.write(exported.content)
        print(f"spec written to {args.out}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(exported.content)
        sys.stdout.flush()
    print(
        f"export dispatch: {exported.dispatch.dispatch_id} "
        f"({exported.dispatch.declared_shards} shard(s) declared, state AWAITING_RESULTS)",
        file=sys.stderr,
    )
    print(f"spec sha256: {exported.dispatch.spec_sha256}", file=sys.stderr)
    write_token(exported.token, args.token_file)
    return EXIT_OK
