"""The Robot Framework consumer command for runcomposer-exec (DESIGN.md §6.2c):

    runcomposer-exec spec.json --out results \\
        --command "python3 agent/robot_command.py {ids_file} {out_dir} <suite_root>"

Reads the materialized item ids (Robot longnames) from the ids file, runs
exactly those tests out of the executing machine's own checkout, and writes
output.xml into the output dir — which the `robot-output-xml` ResultParser
ingests on the composer side.

Robot Framework is the only import: this file runs where `python3` and
`robot` exist and runcomposer does not.

Executor obligation 2 (DESIGN.md §3.3) lives here. The agent has no catalog
and no TestSource, so it checks the property that actually governs this run:
every requested item id must exist in the live suite. Missing ids are drift —
refused by default, executed as the intersection when RC_ALLOW_DRIFT=1, and
named in drift.json either way.
"""

import json
import os
import sys
from pathlib import Path

import robot
from robot.api import TestSuiteBuilder

DRIFT_FILENAME = "drift.json"


def longnames(suite):
    """Every test longname in the built suite — the agent's live id space."""
    for test in suite.tests:
        yield test.longname
    for child in suite.suites:
        yield from longnames(child)


def main() -> int:
    ids_file, out_dir, suite_root = (Path(arg).resolve() for arg in sys.argv[1:4])
    requested = [line.strip() for line in ids_file.read_text().splitlines() if line.strip()]
    out_dir.mkdir(parents=True, exist_ok=True)

    present = set(longnames(TestSuiteBuilder().build(str(suite_root))))
    drifted = [item_id for item_id in requested if item_id not in present]
    if drifted:
        (out_dir / DRIFT_FILENAME).write_text(
            json.dumps({"suite_root": str(suite_root), "missing_item_ids": drifted}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        print(
            f"drift: {len(drifted)} of {len(requested)} requested item id(s) are not in "
            f"{suite_root} (see {DRIFT_FILENAME})",
            file=sys.stderr,
        )
        if len(drifted) == len(requested):
            print(
                "hint: nothing matched at all — a Robot longname starts at the suite "
                "root's directory name, so this checkout has to present the same "
                "top-level suite name as the composer's source root",
                file=sys.stderr,
            )
        if os.environ.get("RC_ALLOW_DRIFT") != "1":
            print(
                "refusing to execute — the composed item set is no longer executable "
                "here (DESIGN.md §3.3); set RC_ALLOW_DRIFT=1 to run the intersection",
                file=sys.stderr,
            )
            return 3

    executable = [item_id for item_id in requested if item_id in present]
    if not executable:
        print("nothing left to execute after the drift check", file=sys.stderr)
        return 3
    rc = robot.run(
        str(suite_root),
        test=executable,
        outputdir=str(out_dir),
        output="output.xml",
        log="NONE",  # drop these two to carry log.html/report.html home in the bundle
        report="NONE",
    )
    return 0 if rc <= 250 else rc  # test failures still produce a valid bundle


if __name__ == "__main__":
    raise SystemExit(main())
