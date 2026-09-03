"""The UI ships EN and DE (DESIGN.md §10.1), so a label added in one locale
and forgotten in the other is a bug, not a style question.

The i18n helper falls back to printing the key itself when a message is
missing, which is quiet enough to survive a click-through and end up in a
release — this is the test that would have caught it.
"""

import json
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SOURCE_LOCALES = REPO / "ui" / "public" / "locales"
BUNDLED_LOCALES = REPO / "src" / "runcomposer" / "ui_dist" / "locales"


def _keys(node, prefix=""):
    """Every leaf path in a locale file, as dotted keys."""
    if isinstance(node, dict):
        return {key for name, child in node.items() for key in _keys(child, f"{prefix}{name}.")}
    return {prefix.rstrip(".")}


def _load(directory: Path) -> dict[str, dict]:
    return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))}


@pytest.mark.parametrize("directory", [SOURCE_LOCALES, BUNDLED_LOCALES], ids=["source", "bundled"])
def test_every_locale_carries_the_same_keys(directory: Path) -> None:
    locales = _load(directory)
    assert set(locales) == {"de", "en"}, f"expected en + de in {directory}"

    missing = {name: _keys(locales["en"]) - _keys(messages) for name, messages in locales.items()}
    extra = {name: _keys(messages) - _keys(locales["en"]) for name, messages in locales.items()}
    assert not any(missing.values()), f"keys missing from a locale: {missing}"
    assert not any(extra.values()), f"keys in a locale that EN does not have: {extra}"


@pytest.mark.parametrize("locale", ["de", "en"])
def test_no_message_is_left_empty(locale: str) -> None:
    def walk(node, path=""):
        if isinstance(node, dict):
            for name, child in node.items():
                walk(child, f"{path}{name}.")
            return
        assert isinstance(node, str) and node.strip(), f"{locale}: {path.rstrip('.')} is empty"

    walk(json.loads((SOURCE_LOCALES / f"{locale}.json").read_text(encoding="utf-8")))


def test_the_bundled_locales_match_the_source() -> None:
    """The wheel serves ui_dist; a rebuild is part of a UI change (CONTRIBUTING)."""
    assert _load(SOURCE_LOCALES) == _load(BUNDLED_LOCALES)
