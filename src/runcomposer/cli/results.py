"""Results in and housekeeping: ingest · quarantine · history · gc."""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from typing import Any

from .common import (
    EXIT_NO,
    EXIT_OK,
    add_config_arg,
    add_json_arg,
    emit_json,
    parse_labels,
    service_for,
)


def register(sub: Any) -> dict[str, Any]:
    p_ingest = sub.add_parser("ingest", help="ingest a results bundle (dir or file)")
    p_ingest.add_argument("bundle", help="path to the results bundle")
    p_ingest.add_argument("--run", help="run id (required when the bundle has no marker)")
    p_ingest.add_argument("--dispatch", help="dispatch id (default: from marker, else the one "
                          "awaiting results, else the latest)")
    p_ingest.add_argument("--shard", help="shard label (default: from marker, else '1')")
    p_ingest.add_argument("--format", help="result format id (default: what the run expects)")
    p_ingest.add_argument(
        "--allow-unsolicited",
        action="store_true",
        help="promote an unsolicited bundle (no marker / unknown run) to its own "
        "run with origin 'ingested' (DESIGN.md §4)",
    )
    add_json_arg(p_ingest)
    add_config_arg(p_ingest)

    p_quarantine = sub.add_parser(
        "quarantine", help="list, attach, promote or discard quarantined deliveries"
    )
    q_sub = p_quarantine.add_subparsers(dest="action", required=True)
    q_list = q_sub.add_parser("list", help="what is in quarantine, oldest first")
    add_json_arg(q_list)
    add_config_arg(q_list)
    q_attach = q_sub.add_parser("attach", help="bind an entry to an existing run")
    q_attach.add_argument("entry_id")
    q_attach.add_argument("run_id")
    q_attach.add_argument("--format", help="result format id (default: what the run expects)")
    add_json_arg(q_attach)
    add_config_arg(q_attach)
    q_promote = q_sub.add_parser("promote", help="make an entry its own run (origin 'ingested')")
    q_promote.add_argument("entry_id")
    q_promote.add_argument("--format", help="result format id, when the entry does not know it")
    add_json_arg(q_promote)
    add_config_arg(q_promote)
    q_discard = q_sub.add_parser("discard", help="delete an entry")
    q_discard.add_argument("entry_id")
    add_config_arg(q_discard)

    p_gc = sub.add_parser(
        "gc", help="apply retention: prune old runs with their artifacts, bound the "
        "quarantine, expire processed inbox entries (DESIGN.md §6.4)"
    )
    add_json_arg(p_gc)
    add_config_arg(p_gc)

    p_history = sub.add_parser(
        "history", help="the duration history runners balance their plans with: import, show"
    )
    h_sub = p_history.add_subparsers(dest="action", required=True)
    h_import = h_sub.add_parser(
        "import",
        help="seed durations from a file, so the first plan is balanced instead of cold. "
        "A seed answers only for a test no execution has measured yet",
    )
    h_import.add_argument("file", help="CSV (columns item, duration_ms or duration_s) or JSON "
                          "({\"durations\": {item: milliseconds}}); item is an id or any native "
                          "name the catalog resolves")
    h_import.add_argument("--label", action="append", default=[], metavar="KEY=VALUE",
                          help="scope the seeds like a run's labels scope its history "
                          "(repeatable) — give what your runner's history_selector asks for")
    add_json_arg(h_import)
    add_config_arg(h_import)
    h_show = h_sub.add_parser(
        "durations", help="print the mean duration per item as a runner's planner sees it"
    )
    h_show.add_argument("--label", action="append", default=[], metavar="KEY=VALUE",
                        help="only history of runs (and seeds) carrying this label (repeatable)")
    h_show.add_argument("--last", type=int, default=5, metavar="N",
                        help="over the last N dispatches that delivered (default 5)")
    h_show.add_argument("--status", action="append", default=[], metavar="STATUS",
                        help="only executions that ended with this status (repeatable), "
                        "e.g. --status PASS")
    add_json_arg(h_show)
    add_config_arg(h_show)

    return {
        "ingest": cmd_ingest, "quarantine": cmd_quarantine, "history": cmd_history, "gc": cmd_gc,
    }


def cmd_history(args: argparse.Namespace) -> int:
    labels = parse_labels(args.label)
    with service_for(args) as service:
        if args.action == "import":
            from runcomposer.app.durations import read_durations_file

            report = service.import_durations(read_durations_file(args.file), labels=labels)
            if args.as_json:
                emit_json(report)
            else:
                scope = f" under labels {report['labels']}" if report["labels"] else ""
                print(f"imported {report['imported']} duration(s){scope}")
                if report["without_duration"]:
                    print(f"  skipped {report['without_duration']} with a duration of 0 "
                          "(0 means: not known)")
                for name in report["unknown"][:20]:
                    print(f"  not in the catalog, skipped: {name}")
                if len(report["unknown"]) > 20:
                    print(f"  … and {len(report['unknown']) - 20} more not in the catalog")
            return EXIT_OK if report["imported"] or not report["unknown"] else EXIT_NO
        durations = service.store.duration_aggregates(
            labels=labels or None, last_n=args.last, statuses=args.status or None
        )
    if args.as_json:
        emit_json({"durations": durations, "items": len(durations)})
        return EXIT_OK
    for item_id in sorted(durations, key=lambda key: (-durations[key], key)):
        print(f"{durations[item_id]:>10.0f} ms  {item_id}")
    print(f"{len(durations)} item(s) with a known duration")
    return EXIT_OK


def _print_report(report: Any, *, as_json: bool) -> None:
    from runcomposer.app.views import ingest_json

    if as_json:
        emit_json(ingest_json(report))
        return
    for warning in report.warnings:
        print(f"warning: {warning}")
    outcome = {
        "new": "delivery recorded",
        "duplicate": "the same bundle was already ingested — no-op",
        "replaced": "shard re-delivered — previous verdicts replaced (last-writer-wins)",
    }[report.outcome]
    print(f"{report.run_id} shard {report.shard}: {outcome} ({report.verdict_count} verdict(s))")
    completion = f" ({report.completion})" if report.completion else ""
    print(f"run state: {report.run_state}{completion}")


def cmd_ingest(args: argparse.Namespace) -> int:
    from runcomposer.core.errors import IngestError

    with service_for(args) as service:
        try:
            report = service.ingest(
                args.bundle, run_id=args.run, dispatch_id=args.dispatch, shard=args.shard,
                format=args.format,
            )
        except IngestError as exc:
            if not (args.allow_unsolicited and exc.reason in ("unsolicited", "unknown-run")):
                raise
            report = service.ingestion.promote_bundle(
                args.bundle, **({"format_id": args.format} if args.format else {})
            )
            print(
                f"unsolicited bundle promoted to run {report.run_id} (origin: ingested)",
                file=sys.stderr if args.as_json else sys.stdout,
            )
    _print_report(report, as_json=args.as_json)
    return EXIT_OK


def cmd_quarantine(args: argparse.Namespace) -> int:
    with service_for(args) as service:
        if args.action == "list":
            entries = service.quarantine.entries()
            if args.as_json:
                emit_json({"entries": [asdict(entry) for entry in entries]})
                return EXIT_OK
            if not entries:
                print("quarantine is empty")
                return EXIT_OK
            print(f"{'ENTRY':<28}  {'RECEIVED':<20}  {'REASON':<18}  {'VIA':<9}  CLAIMED RUN")
            for entry in entries:
                print(
                    f"{entry.entry_id:<28}  {entry.received_at:<20}  {entry.reason:<18}  "
                    f"{entry.transport:<9}  {entry.claimed_run_id or '-'}"
                )
            return EXIT_OK
        if args.action == "attach":
            report = service.ingestion.attach_quarantined(
                args.entry_id, args.run_id, format=args.format
            )
            _print_report(report, as_json=args.as_json)
            return EXIT_OK
        if args.action == "promote":
            report = service.ingestion.promote_quarantined(args.entry_id, format=args.format)
            _print_report(report, as_json=args.as_json)
            return EXIT_OK
        service.quarantine.remove(args.entry_id)
        print(f"quarantine entry {args.entry_id} discarded")
        return EXIT_OK


def cmd_gc(args: argparse.Namespace) -> int:
    with service_for(args) as service:
        report = service.gc()
    if args.as_json:
        emit_json(report)
        return EXIT_OK
    quarantine_removed = report["quarantine_removed"]
    print(f"quarantine: removed {len(quarantine_removed)} entr(y/ies) beyond the configured bound")
    for entry_id in quarantine_removed:
        print(f"  removed {entry_id}")
    print(f"inbox/processed: removed {report['inbox_processed_removed']} expired bundle(s)")
    print(f"store: pruned {len(report['runs_removed'])} run(s) past retention")
    print(f"artifacts: removed {report['artifact_dirs_removed']} run director(y/ies)")
    if report["expired_dispatches"]:
        print(f"dispatches: expired {len(report['expired_dispatches'])} that waited for results "
              "longer than core.dispatch.awaiting_timeout_s")
    return EXIT_OK if report is not None else EXIT_NO
