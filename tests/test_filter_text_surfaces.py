"""A filter given as text wherever a filter is taken as a string: the
command line's filter argument (`compile`, `spec`, `plan`, `run`) and a
selection's ``tag_filter`` over HTTP and in ``Service`` calls. The text is
read once, where the selection is composed; a run spec stores the AST."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from runcomposer.cli.main import main
from runcomposer.core.filter import parse_filter, quote_pattern
from runcomposer.core.filtertext import parse_filter_text
from runcomposer.sdk import Service, load_config

from .conftest import write_config

API = "/api/v1"


def _ids(service: Service, ast: Any) -> list[str]:
    """What the AST selects from the catalog, in catalog order."""
    node = parse_filter(ast)
    return [item.id for item in service.catalog.items() if node.matches(item.tags)]


class Cli:
    def __init__(self, config: Path, capsys: Any):
        self.config = config
        self._capsys = capsys

    def __call__(self, *argv: str) -> tuple[int, str, str]:
        try:
            code = main([*argv, "--config", str(self.config)])
        except SystemExit as exit_:
            code = exit_.code if isinstance(exit_.code, int) else 1
        captured = self._capsys.readouterr()
        assert "Traceback" not in captured.err, captured.err
        return code, captured.out, captured.err

    def service(self) -> Service:
        return Service(load_config(str(self.config)))


@pytest.fixture()
def cli(tmp_path: Path, capsys) -> Cli:
    return Cli(write_config(tmp_path), capsys)


@pytest.fixture()
def oddly_tagged(tmp_path: Path) -> dict[str, Any]:
    """A corpus whose tags need quoting in filter text: a space, parentheses,
    and a tag that is literally spelled like an expression."""
    items = [
        {"id": "Shop.Cart.V2.T001", "tags": ["Cart", "Cart V2"]},
        {"id": "Shop.Checkout.Gift.T001", "tags": ["Checkout", "Checkout (Gift)"]},
        {"id": "Shop.Auth.Cart.T001", "tags": ["Auth OR Cart"]},
        {"id": "Shop.Auth.Login.T001", "tags": ["Auth"]},
    ]
    (tmp_path / "corpus.json").write_text(json.dumps({"items": items}), encoding="utf-8")
    return {"sources": {"manifest": {"path": "corpus.json"}}}


# -- the command line -----------------------------------------------------------------


class TestCommandLine:
    def test_compile_reads_an_or_as_the_union(self, cli):
        code, out, _err = cli("compile", "Auth OR Cart", "--json")
        assert code == 0
        selected = [item["id"] for item in json.loads(out)["items"]]
        with cli.service() as service:
            union = _ids(service, {"op": "OR", "items": ["Auth", "Cart"]})
        assert selected == union and len(union) == 17

    def test_compile_reads_and_not(self, cli):
        with cli.service() as service:
            for text, ast in (
                ("Checkout AND NOT Payments-Cards", {"op": "AND", "items": ["Checkout", {"not": "Payments-Cards"}]}),
                ("Payments AND NOT Payments-Cards", {"op": "AND", "items": ["Payments", {"not": "Payments-Cards"}]}),
                ("(Auth OR Cart) AND Smoke", {"op": "AND", "items": [{"op": "OR", "items": ["Auth", "Cart"]}, "Smoke"]}),
            ):
                code, out, _err = cli("compile", text, "--json")
                assert code == 0
                assert [item["id"] for item in json.loads(out)["items"]] == _ids(service, ast), text
        assert len(_ids(service, {"op": "AND", "items": ["Payments", {"not": "Payments-Cards"}]})) == 11

    def test_a_quoted_tag_is_one_tag(self, tmp_path, capsys, oddly_tagged):
        cli = Cli(write_config(tmp_path, **oddly_tagged), capsys)
        for text, expected in (
            ('"Cart V2" OR "Checkout (Gift)"', ["Shop.Cart.V2.T001", "Shop.Checkout.Gift.T001"]),
            ('"Auth OR Cart"', ["Shop.Auth.Cart.T001"]),
            ("Auth OR Cart", ["Shop.Cart.V2.T001", "Shop.Auth.Login.T001"]),
        ):
            code, out, _err = cli("compile", text, "--json")
            assert code == 0
            assert [item["id"] for item in json.loads(out)["items"]] == expected, text

    def test_unreadable_text_is_a_usage_error_that_marks_the_column(self, cli):
        code, out, err = cli("compile", "Auth OR (Cart")
        assert code == 2 and out == ""
        assert err.splitlines() == [
            "error: cannot read the filter text at column 9: the '(' opened here is not closed "
            "— expected AND, OR or ')'",
            "  Auth OR (Cart",
            "          ^",
        ]
        code, _out, err = cli("run", "Auth AND", "--runner", "demo")
        assert code == 2 and "column 9" in err and "the text ends where a pattern" in err

    def test_the_spec_stores_the_ast_never_the_text(self, cli):
        code, out, _err = cli("spec", "Auth OR Cart", "--dry-run", "--format", "json")
        assert code == 0
        selection = json.loads(out)["selection"]
        assert selection["tag_filter"] == {"op": "OR", "items": ["Auth", "Cart"]}
        assert selection["materialized"]["count"] == 17

    def test_plan_and_run_take_filter_text(self, cli):
        code, out, _err = cli("plan", "Auth-SSO OR Cart-V2", "--runner", "demo", "--json")
        assert code == 0 and json.loads(out)["item_count"] == 3 + 4
        code, out, _err = cli("run", "Auth-SSO OR Cart-V2", "--runner", "demo", "--json")
        run = json.loads(out)
        assert code in (0, 1) and run["planned_count"] == 7
        assert run["selection"]["tag_filter"] == {"op": "OR", "items": ["Auth-SSO", "Cart-V2"]}

    def test_a_yaml_ast_is_still_an_ast(self, cli):
        with cli.service() as service:
            for argument, ast in (
                ("{op: OR, items: [Auth, Cart]}", {"op": "OR", "items": ["Auth", "Cart"]}),
                ('{"not": "Regression"}', {"not": "Regression"}),
                ("not: Regression", {"not": "Regression"}),
                ("op: AND\nitems: [Auth, Smoke]", {"op": "AND", "items": ["Auth", "Smoke"]}),
            ):
                code, out, _err = cli("compile", argument, "--json")
                assert code == 0
                assert [item["id"] for item in json.loads(out)["items"]] == _ids(service, ast), argument

    def test_a_single_pattern_is_one_word_of_the_grammar_and_anything_else_must_parse(self, cli):
        with cli.service() as service:
            for argument in ("Payments", "prefix:Checkout-", "regex:^Sprint-1[23]$"):
                code, out, _err = cli("compile", argument, "--json")
                assert code == 0
                assert [item["id"] for item in json.loads(out)["items"]] == _ids(service, argument)
                assert json.loads(out)["warnings"] == []
            for pattern in ("regex:^Cart(V2)?$", "regex:(?i)^cart-"):
                code, out, _err = cli("compile", quote_pattern(pattern), "--json")
                assert code == 0
                assert [item["id"] for item in json.loads(out)["items"]] == _ids(service, pattern)
        code, _out, err = cli("compile", "regex:^Cart(V2)?$")
        assert code == 2
        assert "error: cannot read the filter text at column 1: the pattern 'regex:^Cart' runs into '('" in err
        assert err.rstrip().endswith("regex:^Cart(V2)?$\n  ^")
        code, _out, err = cli("compile", "Payments Smoke")
        assert code == 2 and "at column 10: 'Smoke' where AND, OR or the end of the text was expected" in err


# -- over HTTP --------------------------------------------------------------------------


class TestHttp:
    def test_compile_plan_and_runs_read_a_string_filter_as_text(self, make_client):
        client, service = make_client()
        union = _ids(service, {"op": "OR", "items": ["Auth", "Cart"]})

        compiled = client.post(f"{API}/selection/compile", json={"tag_filter": "Auth OR Cart"})
        assert compiled.status_code == 200 and compiled.json()["count"] == len(union) == 17

        plan = client.post(f"{API}/plan", json={"selection": {"tag_filter": "Auth OR Cart"}, "runner": "demo"})
        assert plan.status_code == 200, plan.text
        assert plan.json()["item_count"] == 17

        created = client.post(f"{API}/runs", json={"selection": {"tag_filter": "Checkout AND NOT Payments-Cards"}})
        assert created.status_code == 201, created.text
        spec = service.require_spec(created.json()["run"]["id"])
        assert spec["selection"]["tag_filter"] == {"op": "AND", "items": ["Checkout", {"not": "Payments-Cards"}]}
        assert spec["selection"]["materialized"]["item_ids"] == _ids(
            service, {"op": "AND", "items": ["Checkout", {"not": "Payments-Cards"}]}
        )

        preview = client.post(f"{API}/selection/spec-preview", json={"selection": {"tag_filter": "NOT Regression"}})
        assert preview.status_code == 200, preview.text
        assert preview.json()["spec"]["selection"]["tag_filter"] == {"not": "Regression"}

    def test_a_syntax_error_is_a_400_naming_the_position(self, make_client):
        client, service = make_client()
        for path, body in (
            ("/runs", {"selection": {"tag_filter": "Auth OR (Cart"}}),
            ("/plan", {"selection": {"tag_filter": "Auth OR (Cart"}, "runner": "demo"}),
            ("/selection/compile", {"tag_filter": "Auth OR (Cart"}),
        ):
            response = client.post(f"{API}{path}", json=body)
            assert response.status_code == 400, (path, response.text)
            error = response.json()
            assert (error["code"], error["problem"], error["position"]) == ("filter-syntax", "unbalanced-open", 8)
            assert "column 9" in error["detail"]
        assert service.store.list_runs().total == 0

    def test_a_bad_pattern_inside_filter_text_names_its_node(self, make_client):
        client, _service = make_client()
        response = client.post(f"{API}/selection/compile", json={"tag_filter": 'Auth OR "regex:["'})
        assert response.status_code == 400
        assert response.json()["code"] == "invalid-filter"
        assert response.json()["detail"].startswith("tag_filter.items[1]: invalid regular expression")

    def test_structured_filters_are_untouched(self, make_client):
        client, service = make_client()
        ast = {"op": "OR", "items": ["Auth", {"not": "prefix:Quarantine-"}, "Sprint 12"]}
        created = client.post(f"{API}/runs", json={"selection": {"tag_filter": ast}})
        assert created.status_code == 201, created.text
        assert service.require_spec(created.json()["run"]["id"])["selection"]["tag_filter"] == ast

    def test_a_single_pattern_reads_as_itself_and_anything_else_must_parse(self, make_client):
        client, service = make_client()
        for pattern in ("Auth-SSO", "prefix:Payments-", '"regex:^Cart(V2)?$"', '"Sprint 12"'):
            body = client.post(f"{API}/selection/compile", json={"tag_filter": pattern}).json()
            assert [item["id"] for item in body["items"]] == _ids(service, parse_filter_text(pattern)), pattern
            assert not [w for w in body["warnings"] if "pattern" in w], body["warnings"]
        for text, problem, position in (
            ("regex:^Cart(V2)?$", "needs-quotes", 0),
            ("regex:^(Auth|Cart)-", "needs-quotes", 0),
            ("Sprint 12", "missing-operator", 7),
            ("(Payments", "unbalanced-open", 0),
            ("", "empty", 0),
            ("prefix:", "empty", 0),
        ):
            answer = client.post(f"{API}/selection/compile", json={"tag_filter": text})
            assert answer.status_code == 400, text
            body = answer.json()
            assert (body["code"], body["problem"], body["position"]) == ("filter-syntax", problem, position), text

    def test_a_tag_spelled_like_an_expression_is_written_quoted(self, make_client, oddly_tagged):
        client, service = make_client(**oddly_tagged)
        quoted = client.post(f"{API}/runs", json={"selection": {"tag_filter": '"Auth OR Cart"'}})
        spec = service.require_spec(quoted.json()["run"]["id"])
        assert spec["selection"]["tag_filter"] == "Auth OR Cart"
        assert spec["selection"]["materialized"]["item_ids"] == ["Shop.Auth.Cart.T001"]


# -- the Service call ---------------------------------------------------------------------


def test_service_compose_reads_a_string_filter_as_text(service):
    result = service.compose_run({"tag_filter": "Auth-SSO OR Cart-V2"}, title="Two areas", origin="script")
    assert result.spec["selection"]["tag_filter"] == {"op": "OR", "items": ["Auth-SSO", "Cart-V2"]}
    assert len(result.items) == 7


# -- a filter nested too deeply ----------------------------------------------------------


def _nested_and(depth: int) -> Any:
    node: Any = "Auth"
    for _ in range(depth):
        node = {"op": "AND", "items": ["Auth", node]}
    return node


class TestDepth:
    """A filter nested deeper than the documented bound is a 400
    ``filter-too-deep`` wherever it arrives — never a 500 from a recursion
    further in — and a one-line error on the command line."""

    def test_every_route_refuses_it(self, make_client):
        client, _service = make_client()
        too_deep = [_nested_and(150), {"not": _nested_and(60)}, "NOT " * 300 + "Auth"]
        for tag_filter in too_deep:
            for route, body in (
                ("/runs", {"selection": {"tag_filter": tag_filter}}),
                ("/plan", {"selection": {"tag_filter": tag_filter}, "runner": "demo"}),
                ("/selection/spec-preview", {"selection": {"tag_filter": tag_filter}}),
                ("/selection/compile", {"tag_filter": tag_filter}),
            ):
                answer = client.post(f"{API}{route}", json=body)
                assert answer.status_code == 400, (route, answer.text[:200])
                assert answer.json()["code"] == "filter-too-deep", route

    def test_the_command_line_says_so_in_one_line(self, cli):
        code, _out, err = cli("spec", "NOT " * 400 + "Auth", "--dry-run")
        assert code in (1, 2) and "Traceback" not in err
        assert "nests deeper than 40 levels" in err


class TestPatternLimit:
    """A filter of more than ``MAX_FILTER_PATTERNS`` (1000) patterns is a 400
    ``invalid-filter`` wherever it arrives, as text or as the AST — before
    a single item is matched against it."""

    def test_every_route_refuses_it_and_a_thousand_patterns_still_pass(self, make_client):
        from runcomposer.core.filter import MAX_FILTER_PATTERNS

        assert MAX_FILTER_PATTERNS == 1000
        client, _service = make_client()
        tags = [f"Sprint-{index}" for index in range(1001)]
        too_wide = [" OR ".join(tags), {"op": "OR", "items": tags}, {"not": {"op": "AND", "items": tags}}]
        for tag_filter in too_wide:
            for route, body in (
                ("/runs", {"selection": {"tag_filter": tag_filter}}),
                ("/plan", {"selection": {"tag_filter": tag_filter}, "runner": "demo"}),
                ("/selection/spec-preview", {"selection": {"tag_filter": tag_filter}}),
                ("/selection/compile", {"tag_filter": tag_filter}),
            ):
                answer = client.post(f"{API}{route}", json=body)
                assert answer.status_code == 400, (route, answer.text[:200])
                assert answer.json()["code"] == "invalid-filter", route
                assert "more than 1000 patterns" in answer.json()["detail"], route
        fits = client.post(f"{API}/selection/compile", json={"tag_filter": " OR ".join(tags[:1000])})
        assert fits.status_code == 200, fits.text[:200]

    def test_the_command_line_says_so_in_one_line(self, cli):
        code, _out, err = cli("compile", " OR ".join(f"Sprint-{index}" for index in range(1001)))
        assert code in (1, 2) and "Traceback" not in err
        assert "more than 1000 patterns" in err


class TestWhereAFilterIsWrong:
    """An ``invalid-filter`` answer says where in the filter: ``path``, the
    keys and indices from its root; filter text says ``position``."""

    def test_an_ast_names_the_node(self, make_client):
        client, _service = make_client()
        body = client.post(f"{API}/selection/compile", json={
            "tag_filter": {"op": "AND", "items": ["Auth", {"not": "regex:("}]}}).json()
        assert (body["code"], body["path"]) == ("invalid-filter", ["items", 1, "not"])
        assert body["reason"].startswith("invalid regular expression") and body["detail"].endswith(body["reason"])
        created = client.post(f"{API}/runs", json={"selection": {
            "tag_filter": {"op": "OR", "items": ["Auth", "prefix:"]}}}).json()
        assert (created["code"], created["path"]) == ("invalid-filter", ["items", 1])

    def test_text_names_the_position(self, make_client):
        client, _service = make_client()
        body = client.post(f"{API}/selection/compile", json={"tag_filter": "Auth AND (Cart"}).json()
        assert (body["code"], body["position"], body["problem"]) == ("filter-syntax", 9, "unbalanced-open")
