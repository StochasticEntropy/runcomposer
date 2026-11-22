"""``runcomposer`` — argument parsing and the one place errors become exit
codes."""

from __future__ import annotations

import argparse
import errno
import io
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


def _reader_gone(exc: OSError) -> bool:
    """Whether ``exc`` is the reader of stdout having closed the pipe. POSIX
    says so with ``EPIPE`` (``BrokenPipeError``). Windows says ``EINVAL``,
    which other failures say too, so there it counts only when stdout
    itself cannot be written: flushing it fails again (the bytes that
    failed are still in its buffer), or a probe written past the buffer
    does. (Where stdout can be written after all, the probe is one line
    break in an output that ends in an error anyway.)"""
    if isinstance(exc, BrokenPipeError):
        return True
    if os.name != "nt" or exc.errno != errno.EINVAL:
        return False
    try:
        sys.stdout.flush()
        os.write(sys.stdout.fileno(), b"\n")
    except OSError as again:
        return again.errno in (errno.EINVAL, errno.EPIPE)
    except ValueError:  # stdout is closed or has no file descriptor
        return False
    return False


def _text_output() -> None:
    """Make stdout and stderr able to write every character: what runcomposer
    prints holds titles, item names and labels in any script, and its own
    ``→`` and ``─``. Into a file or a pipe they write UTF-8 — the encoding
    a reader of ``--json`` expects; Windows would write its legacy code
    page there, which cannot hold most characters, and stop with a
    traceback at the first. To a terminal they keep its encoding, and a
    character it lacks is replaced instead of ending the command."""
    for stream in (sys.stdout, sys.stderr):
        if not isinstance(stream, io.TextIOWrapper):
            continue
        if stream.encoding.lower().replace("-", "").replace("_", "") == "utf8":
            continue
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            continue  # a stream that cannot change keeps what it has


#: The commands that plan or run with a runner.
_PLANS_OR_RUNS = frozenset({"run", "dispatch", "rerun", "plan", "spec"})


def main(argv: list[str] | None = None) -> int:
    from runcomposer.core.errors import ConfigError, RunComposerError, RunnerUnavailable
    from runcomposer.core.taxonomy import TaxonomyError

    _text_output()
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
    except OSError as exc:
        if not _reader_gone(exc):
            raise
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
