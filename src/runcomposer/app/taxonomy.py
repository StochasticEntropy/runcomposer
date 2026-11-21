"""Reading the configured taxonomy file (DESIGN.md §2, §8): the file, or
the bundled demo taxonomy, read and validated on every call — the file can
change under a running server."""

from __future__ import annotations

from typing import Any

import yaml

from runcomposer.core.taxonomy import TaxonomyError, validate_taxonomy

from .config import Config

__all__ = ["read_taxonomy"]


def read_taxonomy(config: Config) -> dict[str, Any]:
    """Read and validate the configured taxonomy file (DESIGN.md §2, §8)
    — on every read, because the file can change under a running server."""
    source = config.taxonomy_source
    origin = str(source)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaxonomyError(
            f"{origin}: taxonomy file cannot be read ({exc.strerror or exc}) — "
            "core.taxonomy_file is resolved relative to the config file's directory"
        ) from None
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TaxonomyError(f"{origin}: taxonomy file is not valid YAML: {exc}") from None
    return validate_taxonomy(document, origin=origin)
