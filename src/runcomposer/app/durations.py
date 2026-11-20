"""Imported durations (DESIGN.md §6.2, §7): reading the file
``runcomposer history import`` takes.

A deployment that already knows how long its tests take — from a CI server's
reports, from an earlier tool — can start with a balanced plan instead of a
cold one. The file is deliberately plain:

- **CSV** with a header row naming an ``item`` column and a ``duration_ms``
  (or ``duration_s``) column; other columns are ignored::

      item,duration_ms
      Shop.Checkout.Card.Pay By Card,4200
      Shop.Cart.Core.Add Item,650

- **JSON**: ``{"durations": {"<item>": <milliseconds>, …}}``, or a list of
  ``{"item": …, "duration_ms": …}`` objects (``duration_s`` likewise).

``item`` is an item id or any native name the catalog resolves.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from runcomposer.core.errors import InvalidRequest

__all__ = ["read_durations_file"]

_NAME_COLUMNS = ("item", "item_id", "id", "name")


def _milliseconds(entry: dict[str, Any], where: str) -> float:
    try:
        if entry.get("duration_ms") not in (None, ""):
            return float(entry["duration_ms"])
        if entry.get("duration_s") not in (None, ""):
            return float(entry["duration_s"]) * 1000.0
    except (TypeError, ValueError):
        raise InvalidRequest(f"{where}: the duration is not a number") from None
    raise InvalidRequest(f"{where}: no 'duration_ms' (or 'duration_s')")


def _name(entry: dict[str, Any], where: str) -> str:
    for column in _NAME_COLUMNS:
        if entry.get(column):
            return str(entry[column])
    raise InvalidRequest(f"{where}: no 'item' naming the test")


def read_durations_file(path: str | Path) -> dict[str, float]:
    """``{name: milliseconds}`` from a CSV or JSON durations file. A name
    given twice keeps the mean of its values."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise InvalidRequest(f"{file}: cannot be read ({exc.strerror or exc})") from None
    collected: dict[str, list[float]] = {}

    def add(name: str, value: float, where: str) -> None:
        if value < 0:
            raise InvalidRequest(f"{where}: a duration cannot be negative")
        collected.setdefault(name, []).append(value)

    if file.suffix.lower() == ".json" or text.lstrip().startswith(("{", "[")):
        try:
            document = json.loads(text)
        except ValueError as exc:
            raise InvalidRequest(f"{file}: not valid JSON: {exc}") from None
        if isinstance(document, dict) and isinstance(document.get("durations"), (dict, list)):
            document = document["durations"]
        if isinstance(document, dict):
            for name, value in document.items():
                add(str(name), _milliseconds({"duration_ms": value}, f"{file}: {name!r}"), str(file))
        elif isinstance(document, list):
            for index, entry in enumerate(document):
                where = f"{file}: entry {index}"
                if not isinstance(entry, dict):
                    raise InvalidRequest(f"{where}: must be an object")
                add(_name(entry, where), _milliseconds(entry, where), where)
        else:
            raise InvalidRequest(
                f"{file}: expected {{\"durations\": {{item: milliseconds}}}} or a list of "
                "{item, duration_ms} objects"
            )
    else:
        reader = csv.DictReader(text.splitlines())
        if not reader.fieldnames or not set(reader.fieldnames) & set(_NAME_COLUMNS):
            raise InvalidRequest(
                f"{file}: the first row must name the columns — 'item' and 'duration_ms' "
                "(or 'duration_s')"
            )
        for row in reader:
            where = f"{file}: line {reader.line_num}"
            add(_name(row, where), _milliseconds(row, where), where)
    return {name: sum(values) / len(values) for name, values in collected.items()}
