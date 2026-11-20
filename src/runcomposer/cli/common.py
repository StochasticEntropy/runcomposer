"""Shared CLI plumbing: the service, flags every command spells the same
way, output, and exit codes (docs/cli.md, "Common ground").

Exit codes, the same for every command:

- ``0`` — done; for commands that wait on a run: the run is ``PASS``.
- ``1`` — the answer is "no": the run completed but is not ``PASS``, a spec
  is invalid, a bundle was refused, a named run does not exist, a check
  found drift.
- ``2`` — the command line or the configuration is wrong.
- ``3`` — the run ended without a result: the dispatch was refused, failed,
  was cancelled or went stale, or the wait timed out.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING, Any, NoReturn

import yaml

if TYPE_CHECKING:
    from runcomposer.app.service import Service
    from runcomposer.core.model import RunRecord

EXIT_OK = 0
EXIT_NO = 1
EXIT_USAGE = 2
EXIT_NO_RESULT = 3


def fail(message: str, code: int = EXIT_USAGE) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="config file (default: ./config.yaml if present)")


def add_json_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="print one JSON document on stdout instead of text")


def add_selection_args(parser: argparse.ArgumentParser, *, history: bool = True) -> None:
    parser.add_argument(
        "filter",
        nargs="?",
        help="tag filter: a pattern string ('Payments', 'prefix:Checkout-') or a "
        "YAML/JSON AST ('{op: AND, items: [Payments, {not: prefix:Quarantine-}]}')",
    )
    parser.add_argument("--id", action="append", default=[], dest="item_ids", metavar="ITEM_ID",
                        help="explicit item pick (repeatable; intersects with the filter)")
    if history:
        parser.add_argument("--from-history", metavar="QUERY",
                            help="history-based selection, resolved at compose time with "
                            "derived_from provenance (DESIGN.md §7), e.g. "
                            "'failed@latest?suite=nightly'")


def add_param_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                        help="per-run runner parameter (repeatable). VALUE is read as YAML, so "
                        "'--param partitions=[env1,env2]' is a list, '--param allow_drift=true' a "
                        "boolean and '--param variables={LOCALE: de, RETRIES: 3}' a mapping whose "
                        "values are taken as text; see `runcomposer doctor` for what a runner accepts")


def add_wait_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-wait", action="store_true",
                        help="return once the runner has handed the work off instead of waiting "
                        "for results that arrive later (a run left AWAITING_RESULTS)")
    parser.add_argument("--timeout", type=float, metavar="SECONDS",
                        help="give up waiting for results after this long (exit 3)")


def selection_data(args: argparse.Namespace) -> dict[str, Any]:
    selection: dict[str, Any] = {}
    if getattr(args, "filter", None):
        try:
            selection["tag_filter"] = yaml.safe_load(args.filter)
        except yaml.YAMLError as exc:
            fail(f"cannot parse filter: {exc}")
    if getattr(args, "item_ids", None):
        selection["item_ids"] = list(args.item_ids)
    if getattr(args, "from_history", None):
        selection["history"] = args.from_history
    if not selection:
        fail("provide a filter, --id picks, and/or --from-history")
    return selection


def parse_labels(pairs: list[str]) -> dict[str, str]:
    labels = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            fail(f"--label must be KEY=VALUE, got {pair!r}")
        labels[key] = value
    return labels


def parse_params(pairs: list[str]) -> dict[str, Any] | None:
    """``NAME=VALUE`` pairs, VALUE read as YAML. ``None`` when none given —
    which means "whatever the spec document carries", not "no parameters"."""
    if not pairs:
        return None
    params: dict[str, Any] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name:
            fail(f"--param must be NAME=VALUE, got {pair!r}")
        try:
            parsed = yaml.safe_load(value)
            if isinstance(parsed, dict):
                # A mapping on the command line is name → text, as a `mapping`
                # parameter wants it: `{RETRIES: 3}` means the text "3",
                # exactly as typed — not a number the runner would refuse.
                parsed = yaml.load(value, Loader=yaml.BaseLoader)
            params[name] = parsed
        except yaml.YAMLError as exc:
            fail(f"--param {name}: cannot parse value: {exc}")
    return params


def service_for(args: argparse.Namespace) -> Service:
    from runcomposer.app.config import load_config
    from runcomposer.app.service import Service

    return Service(load_config(args.config))


def emit_json(document: Any) -> None:
    sys.stdout.write(json.dumps(document, indent=2, ensure_ascii=False) + "\n")


def counts_text(summary: dict[str, int]) -> str:
    order = ("PASS", "FAIL", "ERROR", "SKIP", "MISSING")
    parts = [f"{summary[status]} {status}" for status in order if summary.get(status)]
    return ", ".join(parts) or "no verdicts"


def run_exit_code(run: RunRecord) -> int:
    """0 only for COMPLETE (PASS); 1 for any other completion; 3 when the
    run has no result at all."""
    if run.state == "COMPLETE":
        return EXIT_OK if run.completion == "PASS" else EXIT_NO
    return EXIT_NO_RESULT
