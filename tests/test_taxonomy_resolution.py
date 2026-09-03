"""Resolving the taxonomy against the catalog (DESIGN.md §2, §9).

A written leaf carries exactly one pattern, which is what keeps the format
hand-writable and also what hides a family of tags behind a single opaque
node: `regex:^Cart(V2)?$` renders as one row, and the tags it stands for have
no row of their own and cannot be picked. These tests hold the resolved tree
to the claim the docs make for it — every tag its own node, nothing dead, and
nothing left out.
"""

import pytest
from fastapi.testclient import TestClient

from runcomposer.api import create_app
from runcomposer.cli import main as cli_main
from runcomposer.config import Config, load_config
from runcomposer.core.filter import parse_filter
from runcomposer.core.model import Item
from runcomposer.core.taxonomy import TaxonomyError, resolve_taxonomy, validate_taxonomy
from runcomposer.service import Service

ITEMS = [
    Item(id="t1", tags=("Cart", "Smoke", "Sprint-12")),
    Item(id="t2", tags=("CartV2", "Regression")),
    Item(id="t3", tags=("Payments", "Smoke")),
    Item(id="t4", tags=("Loose",)),
]


def walk(nodes):
    for node in nodes:
        yield node
        yield from walk(node.get("children") or [])


def by_label(nodes, label):
    for node in walk(nodes):
        if node["label"] == label:
            return node
    raise AssertionError(f"no node labelled {label!r} in {[n['label'] for n in walk(nodes)]}")


def resolve(document, items=ITEMS):
    return resolve_taxonomy(document, items)


class TestExpansion:
    """The point of the exercise: a pattern's tags become their own nodes."""

    def test_a_family_pattern_gains_one_node_per_matching_tag(self):
        tree = resolve({"taxonomy": [{"label": "Cart", "filter": "regex:^Cart(V2)?$"}]})
        cart = tree["taxonomy"][0]
        assert cart["filter"] == "regex:^Cart(V2)?$"  # the written node is untouched
        assert [child["label"] for child in cart["children"]] == ["Cart", "CartV2"]
        assert [child["filter"] for child in cart["children"]] == ["Cart", "CartV2"]

    def test_every_tag_node_selects_exactly_its_own_tag(self):
        tree = resolve({"taxonomy": [{"label": "Cart", "filter": "regex:^Cart(V2)?$"}]})
        tags = {tag for item in ITEMS for tag in item.tags}
        for node in walk(tree["taxonomy"]):
            if node["origin"] != "tag":
                continue
            matched = [tag for tag in tags if parse_filter(node["filter"]).matches((tag,))]
            assert matched == [node["label"]]

    def test_a_pattern_matching_one_tag_stays_a_plain_leaf(self):
        # The node already *is* that tag's node; a single identical child
        # would be noise, not navigation.
        tree = resolve({"taxonomy": [{"label": "Payments", "filter": "Payments"}]})
        assert "children" not in tree["taxonomy"][0]

    def test_a_tag_a_descendant_claims_is_not_repeated_on_the_parent(self):
        tree = resolve(
            {
                "taxonomy": [
                    {
                        "label": "Cart",
                        "filter": "regex:^Cart(V2)?$",
                        "children": [{"label": "Cart v2 only", "filter": "CartV2"}],
                    }
                ]
            }
        )
        labels = [child["label"] for child in tree["taxonomy"][0]["children"]]
        assert labels == ["Cart v2 only", "Cart"]  # written children first, then what is left
        assert [node["label"] for node in walk(tree["taxonomy"])].count("CartV2") == 0

    def test_a_tag_spelled_like_a_grammar_prefix_is_escaped_not_parsed(self):
        items = [Item(id="t", tags=("regex:^oops$", "plain"))]
        tree = resolve({"taxonomy": [{"label": "All", "filter": "regex:."}]}, items)
        node = by_label(tree["taxonomy"], "regex:^oops$")
        assert node["filter"] == "regex:^regex:\\^oops\\$$"
        assert parse_filter(node["filter"]).matches(("regex:^oops$",))
        assert not parse_filter(node["filter"]).matches(("oops",))

    def test_case_variant_tags_get_a_node_each_that_means_itself(self):
        # Literals match case-insensitively (§3.1), so `Adapter` as a literal
        # would select `ADAPTER` too and the node would promise one tag and
        # deliver two.
        items = [Item(id="a", tags=("Adapter",)), Item(id="b", tags=("ADAPTER",))]
        tree = resolve({"taxonomy": [{"label": "Adapters", "filter": "Adapter"}]}, items)
        children = tree["taxonomy"][0]["children"]
        # Tag order is case-insensitive, ties broken by the exact spelling.
        assert [child["label"] for child in children] == ["ADAPTER", "Adapter"]
        for child in children:
            pattern = parse_filter(child["filter"])
            assert pattern.matches((child["label"],))
            assert not pattern.matches((child["label"].swapcase(),))


class TestCollapsing:
    """A leaf that clicks and selects nothing is worse than no leaf."""

    def test_a_node_matching_nothing_collapses_away(self):
        tree = resolve(
            {
                "taxonomy": [
                    {"label": "Payments", "filter": "Payments"},
                    {"label": "Renamed", "filter": "NoSuchTag"},
                ]
            }
        )
        assert [node["label"] for node in tree["taxonomy"]] == ["Payments", "Unassigned tags"]
        assert tree["resolved"]["nodes_dropped"] == 1

    def test_a_group_whose_children_all_collapse_goes_with_them(self):
        tree = resolve(
            {
                "taxonomy": [
                    {"label": "Gone", "children": [{"label": "Also gone", "filter": "Nope"}]},
                    {"label": "Payments", "filter": "Payments"},
                ]
            }
        )
        assert [node["label"] for node in tree["taxonomy"]][0] == "Payments"
        assert tree["resolved"]["nodes_dropped"] == 2

    def test_ids_are_file_positions_so_a_collapse_does_not_renumber_the_rest(self):
        document = {
            "taxonomy": [
                {
                    "label": "Areas",
                    "children": [
                        {"label": "Renamed", "filter": "NoSuchTag"},
                        {"label": "Payments", "filter": "Payments"},
                    ],
                }
            ]
        }
        tree = resolve(document)
        assert by_label(tree["taxonomy"], "Payments")["id"] == "taxonomy[0].children[1]"


class TestUnassigned:
    """Tags nothing claims are reachable, or the tree hides the corpus."""

    def test_unclaimed_tags_land_in_one_synthetic_node(self):
        tree = resolve({"taxonomy": [{"label": "Payments", "filter": "Payments"}]})
        node = tree["taxonomy"][-1]
        assert node["origin"] == "unassigned"
        assert [child["label"] for child in node["children"]] == [
            "Cart", "CartV2", "Loose", "Regression", "Smoke", "Sprint-12",
        ]
        assert tree["resolved"]["tags_unassigned"] == 6

    def test_no_synthetic_node_when_every_tag_is_claimed(self):
        tree = resolve({"taxonomy": [{"label": "All", "filter": "regex:."}]})
        assert all(node["origin"] != "unassigned" for node in tree["taxonomy"])
        assert tree["resolved"]["tags_unassigned"] == 0

    def test_its_label_is_uniqued_rather_than_dropped_on_a_collision(self):
        tree = resolve(
            {"taxonomy": [{"label": "Unassigned tags", "filter": "Payments"}]}
        )
        labels = [node["label"] for node in tree["taxonomy"]]
        assert labels == ["Unassigned tags", "Unassigned tags (2)"]
        assert tree["taxonomy"][1]["origin"] == "unassigned"


class TestCounts:
    def test_counts_roll_the_whole_subtree_up(self):
        tree = resolve(
            {
                "taxonomy": [
                    {
                        "label": "Areas",
                        "children": [
                            {"label": "Cart", "filter": "regex:^Cart(V2)?$"},
                            {"label": "Payments", "filter": "Payments"},
                        ],
                    }
                ]
            }
        )
        areas = tree["taxonomy"][0]
        assert areas["tag_count"] == 3  # Cart, CartV2, Payments
        assert areas["item_count"] == 3  # t1, t2, t3
        assert by_label(tree["taxonomy"], "Cart")["item_count"] == 2

    def test_an_item_carrying_two_tags_of_one_node_is_counted_once(self):
        items = [Item(id="only", tags=("Smoke", "Regression"))]
        tree = resolve({"taxonomy": [{"label": "Suites", "filter": "regex:^(Smoke|Regression)$"}]}, items)
        assert tree["taxonomy"][0]["item_count"] == 1
        assert tree["taxonomy"][0]["tag_count"] == 2

    def test_the_summary_reports_what_the_resolution_reached(self):
        summary = resolve({"taxonomy": [{"label": "Payments", "filter": "Payments"}]})["resolved"]
        assert summary == {
            "tags_total": 7,
            "tags_claimed": 1,          # only 'Payments' has a written node
            "tags_selectable": 7,       # …but every tag has a node in the result
            "tags_unassigned": 6,
            "items_total": 4,
            "items_claimed": 1,
            "nodes_written": 1,
            "nodes_dropped": 0,
            "nodes_total": 8,           # the leaf, the unassigned node, 6 tags
        }


class TestTheResolvedTreeIsATaxonomyDocument:
    """Same three-key grammar in, same grammar out — so the tree's consumer
    needs no second vocabulary, and the format's own validator can say so."""

    def test_the_result_validates_as_a_taxonomy_document(self):
        tree = resolve(
            {
                "taxonomy": [
                    {"label": "Cart", "filter": "regex:^Cart(V2)?$"},
                    {"label": "Suites", "children": [{"label": "Smoke", "filter": "Smoke"}]},
                ]
            }
        )
        assert validate_taxonomy({"taxonomy": tree["taxonomy"]})

    def test_a_tag_colliding_with_a_written_sibling_label_keeps_both(self):
        tree = resolve(
            {
                "taxonomy": [
                    {
                        "label": "Cart",
                        "filter": "regex:^Cart(V2)?$",
                        # a written child named exactly like one of the tags
                        "children": [{"label": "CartV2", "filter": "Smoke"}],
                    }
                ]
            }
        )
        labels = [child["label"] for child in tree["taxonomy"][0]["children"]]
        assert labels == ["CartV2", "Cart", "CartV2 (2)"]
        assert validate_taxonomy({"taxonomy": tree["taxonomy"]})

    def test_a_malformed_document_is_refused_before_anything_is_resolved(self):
        with pytest.raises(TaxonomyError, match="did you mean 'filter'?"):
            resolve({"taxonomy": [{"label": "A", "pattern": "Payments"}]})


class TestResolutionFollowsTheCatalog:
    def test_the_same_document_resolves_differently_against_a_different_catalog(self):
        document = {"taxonomy": [{"label": "Cart", "filter": "regex:^Cart(V2)?$"}]}
        before = resolve(document, [Item(id="a", tags=("Cart",))])
        after = resolve(document, [Item(id="a", tags=("Cart",)), Item(id="b", tags=("CartV2",))])
        assert "children" not in before["taxonomy"][0]
        assert [c["label"] for c in after["taxonomy"][0]["children"]] == ["Cart", "CartV2"]

    def test_a_taxonomy_over_an_empty_catalog_resolves_to_nothing_rather_than_to_dead_leaves(self):
        tree = resolve({"taxonomy": [{"label": "Cart", "filter": "Cart"}]}, [])
        assert tree["taxonomy"] == []
        assert tree["resolved"]["tags_total"] == 0


class TestTheTagUniverse:
    """`tags` is served beside the tree because it is NOT derivable from it."""

    def test_a_tag_reachable_only_through_a_regex_is_in_tags_and_in_no_node(self):
        # A written leaf whose pattern resolves to exactly ONE tag already *is*
        # that tag's node and gets no tag child — so "CartV2", reachable only
        # through this regex, is spelled nowhere in the nodes. A client
        # deriving completion candidates from the tree would silently miss it.
        document = {"taxonomy": [{"label": "Second cart", "filter": "regex:^Cart(V2)$"}]}
        resolved = resolve_taxonomy(document, ITEMS)
        labels = {node["label"] for node in walk(resolved["taxonomy"])}
        assert "CartV2" not in labels
        assert "CartV2" in resolved["tags"]

    def test_tags_holds_the_whole_catalog_even_where_the_tree_reaches_nothing(self):
        resolved = resolve_taxonomy({"taxonomy": [{"label": "Nope", "filter": "Nothing"}]}, ITEMS)
        assert resolved["tags"] == [
            "Cart",
            "CartV2",
            "Loose",
            "Payments",
            "Regression",
            "Smoke",
            "Sprint-12",
        ]

    def test_tags_is_the_same_order_the_tree_uses(self):
        resolved = resolve_taxonomy({"taxonomy": [{"label": "All", "filter": "regex:."}]}, ITEMS)
        under = [child["label"] for child in resolved["taxonomy"][0]["children"]]
        assert under == resolved["tags"]


# -- the surfaces ---------------------------------------------------------------


@pytest.fixture()
def config(tmp_path):
    """Default config: the bundled demo corpus and the bundled demo taxonomy."""
    return Config(data={"store": {"sqlite": {"path": str(tmp_path / "tx.db")}}})


class TestTheDemoWorldResolves:
    """The shipped taxonomy over the shipped corpus (DESIGN.md §12) — the
    capability demonstrated rather than asserted."""

    def test_every_demo_tag_becomes_individually_selectable(self, config):
        resolved = Service(config).resolved_taxonomy()
        summary = resolved["resolved"]
        assert summary["tags_total"] == summary["tags_selectable"]
        assert summary["nodes_total"] > summary["nodes_written"]

    def test_the_written_tree_leaves_most_demo_tags_unselectable(self, config):
        """The gap this closes, measured on the shipped corpus — the numbers
        the CHANGELOG quotes, so they cannot rot unnoticed."""
        service = Service(config)
        tags = {tag for item in service.source.items() for tag in item.tags}

        def selectable_alone(tree):
            return {
                matched[0]
                for node in walk(tree)
                if node.get("filter")
                for matched in [[t for t in tags if parse_filter(node["filter"]).matches((t,))]]
                if len(matched) == 1
            }

        assert len(tags) == 47
        assert len(selectable_alone(service.taxonomy()["taxonomy"])) == 9
        resolved = service.resolved_taxonomy()
        assert len(selectable_alone(resolved["taxonomy"])) == 47
        assert resolved["resolved"]["tags_selectable"] == 47

    def test_ticket_and_area_tags_the_written_tree_never_mentions_are_reachable(self, config):
        nodes = Service(config).resolved_taxonomy()["taxonomy"]
        labels = {node["label"] for node in walk(nodes)}
        assert "Payments-Cards" in labels  # matched by no written leaf
        assert any(label.startswith("SHOP-") for label in labels)


class TestExcludingASelection:
    """The numbers docs/taxonomy.md quotes for the de Morgan table.

    The UI's picker writes an exclusion as the AND of the negations, which is
    the same proposition as negating the group. Getting the glue backwards is
    the one mistake here that is silent AND plausible-looking: it produces a
    large, believable count instead of an error. These are the four readings on
    the shipped corpus, so the table in the docs cannot rot unnoticed.
    """

    @pytest.mark.parametrize(
        "name, tag_filter, expected",
        [
            ("either", {"op": "OR", "items": ["Payments", "Cart"]}, 22),
            ("neither", {"not": {"op": "OR", "items": ["Payments", "Cart"]}}, 38),
            (
                "neither, written as the AND of the negations — what the picker writes",
                {"op": "AND", "items": [{"not": "Payments"}, {"not": "Cart"}]},
                38,
            ),
            (
                "the wrong glue: an exclusion that excludes nothing",
                {"op": "OR", "items": [{"not": "Payments"}, {"not": "Cart"}]},
                60,
            ),
        ],
    )
    def test_the_readings_of_excluding_two_tags(self, config, name, tag_filter, expected):
        items, _ = Service(config).preview({"tag_filter": tag_filter})
        assert len(items) == expected, name

    def test_the_wrong_glue_is_the_whole_corpus(self, config):
        """Stated separately because it is the point: the failure mode is a
        filter that reports no problem and removes nothing."""
        service = Service(config)
        everything = len(service.source.items())
        items, _ = service.preview(
            {"tag_filter": {"op": "OR", "items": [{"not": "Payments"}, {"not": "Cart"}]}}
        )
        assert len(items) == everything == 60


class TestTheEndpoint:
    def test_the_default_response_is_unchanged(self, config):
        client = TestClient(create_app(config))
        body = client.get("/api/v1/taxonomy").json()
        assert body == Service(config).taxonomy()
        assert "resolved" not in body
        assert all("id" not in node for node in walk(body["taxonomy"]))

    def test_resolve_true_serves_the_resolved_tree_and_a_summary(self, config):
        client = TestClient(create_app(config))
        body = client.get("/api/v1/taxonomy?resolve=true").json()
        assert body["resolved"]["tags_selectable"] == body["resolved"]["tags_total"]
        assert all("id" in node for node in walk(body["taxonomy"]))

    def test_resolve_true_also_serves_the_catalog_tags(self, config):
        """The UI offers tag completion in the filter's value field, and the
        tags have to come from the same resolution the tree does."""
        service = Service(config)
        body = TestClient(create_app(config)).get("/api/v1/taxonomy?resolve=true").json()
        assert body["tags"] == sorted(
            {tag for item in service.source.items() for tag in item.tags},
            key=lambda tag: (tag.casefold(), tag),
        )
        assert len(body["tags"]) == body["resolved"]["tags_total"]

    def test_the_default_response_carries_no_tag_list(self, config):
        """`?resolve=true` is the opt-in; the published shape is untouched."""
        body = TestClient(create_app(config)).get("/api/v1/taxonomy").json()
        assert "tags" not in body

    def test_a_broken_file_still_names_the_node_when_resolving(self, tmp_path):
        (tmp_path / "taxonomy.yaml").write_text(
            "taxonomy:\n  - {label: A, filter: Payments}\n", encoding="utf-8"
        )
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "core:\n  taxonomy_file: taxonomy.yaml\n  ingestion: { inbox: null }\n"
            f"store:\n  sqlite: {{ path: {tmp_path / 'tx.db'} }}\n",
            encoding="utf-8",
        )
        client = TestClient(
            create_app(load_config(str(config_file))), raise_server_exceptions=False
        )
        assert client.get("/api/v1/taxonomy?resolve=true").status_code == 200
        (tmp_path / "taxonomy.yaml").write_text(
            "taxonomy:\n  - {label: A, pattern: Payments}\n", encoding="utf-8"
        )
        response = client.get("/api/v1/taxonomy?resolve=true")
        assert response.status_code == 500
        assert "did you mean 'filter'?" in response.json()["detail"]


class TestTheCli:
    def test_taxonomy_check_reports_what_resolution_reaches(self, capsys):
        assert cli_main(["taxonomy-check", "--warn-only"]) == 0
        out = capsys.readouterr().out
        assert "resolved tree" in out
        assert "selectable on their own" in out

    def test_tree_prints_the_resolved_tree_with_its_tag_nodes(self, capsys):
        assert cli_main(["taxonomy-check", "--tree"]) == 0
        out = capsys.readouterr().out
        assert "Cart" in out and "CartV2" in out
        assert "item(s)" in out

    def test_tree_respects_limit(self, capsys):
        assert cli_main(["taxonomy-check", "--tree", "--limit", "3"]) == 0
        out = capsys.readouterr().out
        assert "more (use --limit 0 for all)" in out
