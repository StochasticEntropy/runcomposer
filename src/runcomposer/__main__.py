"""``python -m runcomposer``: the command line, the same entry point as the
``runcomposer`` script (exit codes included)."""

from runcomposer.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
