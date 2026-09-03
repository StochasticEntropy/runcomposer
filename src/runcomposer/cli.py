"""runcomposer CLI (DESIGN.md §9).

P1 surface: validate · demo · catalog · compile · spec · runs · ingest ·
serve. The vendorable `runcomposer-exec` consumer is its own entry point
(see runcomposer_exec.py).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any

import yaml

from runcomposer import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="runcomposer",
        description="Tag-based test run composer & orchestrator.",
    )
    parser.add_argument("--version", action="version", version=f"runcomposer {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="validate a runspec document (YAML or JSON)")
    p_validate.add_argument("spec", help="path to the runspec file")
    p_validate.add_argument(
        "--for-dispatch",
        action="store_true",
        help="additionally require the dispatch/export profile: "
        "materialized selection + results contract (DESIGN.md §3.1)",
    )

    p_demo = sub.add_parser("demo", help="boot the neutral web-shop demo end-to-end")
    p_demo.add_argument(
        "--workspace",
        metavar="DIR",
        help="where to seed the demo's config + store (default: ./runcomposer-demo). "
        "The directory must be empty or a previous demo workspace; delete it to "
        "undo the demo entirely (DESIGN.md §12)",
    )

    p_catalog = sub.add_parser(
        "catalog", help="list the catalog, its tags and its snapshot hash"
    )
    p_catalog.add_argument("--manifest", help="read this manifest file instead of the "
                           "configured source (default: the configured source, else the "
                           "bundled demo corpus)")
    p_catalog.add_argument("--tags", action="store_true",
                           help="list every tag in the catalog with the number of items "
                           "carrying it, instead of the items")
    p_catalog.add_argument("--limit", type=int, default=0, help="show at most N items")
    _add_config_arg(p_catalog)

    p_taxonomy = sub.add_parser(
        "taxonomy-check",
        help="compare the configured taxonomy with the catalog: tags no leaf "
        "claims, and leaves that match nothing",
    )
    p_taxonomy.add_argument("--warn-only", action="store_true",
                            help="report and exit 0 (default: exit 1 when either side drifts)")
    p_taxonomy.add_argument("--limit", type=int, default=0,
                            help="show at most N entries per section. 0 means no limit")
    _add_config_arg(p_taxonomy)

    p_compile = sub.add_parser("compile", help="preview a selection: matched items + warnings")
    _add_selection_args(p_compile)
    _add_config_arg(p_compile)

    p_spec = sub.add_parser(
        "spec", help="compose a run and emit its runspec (the export workflow's compose step)"
    )
    _add_selection_args(p_spec)
    p_spec.add_argument("--title", default="Untitled run", help="run title")
    p_spec.add_argument("--label", action="append", default=[], metavar="KEY=VALUE",
                        help="free-form provenance label (repeatable)")
    p_spec.add_argument("--format", choices=("yaml", "json"), default="yaml")
    p_spec.add_argument("-o", "--out", help="write the spec to this file instead of stdout")
    p_spec.add_argument("--export", action="store_true",
                        help="mint an export dispatch for the emitted document (DESIGN.md §4)")
    p_spec.add_argument("--runner", dest="runner_section", metavar="RUNNER_ID",
                        help="embed this runner's configured options as the spec's "
                        "runner section (the one open section, DESIGN.md §3)")
    p_spec.add_argument("--expect-format", default="runcomposer-verdicts", metavar="FORMAT",
                        help="results format the run expects back (registered "
                        "ResultParser id, e.g. robot-output-xml)")
    p_spec.add_argument("--from-history", metavar="QUERY",
                        help="history-based selection, resolved at compose time with "
                        "derived_from provenance (DESIGN.md §7), e.g. 'failed@latest'")
    _add_config_arg(p_spec)

    p_export = sub.add_parser("export", help="export a run's results as a normalized document")
    p_export.add_argument("run_id", help="run id to export")
    p_export.add_argument("--format", required=True, help="export format (supported: ctrf)")
    p_export.add_argument("-o", "--out", help="write to this file instead of stdout")
    _add_config_arg(p_export)

    p_dispatch = sub.add_parser(
        "dispatch", help="dispatch a runspec file to an in-process runner (DESIGN.md §9)"
    )
    p_dispatch.add_argument("spec", help="path to the runspec file (YAML or JSON)")
    p_dispatch.add_argument("--runner", required=True, help="runner plugin id (e.g. robot-pool, demo)")
    _add_config_arg(p_dispatch)

    p_runs = sub.add_parser("runs", help="list stored runs")
    p_runs.add_argument("--failed-in", metavar="SELECTOR",
                        help="print item ids that FAILED in the selected run "
                        "(latest | run:<id> | before:<ISO time>, each optionally scoped "
                        "with '?key=value&…' or with --label) — DESIGN.md §7/§9")
    p_runs.add_argument("--state", help="filter by lifecycle state (e.g. COMPLETE)")
    p_runs.add_argument("--label", action="append", default=[], metavar="KEY=VALUE",
                        help="restrict to runs carrying this label (repeatable; all must "
                        "match). Filters the listing — and, with --failed-in, scopes which "
                        "runs 'latest' may choose from")
    p_runs.add_argument("--since", help="only runs created at/after this ISO-8601 UTC time")
    p_runs.add_argument("--until", help="only runs created at/before this ISO-8601 UTC time")
    p_runs.add_argument("--limit", type=int, default=20)
    _add_config_arg(p_runs)

    p_ingest = sub.add_parser("ingest", help="ingest a results bundle (dir or file)")
    p_ingest.add_argument("bundle", help="path to the results bundle")
    p_ingest.add_argument("--run", help="run id (required when the bundle has no marker)")
    p_ingest.add_argument("--dispatch", help="dispatch id (default: from marker, else latest)")
    p_ingest.add_argument("--shard", help="shard label (default: from marker, else '1')")
    p_ingest.add_argument(
        "--allow-unsolicited",
        action="store_true",
        help="promote an unsolicited bundle (no marker / unknown run) to its own "
        "run with origin 'ingested' (DESIGN.md §4)",
    )
    _add_config_arg(p_ingest)

    p_gc = sub.add_parser(
        "gc", help="apply retention: bound the quarantine, expire processed inbox "
        "entries, artifacts, and old runs (DESIGN.md §5, §6.4)"
    )
    _add_config_arg(p_gc)

    p_serve = sub.add_parser("serve", help="run the API + UI server")
    p_serve.add_argument("--host", help="bind host (default: from config, 127.0.0.1)")
    p_serve.add_argument("--port", type=int, help="bind port (default: from config, 8100)")
    _add_config_arg(p_serve)

    args = parser.parse_args(argv)
    handlers = {
        "validate": _cmd_validate,
        "demo": _cmd_demo,
        "catalog": _cmd_catalog,
        "taxonomy-check": _cmd_taxonomy_check,
        "compile": _cmd_compile,
        "spec": _cmd_spec,
        "runs": _cmd_runs,
        "ingest": _cmd_ingest,
        "gc": _cmd_gc,
        "dispatch": _cmd_dispatch,
        "export": _cmd_export,
        "serve": _cmd_serve,
    }
    from runcomposer.config import ConfigError
    from runcomposer.core.taxonomy import TaxonomyError

    try:
        return handlers[args.command](args)
    except (ConfigError, TaxonomyError) as exc:
        # Plugin resolution is lazy, so a bad config can surface past
        # load_config — still a config error, still a clean one-liner (§8).
        # A malformed taxonomy is the same class of problem: hand-written
        # deployment data that must fail loudly, not quietly serve nothing.
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="config file (default: ./config.yaml if present)")


def _add_selection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "filter",
        nargs="?",
        help="tag filter: a pattern string ('Payments', 'prefix:Checkout-') or a "
        "YAML/JSON AST ('{op: AND, items: [Payments, {not: prefix:Quarantine-}]}')",
    )
    parser.add_argument("--id", action="append", default=[], dest="item_ids", metavar="ITEM_ID",
                        help="explicit item pick (repeatable; intersects with the filter)")


def _selection_data(args: argparse.Namespace) -> dict[str, Any]:
    selection: dict[str, Any] = {}
    if args.filter:
        try:
            selection["tag_filter"] = yaml.safe_load(args.filter)
        except yaml.YAMLError as exc:
            print(f"error: cannot parse filter: {exc}", file=sys.stderr)
            raise SystemExit(2) from None
    if args.item_ids:
        selection["item_ids"] = list(args.item_ids)
    if getattr(args, "from_history", None):
        selection["history"] = args.from_history
    if not selection:
        print("error: provide a filter, --id picks, and/or --from-history", file=sys.stderr)
        raise SystemExit(2)
    return selection


def _service(args: argparse.Namespace):
    from runcomposer.config import ConfigError, load_config
    from runcomposer.service import Service

    try:
        return Service(load_config(args.config))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def _parse_labels(pairs: list[str]) -> dict[str, str]:
    labels = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            print(f"error: --label must be KEY=VALUE, got {pair!r}", file=sys.stderr)
            raise SystemExit(2)
        labels[key] = value
    return labels


# -- commands -----------------------------------------------------------------


def _cmd_demo(args: argparse.Namespace) -> int:
    from runcomposer.demo.demo import DemoError, run_demo

    try:
        return run_demo(workspace=args.workspace)
    except DemoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _cmd_validate(args: argparse.Namespace) -> int:
    from runcomposer.core.spec import SpecLoadError, load_document, validate_document

    try:
        doc = load_document(args.spec)
    except (OSError, SpecLoadError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report = validate_document(doc, for_dispatch=args.for_dispatch)
    for warning in report.warnings:
        print(f"warning: {warning}")
    if report.ok:
        profile = " (dispatch profile)" if args.for_dispatch else ""
        print(f"{args.spec}: valid runspec document{profile}")
        return 0
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)
    print(f"{args.spec}: INVALID ({len(report.errors)} error(s))", file=sys.stderr)
    return 1


def _cmd_catalog(args: argparse.Namespace) -> int:
    """The catalog every other command selects from.

    ``--manifest`` reads one file directly — the zero-config path, and what
    this command did when it could do nothing else. Otherwise it lists the
    source the config actually configures, because "which tests can I select,
    and by which tags" is a question about the deployment, not about a file
    the deployment may not even use.
    """
    from importlib import resources

    from runcomposer.plugins.manifest_source import ManifestError, ManifestSource

    if args.manifest:
        try:
            source = ManifestSource(args.manifest)
        except (OSError, ManifestError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    else:
        from runcomposer.config import load_config

        config = load_config(args.config)
        if config.path is None and args.config is None:
            # No config to speak of: the bundled demo corpus, as before.
            source = ManifestSource(resources.files("runcomposer.demo") / "corpus.json")
        else:
            try:
                source = config.build_source()
            except (OSError, ValueError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2

    items = source.items()
    tags = Counter(tag for item in items for tag in item.tags)
    print(f"# {len(items)} items, {len(tags)} distinct tags — snapshot {source.snapshot()}")

    duplicates = getattr(source, "duplicate_ids", ())
    if duplicates:
        print(
            f"# warning: {len(duplicates)} id(s) belong to more than one item — a selection "
            "cannot tell them apart:"
        )
        for item_id in duplicates:
            print(f"#   {item_id}")

    if args.tags:
        shown = tags.most_common(args.limit) if args.limit else sorted(tags.items())
        for tag, count in shown:
            print(f"{count:6d}  {tag}")
        if args.limit and len(tags) > args.limit:
            print(f"# … {len(tags) - args.limit} more (use --limit 0 for all)")
        return 0

    shown = items[: args.limit] if args.limit else items
    for item in shown:
        print(f"{item.id}  [{', '.join(item.tags)}]")
    if args.limit and len(items) > args.limit:
        print(f"# … {len(items) - args.limit} more (use --limit 0 for all)")
    return 0


def _cmd_taxonomy_check(args: argparse.Namespace) -> int:
    """Hold the taxonomy against the catalog, in both directions.

    The taxonomy is hand-written data and the catalog moves underneath it, so
    the two drift apart silently: a newly introduced tag has no home in the
    tree and is invisible to anyone browsing it, and a leaf whose tag was
    renamed stays clickable and selects nothing. Neither shows up anywhere —
    the tree renders, the filter parses, the answer is just empty. This is the
    check that says so.
    """
    from runcomposer.core.filter import parse_filter

    service = _service(args)
    taxonomy = service.taxonomy().get("taxonomy") or []

    leaves: list[tuple[str, str]] = []  # (label path, pattern)

    def walk(nodes: list[Any], path: str) -> None:
        for node in nodes:
            label = f"{path} › {node['label']}" if path else node["label"]
            if node.get("filter"):
                leaves.append((label, node["filter"]))
            walk(node.get("children") or [], label)

    walk(taxonomy, "")

    items = service.source.items()
    tags = sorted({tag for item in items for tag in item.tags})
    matchers = [(label, pattern, parse_filter(pattern)) for label, pattern in leaves]

    unclaimed = [tag for tag in tags if not any(node.matches([tag]) for _l, _p, node in matchers)]
    dead = [
        (label, pattern)
        for label, pattern, node in matchers
        if not any(node.matches([tag]) for tag in tags)
    ]

    print(f"# {len(leaves)} taxonomy leaf pattern(s) over {len(tags)} distinct catalog tag(s)")

    def section(title: str, rows: list[str]) -> None:
        print(f"\n{title}: {len(rows)}")
        shown = rows[: args.limit] if args.limit else rows
        for row in shown:
            print(f"  {row}")
        if args.limit and len(rows) > args.limit:
            print(f"  … {len(rows) - args.limit} more (use --limit 0 for all)")

    if unclaimed:
        section("tags no leaf claims (invisible in the tree)", unclaimed)
    else:
        print("\nevery catalog tag is reachable from the taxonomy")
    if dead:
        section(
            "leaves that match nothing (clickable, selects nothing)",
            [f"{label}  →  {pattern}" for label, pattern in dead],
        )
    else:
        print("every taxonomy leaf matches at least one tag")

    if args.warn_only:
        return 0
    return 1 if (unclaimed or dead) else 0


def _cmd_compile(args: argparse.Namespace) -> int:
    from runcomposer.core.filter import FilterError
    from runcomposer.core.selection import SelectionError

    service = _service(args)
    try:
        items, warnings = service.preview(_selection_data(args))
    except (FilterError, SelectionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"warning: {warning}")
    print(f"# {len(items)} item(s) matched")
    for item in items:
        print(f"{item.id}  [{', '.join(item.tags)}]")
    return 0


def _cmd_spec(args: argparse.Namespace) -> int:
    from runcomposer.core.filter import FilterError
    from runcomposer.core.selection import SelectionError

    service = _service(args)
    runner_section = None
    if args.runner_section:
        runner_section = {args.runner_section: service.config.runner_options(args.runner_section)}
    try:
        result = service.compose_run(
            _selection_data(args),
            title=args.title,
            labels=_parse_labels(args.label),
            origin="cli",
            runner_section=runner_section,
            expect_format=args.expect_format,
        )
    except (FilterError, SelectionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    assert result.run is not None
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if args.format == "json":
        payload = json.dumps(result.spec, indent=2, ensure_ascii=False) + "\n"
    else:
        payload = yaml.safe_dump(result.spec, sort_keys=False, allow_unicode=True)
    spec_bytes = payload.encode("utf-8")

    if args.out:
        with open(args.out, "wb") as fh:
            fh.write(spec_bytes)
        print(f"spec written to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(payload)

    print(f"run: {result.run.id} (state COMPOSED)", file=sys.stderr)
    if args.export:
        dispatch = service.export_dispatch(result.run.id, spec_bytes=spec_bytes)
        print(
            f"export dispatch: {dispatch.dispatch_id} "
            f"({dispatch.declared_shards} shard(s) declared, state AWAITING_RESULTS)",
            file=sys.stderr,
        )
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    service = _service(args)
    if args.failed_in:
        from runcomposer.service import ServiceError

        # --label scopes which runs 'latest' may pick from — without it,
        # "latest" is the latest completed run of ANYTHING in the store, and
        # on a shared instance that is somebody else's selection (§7).
        try:
            item_ids, provenance = service.resolve_history(
                f"failed@{args.failed_in}", labels=_parse_labels(args.label) or None
            )
        except ServiceError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        scope = provenance["query"].get("labels")
        print(
            f"# {len(item_ids)} item(s) FAILED in run {provenance['resolved_run_id']}"
            + (f" (scope: {scope})" if scope else "")
        )
        for item_id in item_ids:
            print(item_id)
        return 0
    runs = service.store.list_runs(
        state=args.state,
        labels=_parse_labels(args.label) or None,
        since=args.since,
        until=args.until,
        limit=args.limit,
    )
    if not runs:
        print("no runs stored" + (f" in state {args.state}" if args.state else ""))
        return 0
    print(f"{'RUN ID':<26}  {'STATE':<16}  {'RESULT':<6}  {'CREATED':<20}  TITLE")
    for run in runs:
        print(
            f"{run.id:<26}  {run.state:<16}  {run.completion or '-':<6}  "
            f"{run.created_at:<20}  {run.title}"
        )
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    from runcomposer.service import IngestError

    service = _service(args)
    try:
        report = service.ingest(
            args.bundle, run_id=args.run, dispatch_id=args.dispatch, shard=args.shard
        )
    except IngestError as exc:
        if args.allow_unsolicited and exc.reason in ("unsolicited", "unknown-run"):
            try:
                report = service.promote_bundle(args.bundle)
            except IngestError as promote_exc:
                print(f"error: {promote_exc}", file=sys.stderr)
                return 1
            print(f"unsolicited bundle promoted to run {report.run_id} (origin: ingested)")
        else:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    for warning in report.warnings:
        print(f"warning: {warning}")
    outcome = {
        "new": "delivery recorded",
        "duplicate": "byte-identical bundle already ingested — no-op",
        "replaced": "shard re-delivered — previous verdicts replaced (last-writer-wins)",
    }[report.outcome]
    print(f"{report.run_id} shard {report.shard}: {outcome} ({report.verdict_count} verdict(s))")
    completion = f" ({report.completion})" if report.completion else ""
    print(f"run state: {report.run_state}{completion}")
    return 0


def _cmd_dispatch(args: argparse.Namespace) -> int:
    from runcomposer.core.ports import DispatchRefused
    from runcomposer.core.spec import SpecLoadError, load_document
    from runcomposer.service import ServiceError

    service = _service(args)
    try:
        doc = load_document(args.spec)
    except (OSError, SpecLoadError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        run_id = service.import_spec(doc, origin="cli")
        dispatch = service.dispatch_runner(run_id, args.runner)
    except ServiceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except DispatchRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if service.last_dispatch_plan:
        print(service.last_dispatch_plan)
    run = service.store.get_run(run_id)
    summary = service.verdict_summary(run_id, dispatch.dispatch_id)
    counts = ", ".join(f"{n} {status}" for status, n in sorted(summary.items())) or "no verdicts"
    print(f"run {run_id}: dispatch {dispatch.dispatch_id} via '{args.runner}' "
          f"({dispatch.declared_shards} shard(s))")
    completion = f" ({run.completion})" if run.completion else ""
    print(f"state: {run.state}{completion} — {counts}")
    return 0


def _cmd_gc(args: argparse.Namespace) -> int:
    service = _service(args)
    report = service.gc()
    quarantine_removed = report["quarantine_removed"]
    print(f"quarantine: removed {len(quarantine_removed)} entr(y/ies) beyond the configured bound")
    for entry_id in quarantine_removed:
        print(f"  removed {entry_id}")
    print(f"inbox/processed: removed {report['inbox_processed_removed']} expired bundle(s)")
    print(f"artifacts: removed {report['artifacts_removed']} expired file(s)")
    print(f"store: pruned {len(report['runs_removed'])} run(s) past retention")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    from runcomposer.service import ServiceError

    if args.format != "ctrf":
        print(f"error: unknown export format {args.format!r} (supported: ctrf)", file=sys.stderr)
        return 2
    service = _service(args)
    try:
        document = service.export_ctrf(args.run_id)
    except ServiceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
        print(f"CTRF report written to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(payload)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from runcomposer.api import create_app
    from runcomposer.config import ConfigError, load_config
    from runcomposer.core.taxonomy import TaxonomyError

    try:
        config = load_config(args.config)
        # Resolves all plugins AND validates the taxonomy; fails loudly (§8).
        app = create_app(config)
    except (ConfigError, TaxonomyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    host = args.host or config.api["host"]
    port = args.port or config.api["port"]
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
