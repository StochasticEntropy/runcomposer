"""Looking and composing: validate · catalog · taxonomy-check · compile · spec."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from typing import Any

from .common import (
    EXIT_NO,
    EXIT_OK,
    EXIT_USAGE,
    add_config_arg,
    add_json_arg,
    add_param_args,
    add_selection_args,
    emit_json,
    parse_labels,
    parse_params,
    selection_data,
    service_for,
)


def register(sub: Any) -> dict[str, Any]:
    p_validate = sub.add_parser("validate", help="validate a runspec document (YAML or JSON)")
    p_validate.add_argument("spec", help="path to the runspec file")
    p_validate.add_argument(
        "--for-dispatch",
        action="store_true",
        help="additionally require the dispatch/export profile: "
        "materialized selection + results contract (DESIGN.md §3.1)",
    )

    p_catalog = sub.add_parser("catalog", help="list the catalog, its tags and its snapshot hash")
    p_catalog.add_argument("--manifest", help="read this manifest file instead of the "
                           "configured sources")
    p_catalog.add_argument("--tags", action="store_true",
                           help="list every tag with the number of items carrying it, "
                           "instead of the items")
    p_catalog.add_argument("--limit", type=int, default=0, help="show at most N entries")
    add_json_arg(p_catalog)
    add_config_arg(p_catalog)

    p_taxonomy = sub.add_parser(
        "taxonomy-check",
        help="compare the configured taxonomy with the catalog: tags no leaf "
        "claims, and leaves that match nothing",
    )
    p_taxonomy.add_argument("--warn-only", action="store_true",
                            help="report and exit 0 (default: exit 1 when either side drifts)")
    p_taxonomy.add_argument("--limit", type=int, default=0,
                            help="show at most N entries per section. 0 means no limit")
    p_taxonomy.add_argument("--tree", action="store_true",
                            help="print the resolved tree (the one the UI renders) instead "
                            "of the drift report")
    add_config_arg(p_taxonomy)

    p_compile = sub.add_parser("compile", help="preview a selection: matched items + warnings")
    add_selection_args(p_compile)
    add_json_arg(p_compile)
    add_config_arg(p_compile)

    p_spec = sub.add_parser(
        "spec", help="compose a run and emit its runspec (the export workflow's compose step)"
    )
    add_selection_args(p_spec)
    p_spec.add_argument("--title", default="Untitled run", help="run title")
    p_spec.add_argument("--label", action="append", default=[], metavar="KEY=VALUE",
                        help="free-form provenance label (repeatable)")
    p_spec.add_argument("--format", choices=("yaml", "json"), default="yaml")
    p_spec.add_argument("-o", "--out", help="write the spec to this file instead of stdout")
    p_spec.add_argument("--dry-run", action="store_true",
                        help="compose and print the document without storing a run")
    p_spec.add_argument("--export", action="store_true",
                        help="hand the document out: mint an export dispatch for exactly the "
                        "bytes emitted, and its ingest token (DESIGN.md §4, §5)")
    p_spec.add_argument("--token-file", metavar="FILE",
                        help="with --export: write the ingest token to this file (mode 0600) "
                        "instead of printing it")
    p_spec.add_argument("--runner", metavar="RUNNER_ID",
                        help="compose for this configured runner: validate --param against "
                        "its per-run parameters and write them into the spec's runner section")
    add_param_args(p_spec)
    p_spec.add_argument("--expect-format", metavar="FORMAT",
                        help="results format the run expects back (a ResultParser format id, "
                        "e.g. robot-output-xml; default: the runner's, else runcomposer-verdicts)")
    add_config_arg(p_spec)

    return {
        "validate": cmd_validate,
        "catalog": cmd_catalog,
        "taxonomy-check": cmd_taxonomy_check,
        "compile": cmd_compile,
        "spec": cmd_spec,
    }


def cmd_validate(args: argparse.Namespace) -> int:
    from runcomposer.core.spec import SpecLoadError, load_document, validate_document

    try:
        doc = load_document(args.spec)
    except (OSError, SpecLoadError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    report = validate_document(doc, for_dispatch=args.for_dispatch)
    for warning in report.warnings:
        print(f"warning: {warning}")
    if report.ok:
        profile = " (dispatch profile)" if args.for_dispatch else ""
        print(f"{args.spec}: valid runspec document{profile}")
        return EXIT_OK
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)
    print(f"{args.spec}: INVALID ({len(report.errors)} error(s))", file=sys.stderr)
    return EXIT_NO


#: The source entry ``catalog --manifest`` reads a file with — the key, and
#: by default the plugin name, a deployment's ``sources:`` uses for it.
MANIFEST_ENTRY = "manifest"


def manifest_catalog(config_path: str | None, manifest: str) -> Any:
    """The catalog of one manifest file, built the way a configured source
    is: the ``sources.manifest`` entry of the configuration — its
    ``plugin:`` / ``module:`` and options, so a deployment that configures
    another plugin under that entry is honoured — else the plugin
    registered under the entry-point name ``manifest``; with ``path`` set
    to the file. Options are checked, the plugin is checked against the
    TestSource contract and errors name the entry, as at startup."""
    from pathlib import Path

    from runcomposer.app.catalog import Catalog
    from runcomposer.app.config import Config, load_config

    config = load_config(config_path)
    entry = dict((config.data.get("sources") or {}).get(MANIFEST_ENTRY) or {})
    # Given on the command line: relative to where the command runs, not
    # to the config file's directory.
    entry["path"] = str(Path(manifest).absolute())
    data = {**config.data, "sources": {MANIFEST_ENTRY: entry}}
    return Catalog(Config(data=data, path=config.path))


def cmd_catalog(args: argparse.Namespace) -> int:
    """The catalog every other command selects from: the merged catalog of
    the configured sources, or — with ``--manifest`` — one manifest file."""
    source: Any
    if args.manifest:
        source = manifest_catalog(args.config, args.manifest)
    else:
        source = service_for(args).catalog

    items = source.items()
    tags = Counter(tag for item in items for tag in item.tags)
    duplicates = list(source.duplicate_ids or ())
    if args.as_json:
        emit_json(
            {
                "items": len(items),
                "tags": len(tags),
                "snapshot": source.snapshot(),
                "duplicate_ids": duplicates,
                "entries": (
                    [{"tag": tag, "count": count} for tag, count in sorted(tags.items())]
                    if args.tags
                    else [{"id": item.id, "tags": list(item.tags)} for item in items]
                )[: args.limit or None],
            }
        )
        return EXIT_OK
    print(f"# {len(items)} items, {len(tags)} distinct tags — snapshot {source.snapshot()}")
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
        return EXIT_OK
    for item in items[: args.limit] if args.limit else items:
        print(f"{item.id}  [{', '.join(item.tags)}]")
    if args.limit and len(items) > args.limit:
        print(f"# … {len(items) - args.limit} more (use --limit 0 for all)")
    return EXIT_OK


def taxonomy_drift(service: Any) -> tuple[list[tuple[str, str]], list[str], list[tuple[str, str]], int]:
    """``(leaves, unclaimed tags, dead leaves, tag count)`` — the taxonomy
    held against the catalog in both directions."""
    from runcomposer.core.filter import parse_filter
    from runcomposer.core.taxonomy import leaf_pattern

    leaves: list[tuple[str, str]] = []  # (label path, the leaf's filter as written)

    def walk(nodes: list[Any], path: str) -> None:
        for node in nodes:
            label = f"{path} › {node['label']}" if path else node["label"]
            if node.get("filter"):
                leaves.append((label, node["filter"]))
            walk(node.get("children") or [], label)

    walk(service.taxonomy().get("taxonomy") or [], "")
    tags = sorted(service.catalog.tag_counts())
    matchers = [(label, written, parse_filter(leaf_pattern(written))) for label, written in leaves]
    unclaimed = [tag for tag in tags if not any(node.matches([tag]) for _l, _p, node in matchers)]
    dead = [
        (label, pattern)
        for label, pattern, node in matchers
        if not any(node.matches([tag]) for tag in tags)
    ]
    return leaves, unclaimed, dead, len(tags)


def cmd_taxonomy_check(args: argparse.Namespace) -> int:
    """Hold the taxonomy against the catalog, in both directions: a new tag
    has no home in the tree and is invisible to anyone browsing it; a leaf
    whose tag was renamed stays clickable and selects nothing."""
    service = service_for(args)
    if args.tree:
        return _print_resolved_taxonomy(service, limit=args.limit)
    leaves, unclaimed, dead, tag_count = taxonomy_drift(service)
    print(f"# {len(leaves)} taxonomy leaf pattern(s) over {tag_count} distinct catalog tag(s)")

    def section(title: str, rows: list[str]) -> None:
        print(f"\n{title}: {len(rows)}")
        for row in rows[: args.limit] if args.limit else rows:
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

    resolved = service.resolved_taxonomy()["resolved"]
    print(
        f"\nresolved tree (GET /api/v1/taxonomy?resolve=true, and what the UI renders):"
        f"\n  {resolved['nodes_written']} written node(s) → {resolved['nodes_total']}, "
        f"{resolved['nodes_dropped']} collapsed as matching nothing"
        f"\n  {resolved['tags_selectable']} of {resolved['tags_total']} tag(s) selectable on "
        f"their own; {resolved['tags_unassigned']} reachable only under the unassigned node"
        f"\n  see the tree itself with --tree"
    )
    if args.warn_only:
        return EXIT_OK
    return EXIT_NO if (unclaimed or dead) else EXIT_OK


def _print_resolved_taxonomy(service: Any, *, limit: int) -> int:
    resolved = service.resolved_taxonomy()
    printed = 0

    def walk(nodes: list[Any], depth: int) -> bool:
        nonlocal printed
        for node in nodes:
            if limit and printed >= limit:
                return False
            pattern = f"  {node['filter']}" if node.get("filter") else ""
            print(
                f"{'  ' * depth}{node['label']}{pattern}"
                f"  ({node['item_count']} item(s), {node['tag_count']} tag(s))"
            )
            printed += 1
            if not walk(node.get("children") or [], depth + 1):
                return False
        return True

    complete = walk(resolved["taxonomy"], 0)
    summary = resolved["resolved"]
    if not complete:
        print(f"… {summary['nodes_total'] - printed} more (use --limit 0 for all)")
    print(
        f"\n# {summary['nodes_written']} written node(s) → {summary['nodes_total']}; "
        f"{summary['tags_selectable']} of {summary['tags_total']} tag(s) selectable on their own; "
        f"{summary['items_claimed']} of {summary['items_total']} item(s) reachable"
    )
    return EXIT_OK


def cmd_compile(args: argparse.Namespace) -> int:
    service = service_for(args)
    preview = service.preview(selection_data(args))
    if args.as_json:
        emit_json(
            {
                "count": len(preview.items),
                "total": preview.catalog_size,
                "items": [{"id": item.id, "tags": list(item.tags)} for item in preview.items],
                "warnings": preview.warnings,
                "derived_from": preview.derived_from,
                "snapshot": preview.snapshot,
            }
        )
        return EXIT_OK
    for warning in preview.warnings:
        print(f"warning: {warning}")
    for provenance in preview.derived_from:
        print(f"# from history: run {provenance['resolved_run_id']}")
    print(f"# {len(preview.items)} item(s) matched")
    for item in preview.items:
        print(f"{item.id}  [{', '.join(item.tags)}]")
    return EXIT_OK


def cmd_spec(args: argparse.Namespace) -> int:
    from runcomposer.app.compose import serialize_spec
    from runcomposer.core.errors import InvalidRequest

    service = service_for(args)
    params = parse_params(args.param)
    if params is not None and not args.runner:
        raise InvalidRequest("--param needs --runner: parameters belong to a runner")
    if args.dry_run and args.export:
        raise InvalidRequest("--dry-run stores nothing, so there is no run to --export")
    selection = selection_data(args)
    labels = parse_labels(args.label)

    if args.dry_run:
        from runcomposer.core.parameters import validate_parameters

        section, expect = None, args.expect_format
        if args.runner:
            info = service.runner(args.runner).describe()
            section = {args.runner: validate_parameters(info.parameters, params)}
            expect = expect or info.result_format
        result = service.build_spec_document(
            selection, title=args.title, labels=labels, runner_section=section,
            expect_format=expect,
        )
    else:
        result = service.compose_run(
            selection,
            title=args.title,
            labels=labels,
            origin="cli",
            expect_format=args.expect_format,
            runner_id=args.runner,
            parameters=params,
        )
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    token = None
    if args.export:
        assert result.run is not None
        exported = service.export(result.run.id, format=args.format)
        content, token = exported.content, exported.token
    else:
        content = serialize_spec(result.spec, args.format)

    if args.out:
        with open(args.out, "wb") as handle:
            handle.write(content)
        print(f"spec written to {args.out}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(content)
        sys.stdout.flush()

    if args.dry_run:
        print("dry run: nothing was stored", file=sys.stderr)
        return EXIT_OK
    assert result.run is not None
    print(f"run: {result.run.id} (state COMPOSED)", file=sys.stderr)
    if args.export:
        print(
            f"export dispatch: {exported.dispatch.dispatch_id} "
            f"({exported.dispatch.declared_shards} shard(s) declared, state AWAITING_RESULTS)",
            file=sys.stderr,
        )
        print(f"spec sha256: {exported.dispatch.spec_sha256}", file=sys.stderr)
        write_token(token, args.token_file)
    return EXIT_OK


def write_token(token: str | None, token_file: str | None) -> None:
    """The ingest token exists in this output and nowhere else afterwards."""
    import os

    if token is None:
        return
    if token_file:
        descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        print(f"ingest token written to {token_file}", file=sys.stderr)
    else:
        print(
            f"ingest token (shown once — needed to push results over HTTP): {token}",
            file=sys.stderr,
        )
