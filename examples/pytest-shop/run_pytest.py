"""The pytest consumer command for runcomposer-exec (DESIGN.md §6.2c usage):

    runcomposer-exec spec.json --out results \\
        --command "python examples/pytest-shop/run_pytest.py {ids_file} {out_dir}"

Reads the materialized item ids (pytest nodeids) from the ids file, runs the
example suite on exactly those, and writes junit.xml into the output dir —
which the `junit-xml` ResultParser ingests.
"""

import os
import sys
from pathlib import Path

import pytest


def main() -> int:
    ids_file, out_dir = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    here = Path(__file__).parent.resolve()
    nodeids = [line.strip() for line in ids_file.read_text().splitlines() if line.strip()]
    out_dir.mkdir(parents=True, exist_ok=True)
    # Pin rootdir to this directory so junit classnames are stable
    # ("test_cart", not a path relative to whatever pyproject pytest found).
    os.chdir(here)
    rc = pytest.main(
        [*nodeids, f"--rootdir={here}", f"--junit-xml={out_dir / 'junit.xml'}",
         "-q", "-p", "no:cacheprovider"]
    )
    return 0 if rc in (0, 1) else rc  # test failures still produce a valid bundle


if __name__ == "__main__":
    raise SystemExit(main())
