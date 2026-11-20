"""`demo-corpus` TestSource — the bundled fictional web-shop catalog
(DESIGN.md §12), as a source of its own.

It is the `manifest` source reading a file that ships inside the package.
Being a separate, named plugin is what lets "no `sources:` configured" be
ordinary configuration (`sources: {demo-corpus: {}}`) instead of a special
case inside the manifest source or the config loader.
"""

from __future__ import annotations

from importlib import resources

from runcomposer.plugins.manifest_source import ManifestSource

__all__ = ["DemoCorpusSource"]


class DemoCorpusSource(ManifestSource):
    provider_id = "manifest"
    PATH_OPTIONS: tuple[str, ...] = ()

    def __init__(self) -> None:
        super().__init__(resources.files("runcomposer.demo") / "corpus.json")

    def fingerprint(self) -> str:
        return "bundled"
