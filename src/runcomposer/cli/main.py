"""``runcomposer`` — argument parsing and the one place errors become exit
codes."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from runcomposer import __version__

from . import admin, compose, results, runs
from .common import EXIT_NO, EXIT_NO_RESULT, EXIT_USAGE
from .logs import configure_logging


def build_parser() -> tuple[argparse.ArgumentParser, dict]:
    parser = argparse.ArgumentParser(
        prog="runcomposer",
        description="Tag-based test run composer & orchestrator.",
        epilog="Exit codes: 0 done / run PASS · 1 the answer is no (run not PASS, invalid, "
        "refused, not found) · 2 usage or configuration error · 3 the run ended without a "
        "result (refused, failed, cancelled, stale, timed out). Reference: docs/cli.md",
    )
    parser.add_argument("--version", action="version", version=f"runcomposer {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="log what runcomposer does (the runcomposer.* loggers at INFO)")
    sub = parser.add_subparsers(dest="command", required=True)
    handlers: dict = {}
    for group in (compose, runs, results, admin):
        handlers.update(group.register(sub))
    return parser, handlers


#: The commands that plan or run with a runner.
_PLANS_OR_RUNS = frozenset({"run", "dispatch", "rerun", "plan", "spec"})


def main(argv: list[str] | None = None) -> int:
    from runcomposer.core.errors import ConfigError, RunComposerError, RunnerUnavailable
    from runcomposer.core.taxonomy import TaxonomyError

    parser, handlers = build_parser()
    args = parser.parse_args(argv)
    if args.command != "serve":
        configure_logging(level=logging.INFO if args.verbose else logging.WARNING)
    try:
        try:
            code = int(handlers[args.command](args))
        except ImportError as exc:
            from . import missing_package

            message = missing_package(exc)
            if message is None:
                raise
            print(message, file=sys.stderr)
            return EXIT_USAGE
        # Flush here, inside the handler below: a reader that closed the pipe
        # early (`… | head`) otherwise surfaces as a traceback when the
        # interpreter flushes stdout on its way out.
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        # `runcomposer catalog | head` closes the pipe under us: the reader
        # saying "enough", not a failure. Point stdout at the void so the
        # interpreter's own flush has nothing to fail on.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141
    except (ConfigError, TaxonomyError) as exc:
        # Hand-written deployment data that is wrong: a clean one-liner (§8).
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except RunnerUnavailable as exc:
        # A runner that cannot say what it is. For a command that plans or
        # runs with it, nothing was planned or run: the "no result" exit.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_NO_RESULT if args.command in _PLANS_OR_RUNS else EXIT_NO
    except RunComposerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_NO
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
