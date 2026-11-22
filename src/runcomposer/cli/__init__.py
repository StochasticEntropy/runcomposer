"""The runcomposer command line (DESIGN.md §9; flag-level reference:
docs/cli.md). One module per command group; ``main`` is the entry point.

The entry point itself imports nothing but the standard library before it
reaches the command line: on a minimal install (``pip install --no-deps
runcomposer``, for a machine that only executes) a command that needs a
package that is not there says so in one line instead of a traceback."""

from __future__ import annotations

import sys

__all__ = ["main", "missing_package"]

#: What the command line needs beyond the standard library: import name →
#: the distribution that provides it.
_PACKAGES = {
    "yaml": "PyYAML",
    "jsonschema": "jsonschema",
    "fastapi": "fastapi",
    "starlette": "starlette",
    "uvicorn": "uvicorn",
    "pydantic": "pydantic",
}


def missing_package(exc: ImportError) -> str | None:
    """The one line to say when ``exc`` is a dependency of runcomposer that
    is not installed; ``None`` for any other import error."""
    top = (exc.name or "").split(".")[0]
    package = _PACKAGES.get(top)
    if package is None:
        return None
    return (
        f"error: this command needs the package {package!r}, which is not installed — a minimal "
        "install (pip install --no-deps runcomposer) only executes; install runcomposer with "
        "its dependencies (pip install runcomposer) to use the command line"
    )


try:
    from .main import main
except ImportError as _missing:
    _MESSAGE = missing_package(_missing)
    if _MESSAGE is None:
        raise

    def main(argv: list[str] | None = None) -> int:
        """The entry point of an install without the command line's
        packages: one line saying which is missing, exit 2."""
        print(_MESSAGE, file=sys.stderr)
        return 2
