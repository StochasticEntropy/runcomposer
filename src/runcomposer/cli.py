"""runcomposer CLI (DESIGN.md §9) — P0 commands: validate, demo, catalog."""

from __future__ import annotations

import argparse
import sys

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

    sub.add_parser("demo", help="boot the neutral web-shop demo end-to-end")

    p_catalog = sub.add_parser("catalog", help="list a manifest catalog and its snapshot hash")
    p_catalog.add_argument("--manifest", help="manifest file (default: the bundled demo corpus)")
    p_catalog.add_argument("--limit", type=int, default=0, help="show at most N items")

    args = parser.parse_args(argv)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "demo":
        from runcomposer.demo.demo import run_demo

        return run_demo()
    if args.command == "catalog":
        return _cmd_catalog(args)
    return 2  # unreachable: subparsers are required


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
    from importlib import resources

    from runcomposer.plugins.manifest_source import ManifestError, ManifestSource

    manifest = args.manifest or resources.files("runcomposer.demo") / "corpus.json"
    try:
        source = ManifestSource(manifest)
    except (OSError, ManifestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    items = source.items()
    print(f"# {len(items)} items — snapshot {source.snapshot()}")
    shown = items[: args.limit] if args.limit else items
    for item in shown:
        print(f"{item.id}  [{', '.join(item.tags)}]")
    if args.limit and len(items) > args.limit:
        print(f"# … {len(items) - args.limit} more (use --limit 0 for all)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
