"""Reference connectors: sources, runners, parsers, a store and observers.

They register through the same entry-point groups any third-party connector
uses, and they import **only** ``runcomposer.sdk`` from this package — the
same surface a connector outside this repository has. A guard test holds them
to it.
"""
