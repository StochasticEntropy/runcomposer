"""Selection matrix test (DESIGN.md §11): a synthetic corpus proving
AST→item-set semantics exactly, against an independently written oracle.

The oracle evaluates filters with set algebra over the whole corpus —
deliberately a different implementation strategy than the core's
per-item AST walk — so the two only agree if the semantics agree.
"""

import re

import pytest

from runcomposer.core.model import Item
from runcomposer.core.selection import Selection, SelectionError


def build_corpus() -> list[Item]:
    items = []
    n = 0
    for area in ("Alpha", "Beta", "Gamma", "delta"):  # one lowercase area on purpose
        for group in ("One", "Two", "Three"):
            for i in range(1, 5):
                tags = [area, f"{area}-{group}", f"Sprint-{i}"]
                if n % 2 == 0:
                    tags.append("Smoke")
                else:
                    tags.append("Regression")
                if n % 7 == 0:
                    tags.append("Quarantine-Flaky")
                if n % 11 == 0:
                    tags.append("MIXED-case-TAG")
                items.append(Item(id=f"{area}.{group}.T{i:03d}", tags=tuple(tags)))
                n += 1
    assert len(items) == 48
    return items


CORPUS = build_corpus()
ALL_IDS = {item.id for item in CORPUS}


def oracle(node) -> set[str]:
    """Independent set-algebra evaluator for the filter grammar."""
    if isinstance(node, str):
        if node.startswith("regex:"):
            pattern = re.compile(node[len("regex:"):])
            return {i.id for i in CORPUS if any(pattern.search(t) for t in i.tags)}
        if node.startswith("prefix:"):
            pattern = re.compile("^" + node[len("prefix:"):])
            return {i.id for i in CORPUS if any(pattern.search(t) for t in i.tags)}
        return {i.id for i in CORPUS if any(t.casefold() == node.casefold() for t in i.tags)}
    if set(node) == {"not"}:
        return ALL_IDS - oracle(node["not"])
    child_sets = [oracle(child) for child in node["items"]]
    if node["op"] == "AND":
        return set.intersection(*child_sets)
    return set.union(*child_sets)


FILTER_MATRIX = [
    "Alpha",
    "alpha",  # case-insensitive literal
    "MIXED-CASE-tag",
    "Smoke",
    "prefix:Alpha-",
    "prefix:Sprint-",
    "regex:^Sprint-[12]$",
    "regex:Two",
    {"not": "Quarantine-Flaky"},
    {"op": "AND", "items": ["Alpha", "Smoke"]},
    {"op": "OR", "items": ["Alpha", "Beta"]},
    {"op": "AND", "items": ["Regression", {"not": "prefix:Quarantine-"}]},
    {"op": "OR", "items": [{"op": "AND", "items": ["Gamma", "Smoke"]}, "prefix:delta-"]},
    {
        "op": "AND",
        "items": [
            {"op": "OR", "items": ["Alpha", "Beta", "Gamma"]},
            {"not": {"op": "OR", "items": ["Sprint-3", "Sprint-4"]}},
            "Regression",
        ],
    },
    {"not": {"not": "Alpha"}},
]


@pytest.mark.parametrize("filter_data", FILTER_MATRIX, ids=str)
def test_compile_matches_oracle(filter_data):
    compiled = Selection.from_data({"tag_filter": filter_data}).compile(CORPUS)
    assert {item.id for item in compiled} == oracle(filter_data)


def test_matrix_is_not_vacuous():
    # Guard against a matrix where everything matches nothing/everything.
    sizes = {len(oracle(f)) for f in FILTER_MATRIX}
    assert any(0 < len(oracle(f)) < len(CORPUS) for f in FILTER_MATRIX)
    assert len(sizes) > 3


def test_compile_preserves_catalog_order():
    compiled = Selection.from_data({"tag_filter": "Smoke"}).compile(CORPUS)
    positions = {item.id: n for n, item in enumerate(CORPUS)}
    assert [item.id for item in compiled] == sorted((i.id for i in compiled), key=positions.get)


class TestExplicitPicks:
    def test_item_ids_alone(self):
        picks = [CORPUS[3].id, CORPUS[40].id]
        compiled = Selection.from_data({"item_ids": picks}).compile(CORPUS)
        assert [item.id for item in compiled] == sorted(picks, key=[i.id for i in CORPUS].index)

    def test_filter_and_picks_intersect(self):
        # Fixed AND (DESIGN.md §3.1): picks narrow the filter result.
        smoke_ids = [item.id for item in CORPUS if "Smoke" in item.tags]
        non_smoke = next(item.id for item in CORPUS if "Smoke" not in item.tags)
        picks = smoke_ids[:2] + [non_smoke]
        compiled = Selection.from_data({"tag_filter": "Smoke", "item_ids": picks}).compile(CORPUS)
        assert {item.id for item in compiled} == set(smoke_ids[:2])

    def test_unknown_item_id_is_a_compile_error(self):
        with pytest.raises(SelectionError, match="No.Such.Item"):
            Selection.from_data({"item_ids": ["No.Such.Item"]}).compile(CORPUS)

    def test_empty_selection_is_an_error(self):
        with pytest.raises(SelectionError):
            Selection.from_data({})
