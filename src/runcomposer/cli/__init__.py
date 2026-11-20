"""The runcomposer command line (DESIGN.md §9; flag-level reference:
docs/cli.md). One module per command group; ``main`` is the entry point."""

from .main import main

__all__ = ["main"]
