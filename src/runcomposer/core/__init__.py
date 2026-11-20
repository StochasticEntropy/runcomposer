"""Core: domain model, filter grammar, selection compile, run-spec handling,
lifecycle, ports.

The core knows only the vocabulary of DESIGN.md §2. It imports nothing from
the application layer or from any plugin and names no framework — guarded by
tests. Connector authors import from the SDK facade one level up, not from
here.
"""
