"""runcomposer — tag-based test run composer & orchestrator.

The core's product is the run spec: a versioned, human-readable document
that anything can read, write, and understand (DESIGN.md §0).

Connector authors and embedders import from ``runcomposer.sdk``.
"""

# The single source of the version: packaging reads it from here
# (pyproject.toml, [tool.hatch.version]).
__version__ = "0.2.0"
