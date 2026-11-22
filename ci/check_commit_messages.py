#!/usr/bin/env python3
"""Hold commit messages to the example-world rules (DESIGN.md §12).

    python ci/check_commit_messages.py [REV_RANGE]

checks the message of every commit in ``REV_RANGE`` (default: the commits
of ``HEAD`` that are not on ``origin/main``, or on ``main`` when there is
no remote branch) with the rules ``tests/test_example_world.py`` holds the
tree to — imported from ``tests/example_world_rules.py``, not restated:
hosts, absolute paths, ticket keys, tags written in the text, the reviewed
prose vocabulary (``tests/example_world_words.txt``) — plus the process
words a commit message never uses (how a change came about is not part of
the project's history; see ``PROCESS_WORDS`` there for the words and their
carved-out phrases).

A trailing block of git trailers (``Key: value`` lines, such as an
attribution line) is metadata, not prose, and is not checked.

Exit status: 0 when every message passes, or when there is nothing to
check because this is not a git checkout (an sdist, a container without
``.git`` — the reason is printed); 1 when a message breaks a rule (each
violation is printed as ``<commit>:<line>: [rule] 'token'``); 2 when the
range cannot be read.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests import example_world_rules as rules  # noqa: E402

_TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: \S")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def is_checkout(cwd: Path) -> bool:
    try:
        return git(cwd, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except (OSError, subprocess.CalledProcessError):
        return False


def default_range(cwd: Path) -> str | None:
    """``origin/main..HEAD`` (else ``main..HEAD``); ``None`` when neither
    exists — the whole history is never a default: it holds what was
    published before these rules."""
    for base in ("origin/main", "main"):
        try:
            git(cwd, "rev-parse", "--verify", "--quiet", base)
        except subprocess.CalledProcessError:
            continue
        return f"{base}..HEAD"
    return None


def without_trailers(message: str) -> str:
    """The message without its closing block of git trailers."""
    paragraphs = message.rstrip("\n").split("\n\n")
    if len(paragraphs) > 1 and all(_TRAILER.match(line) for line in paragraphs[-1].splitlines()):
        paragraphs = paragraphs[:-1]
    return "\n\n".join(paragraphs) + "\n"


def commits(rev_range: str, cwd: Path = REPO) -> list[tuple[str, str]]:
    """(short hash, message) of every commit in the range, oldest first."""
    out = git(cwd, "log", "--reverse", "--format=%h%x00%B%x1e", rev_range)
    found = []
    for record in out.split("\x1e"):
        record = record.lstrip("\n")
        if record:
            sha, _, message = record.partition("\x00")
            found.append((sha, message))
    return found


def check(rev_range: str, cwd: Path = REPO) -> list[rules.Violation]:
    violations: list[rules.Violation] = []
    for sha, message in commits(rev_range, cwd):
        violations.extend(rules.scan_message(sha, without_trailers(message)))
    return violations


def main(argv: list[str], cwd: Path = REPO) -> int:
    if not is_checkout(cwd):
        print("check_commit_messages: skipped — not a git checkout, there are no commits to check")
        return 0
    rev_range = argv[1] if len(argv) > 1 else default_range(cwd)
    if rev_range is None:
        print(
            "check_commit_messages: there is no origin/main or main to compare with — "
            "give the range of commits to check, e.g. `python ci/check_commit_messages.py BASE..HEAD`",
            file=sys.stderr,
        )
        return 2
    try:
        checked = len(commits(rev_range, cwd))
        violations = check(rev_range, cwd)
    except subprocess.CalledProcessError as exc:
        print(f"check_commit_messages: cannot read {rev_range!r}: {exc.stderr.strip()}", file=sys.stderr)
        return 2
    if violations:
        print(rules.report(violations))
        print(f"\n{len(violations)} violation(s) in the messages of {checked} commit(s) of {rev_range}")
        return 1
    print(f"check_commit_messages: {checked} commit message(s) of {rev_range} pass")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
