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
from runcomposer.core.filter import parse_filter
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

    def test_a_single_pattern_means_what_it_meant_before(self, cli):
        with cli.service() as service:
            for argument in ("Payments", "prefix:Checkout-", "regex:^Cart(V2)?$", "regex:(?i)^cart-"):
                code, out, _err = cli("compile", argument, "--json")
                assert code == 0
                assert [item["id"] for item in json.loads(out)["items"]] == _ids(service, argument)
        code, out, _err = cli("compile", "regex:^Cart(V2)?$", "--json")
        assert any('write it as "regex:^Cart(V2)?$"' in warning for warning in json.loads(out)["warnings"])


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

    def test_a_single_pattern_string_means_what_it_meant_before(self, make_client):
        client, service = make_client()
        for pattern in ("Auth-SSO", "prefix:Payments-", "regex:^Cart(V2)?$", "regex:^(Auth|Cart)-", "Sprint 12"):
            body = client.post(f"{API}/selection/compile", json={"tag_filter": pattern}).json()
            assert [item["id"] for item in body["items"]] == _ids(service, pattern), pattern
        warned = client.post(f"{API}/selection/compile", json={"tag_filter": "regex:^Cart(V2)?$"}).json()
        assert any("read as the single pattern" in warning for warning in warned["warnings"])

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
