"""Resolving the taxonomy against the catalog (DESIGN.md §2, §9).

The filter of a written leaf is a single pattern, which is what keeps the format
hand-writable and also what hides a family of tags behind a single opaque
node: `regex:^Cart(V2)?$` renders as one row, and the tags it stands for have
no row of their own and cannot be picked. These tests hold the resolved tree
to the claim the docs make for it — every tag its own node, nothing dead, and
nothing left out.
"""

from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from runcomposer.cli import main as cli_main
from runcomposer.core.taxonomy import TaxonomyError, resolve_taxonomy, validate_taxonomy
from runcomposer.http import create_app
from runcomposer.sdk import Item, Service, load_config, parse_filter

from .conftest import write_config

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
        # Literals match case-insensitively (§3.1), so `Cart` as a literal
        # would select `CART` too and the node would promise one tag and
        # deliver two.
        items = [Item(id="a", tags=("Cart",)), Item(id="b", tags=("CART",))]
        tree = resolve({"taxonomy": [{"label": "Carts", "filter": "Cart"}]}, items)
        children = tree["taxonomy"][0]["children"]
        # Tag order is case-insensitive, ties broken by the exact spelling.
        assert [child["label"] for child in children] == ["CART", "Cart"]
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
        assert tree["taxonomy"][0]["label"] == "Payments"
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
        with pytest.raises(TaxonomyError, match=r"did you mean 'filter'\?"):
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
    return load_config(str(write_config(tmp_path)))


def taxonomy_config(tmp_path, text):
    """A config whose taxonomy file holds ``text``."""
    (tmp_path / "taxonomy.yaml").write_text(text, encoding="utf-8")
    return write_config(tmp_path, core={"taxonomy_file": "taxonomy.yaml"})


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
        preview = Service(config).preview({"tag_filter": tag_filter})
        assert len(preview.items) == expected, name
        assert preview.catalog_size == 60

    def test_the_wrong_glue_is_the_whole_corpus(self, config):
        """Stated separately because it is the point: the failure mode is a
        filter that reports no problem and removes nothing."""
        service = Service(config)
        everything = len(service.source.items())
        preview = service.preview(
            {"tag_filter": {"op": "OR", "items": [{"not": "Payments"}, {"not": "Cart"}]}}
        )
        assert len(preview.items) == everything == 60


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
        config_file = taxonomy_config(tmp_path, "taxonomy:\n  - {label: A, filter: Payments}\n")
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
        # …in the one error shape every endpoint answers with (§9).
        assert response.json()["code"] == "taxonomy-invalid"


class TestValidation:
    """§8: hand-written deployment data fails loudly, like an unknown plugin
    id — never a 200 with an empty tree."""

    GOOD: ClassVar[dict] = {
        "taxonomy": [
            {"label": "Areas", "children": [
                {"label": "Payments", "filter": "Payments"},
                {"label": "Checkout", "filter": "prefix:Checkout-"},
            ]},
            {"label": "Quarantine", "filter": "prefix:Quarantine-"},
        ]
    }

    def test_a_good_document_passes_unchanged(self):
        assert validate_taxonomy(self.GOOD) == self.GOOD

    @pytest.mark.parametrize(
        "document, expected",
        [
            (None, "is empty"),
            ([{"label": "x", "filter": "y"}], "nested under 'taxonomy:'"),
            ({"nodes": []}, "no top-level 'taxonomy' key"),
            ({"taxonomy": {"label": "x"}}, "must be a list of nodes"),
            ({"taxonomy": ["Payments"]}, "must be a node mapping"),
            ({"taxonomy": [{"filter": "Payments"}]}, "needs a non-empty 'label'"),
            ({"taxonomy": [{"label": "A", "filter": ["Payments", "Cart"]}]}, "single pattern string"),
            ({"taxonomy": [{"label": "A", "filter": "regex:^(unclosed"}]}, "invalid regular"),
            ({"taxonomy": [{"label": "A", "filter": "x", "children": {}}]}, "must be a list"),
            ({"taxonomy": [{"label": "A", "pattern": "Payments"}]}, "did you mean 'filter'?"),
            ({"taxonomy": [{"label": "A", "filter": "x"}, {"label": "A", "filter": "y"}]},
             "sibling labels must be distinct"),
        ],
    )
    def test_bad_documents_are_refused_with_an_actionable_message(self, document, expected):
        with pytest.raises(TaxonomyError, match=expected):
            validate_taxonomy(document, origin="taxonomy.yaml")

    def test_the_message_names_the_offending_node(self):
        document = {"taxonomy": [
            {"label": "Areas", "children": [
                {"label": "Payments", "filter": "Payments"},
                {"label": "Checkout", "tags": "Checkout-"},
            ]},
        ]}
        with pytest.raises(TaxonomyError) as excinfo:
            validate_taxonomy(document, origin="my-taxonomy.yaml")
        message = str(excinfo.value)
        assert "my-taxonomy.yaml" in message
        assert "taxonomy[0].children[1] ('Checkout')" in message
        assert "['tags']" in message

    def test_unknown_node_keys_are_tolerated_next_to_a_real_one(self):
        # §3 versioning stance: unknown fields inside a known section are
        # ignored — only a node that is neither clickable nor openable fails.
        validate_taxonomy({"taxonomy": [{"label": "A", "filter": "x", "color": "red"}]})

    def test_the_error_is_part_of_the_taxonomy_of_errors(self):
        """Typed and coded, so no transport classifies it by its message."""
        from runcomposer.sdk import RunComposerError

        with pytest.raises(RunComposerError) as excinfo:
            validate_taxonomy({"nodes": []})
        assert excinfo.value.code == "taxonomy-invalid"

    def test_bundled_demo_taxonomy_is_valid(self, config):
        assert Service(config).taxonomy()["taxonomy"][0]["label"] == "Areas"


class TestStartup:
    """Checked at startup and on every request, with the same validator."""

    def test_a_broken_taxonomy_refuses_the_boot(self, tmp_path):
        config_file = taxonomy_config(tmp_path, "nodes:\n  - {label: A}\n")
        with pytest.raises(TaxonomyError, match="no top-level 'taxonomy' key"):
            create_app(load_config(str(config_file)))

    def test_service_validate_is_what_refuses_it(self, tmp_path):
        """The embedding surface gets the same check as the server."""
        config_file = taxonomy_config(tmp_path, "taxonomy:\n  - {label: A, tags: Payments}\n")
        with Service(load_config(str(config_file))) as service, pytest.raises(
            TaxonomyError, match=r"taxonomy\[0\] \('A'\)"
        ):
            service.validate()

    def test_serve_exits_2_with_a_one_liner(self, tmp_path, capsys):
        config_file = taxonomy_config(tmp_path, "nodes:\n  - {label: A}\n")
        assert cli_main(["serve", "--config", str(config_file)]) == 2
        err = capsys.readouterr().err
        assert "no top-level 'taxonomy' key" in err and "Traceback" not in err

    def test_a_missing_file_says_where_it_looked(self, tmp_path):
        config_file = taxonomy_config(tmp_path, "taxonomy: []\n")
        (tmp_path / "taxonomy.yaml").unlink()
        with pytest.raises(TaxonomyError, match="cannot be read"):
            create_app(load_config(str(config_file)))

    def test_a_relative_taxonomy_file_is_found_from_any_working_directory(
        self, tmp_path, monkeypatch
    ):
        config_file = taxonomy_config(tmp_path, "taxonomy:\n  - {label: A, filter: Payments}\n")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        with Service(load_config(str(config_file))) as service:
            assert service.taxonomy() == {"taxonomy": [{"label": "A", "filter": "Payments"}]}

    def test_a_file_broken_after_boot_is_a_500_that_explains(self, tmp_path):
        config_file = taxonomy_config(tmp_path, "taxonomy:\n  - {label: A, filter: Payments}\n")
        client = TestClient(
            create_app(load_config(str(config_file))), raise_server_exceptions=False
        )
        assert client.get("/api/v1/taxonomy").status_code == 200
        # the file is re-read per request (docs/taxonomy.md), so it can rot
        # under a running server — a 200 with an empty tree is the one answer
        # that must not happen
        (tmp_path / "taxonomy.yaml").write_text("groups: []\n", encoding="utf-8")
        response = client.get("/api/v1/taxonomy")
        assert response.status_code == 500
        assert "no top-level 'taxonomy' key" in response.json()["detail"]

    def test_meta_says_whether_a_taxonomy_is_configured(self, tmp_path, config):
        assert TestClient(create_app(config)).get("/api/v1/meta").json()["taxonomy"] == {
            "configured": False
        }
        configured = taxonomy_config(tmp_path, "taxonomy:\n  - {label: A, filter: Payments}\n")
        body = TestClient(create_app(load_config(str(configured)))).get("/api/v1/meta").json()
        assert body["taxonomy"] == {"configured": True}


@pytest.fixture()
def in_tmp(tmp_path, monkeypatch):
    """The CLI without --config runs on built-in defaults relative to the
    working directory — never the repository."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestTheCli:
    def test_taxonomy_check_reports_what_resolution_reaches(self, in_tmp, capsys):
        assert cli_main(["taxonomy-check", "--warn-only"]) == 0
        out = capsys.readouterr().out
        assert "resolved tree" in out
        assert "selectable on their own" in out

    def test_tree_prints_the_resolved_tree_with_its_tag_nodes(self, in_tmp, capsys):
        assert cli_main(["taxonomy-check", "--tree"]) == 0
        out = capsys.readouterr().out
        assert "Cart" in out and "CartV2" in out
        assert "item(s)" in out

    def test_tree_respects_limit(self, in_tmp, capsys):
        assert cli_main(["taxonomy-check", "--tree", "--limit", "3"]) == 0
        out = capsys.readouterr().out
        assert "more (use --limit 0 for all)" in out

    def test_taxonomy_check_names_tags_no_leaf_claims(self, in_tmp, capsys):
        # The bundled taxonomy is a demo tree, not a complete one — so the
        # check has something to report, which is the point: drift between a
        # hand-written tree and a moving catalog is invisible otherwise.
        assert cli_main(["taxonomy-check"]) == 1
        out = capsys.readouterr().out
        assert "taxonomy leaf pattern(s) over 47 distinct catalog tag(s)" in out
        assert "tags no leaf claims" in out
        assert "Payments-Cards" in out
        assert "every taxonomy leaf matches at least one tag" in out
        assert list(in_tmp.iterdir()) == []  # a read-only check writes nothing

    def test_taxonomy_check_warn_only_still_reports(self, in_tmp, capsys):
        assert cli_main(["taxonomy-check", "--warn-only"]) == 0
        assert "tags no leaf claims" in capsys.readouterr().out

    def test_taxonomy_check_names_leaves_that_match_nothing(self, tmp_path, capsys):
        config_file = taxonomy_config(
            tmp_path,
            "taxonomy:\n"
            "  - label: Areas\n"
            "    children:\n"
            "      - label: Payments\n"
            "        filter: Payments\n"
            "      - label: Renamed away\n"
            "        filter: Billing\n",
        )
        assert cli_main(["taxonomy-check", "--config", str(config_file)]) == 1
        out = capsys.readouterr().out
        # A leaf whose tag was renamed stays clickable and selects nothing —
        # the failure mode nothing else surfaces.
        assert "leaves that match nothing" in out
        assert "Areas › Renamed away  →  Billing" in out

    def test_a_malformed_taxonomy_is_exit_2_with_a_one_liner(self, tmp_path, capsys):
        config_file = taxonomy_config(tmp_path, "taxonomy:\n  - {label: A, pattern: Payments}\n")
        assert cli_main(["taxonomy-check", "--config", str(config_file)]) == 2
        err = capsys.readouterr().err
        assert "did you mean 'filter'?" in err and "Traceback" not in err

    def test_doctor_reports_the_drift_of_a_configured_taxonomy(self, tmp_path, capsys):
        config_file = taxonomy_config(
            tmp_path, "taxonomy:\n  - {label: Payments, filter: Payments}\n"
        )
        assert cli_main(["doctor", "--config", str(config_file)]) == 1
        out = capsys.readouterr().out
        assert "46 tag(s) no leaf claims" in out and "runcomposer taxonomy-check" in out
