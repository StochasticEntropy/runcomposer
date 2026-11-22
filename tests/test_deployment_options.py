"""Deployment options (DESIGN.md §8): paths that come from the environment,
the selections a deployment offers its UI, and what ``doctor -v`` shows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runcomposer.cli import main as cli_main
from runcomposer.sdk import ConfigError, Health, RunnerBase, RunnerInfo, Service, load_config

from .conftest import ROBOT_SHOP, needs_robot, write_config

API = "/api/v1"
HERE = "tests.test_deployment_options"


class PathyRunner(RunnerBase):
    PATH_OPTIONS = ("root", "extra_roots")

    def __init__(self, root: str, extra_roots: list[str] | None = None, label: str = ""):
        self.root, self.extra_roots, self.label = root, list(extra_roots or []), label

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="pathy")

    def dispatch(self, ctx) -> None:  # type: ignore[no-untyped-def]
        ctx.refuse("never")

    def health(self) -> Health:
        return Health(
            status="degraded",
            detail="1 of 3 checks failed",
            data={
                "root": self.root,
                "checks": [
                    {"check": "checkout present", "ok": True, "detail": self.root},
                    {"check": "browser installed", "ok": False, "detail": "chromium not found"},
                    {"check": "license", "ok": True},
                ],
                "limits": {"workers": 4, "regions": ["eu", "us"]},
            },
        )


class TestPathsFromTheEnvironment:
    def runner(self, tmp_path: Path, **options: object) -> PathyRunner:
        config = load_config(str(write_config(tmp_path, runners={"pathy": {"module": f"{HERE}:PathyRunner", **options}})))
        with Service(config) as service:
            return service.runner("pathy")

    def test_an_absolute_path_from_a_variable_stays_where_the_variable_points(self, tmp_path, monkeypatch):
        """Substituted first, anchored second: before, the reference was
        anchored as text and ended up under the config directory."""
        checkout = tmp_path / "srv" / "shop" / "checkout"  # absolute on every platform
        monkeypatch.setenv("SHOP_CHECKOUT", str(checkout))
        (tmp_path / "deploy").mkdir()
        runner = self.runner(tmp_path / "deploy", root="${env:SHOP_CHECKOUT}/tests",
                             extra_roots=["${env:SHOP_CHECKOUT}/api", "local/extra"])
        assert runner.root == f"{checkout}/tests"
        assert runner.extra_roots == [f"{checkout}/api", str(tmp_path / "deploy" / "local/extra")]

    def test_a_relative_value_is_anchored_to_the_config_directory_like_any_other(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHOP_SUITES", "suites/ui")
        assert self.runner(tmp_path, root="${env:SHOP_SUITES}").root == str(tmp_path / "suites/ui")
        monkeypatch.delenv("SHOP_SUITES")
        assert self.runner(tmp_path, root="${env:SHOP_SUITES:-fallback}").root == str(tmp_path / "fallback")

    def test_a_missing_variable_is_named_with_the_entry(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SHOP_CHECKOUT", raising=False)
        with pytest.raises(ConfigError, match=r"runners\.pathy: environment variable 'SHOP_CHECKOUT' is not set"):
            self.runner(tmp_path, root="${env:SHOP_CHECKOUT}/tests")

    def test_what_is_not_a_path_option_is_substituted_as_before_and_still_redacted(self, tmp_path, monkeypatch):
        """A path is a location, not a credential: a reference used as a
        path option is not blanked; every other ``${env:…}`` value is."""
        monkeypatch.setenv("SHOP_LABEL", "blue-green-7")
        monkeypatch.setenv("SHOP_CHECKOUT", "/srv/shop/checkout")
        config = load_config(str(write_config(tmp_path, runners={
            "pathy": {"module": f"{HERE}:PathyRunner", "root": "${env:SHOP_CHECKOUT}", "label": "${env:SHOP_LABEL}"}})))
        with Service(config) as service:
            assert service.runner("pathy").label == "blue-green-7"
            assert service.redact("label blue-green-7 at /srv/shop/checkout") == (
                "label ‹from the environment› at /srv/shop/checkout"
            )

    def test_the_core_s_own_paths_take_references_too(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHOP_STATE", str(tmp_path / "state"))
        (tmp_path / "etc").mkdir()
        config = load_config(str(write_config(tmp_path / "etc", core={
            "artifact_dir": "${env:SHOP_STATE}/artifacts", "private_dir": "${env:SHOP_STATE}/private"})))
        assert config.artifact_dir == tmp_path / "state" / "artifacts"
        assert config.private_dir == tmp_path / "state" / "private"

    @needs_robot
    def test_a_robot_corpus_whose_root_comes_from_the_environment(self, tmp_path, monkeypatch):
        """The case that had no way around it: the reference source's root."""
        monkeypatch.setenv("SHOP_CORPUS", str(ROBOT_SHOP.parent))
        config = load_config(str(write_config(tmp_path, sources={
            "shop": {"plugin": "robotframework", "root": "${env:SHOP_CORPUS}/tests"}})))
        with Service(config) as service:
            service.validate()
            assert service.catalog.stats()["items"] > 0


UI = {
    "theme": "dark",   # a key runcomposer does not know: served as it is
    "default_filter": {"not": "prefix:Quarantine-"},
    "quick_filters": [
        {"label": "Checkout", "filter": "Checkout"},
        {"label": "Stable payments", "filter": {"op": "AND", "items": ["Payments", {"not": "prefix:Quarantine-"}]}},
        "Auth",
    ],
    "presets": [
        {
            "name": "release-gate",
            "label": "Release gate",
            "description": "What must be green before a release",
            "filter": {"op": "OR", "items": ["Checkout", "Payments-Cards"]},
            "runner": "demo",
            "parameters": {"seed": "release"},
            "labels": {"suite": "release"},
        },
        {"name": "smoke", "filter": "prefix:Auth-"},
    ],
}


class TestUiSelections:
    def test_quick_filters_take_whole_expressions_and_are_served_in_one_shape(self, make_client):
        client, _service = make_client(ui=UI)
        body = client.get(f"{API}/ui-config").json()
        assert body["theme"] == "dark" and body["locale_default"] == "en"
        assert body["quick_filters"] == [
            {"label": "Checkout", "filter": "Checkout", "tag_filter": "Checkout", "text": "Checkout", "kind": "pattern"},
            {
                "label": "Stable payments",
                "filter": {"op": "AND", "items": ["Payments", {"not": "prefix:Quarantine-"}]},
                "tag_filter": {"op": "AND", "items": ["Payments", {"not": "prefix:Quarantine-"}]},
                "text": "Payments AND NOT prefix:Quarantine-",
                "kind": "expression",
            },
            {"label": "Auth", "filter": "Auth", "tag_filter": "Auth", "text": "Auth", "kind": "pattern"},
        ]
        assert body["default_filter"] == {
            "tag_filter": {"not": "prefix:Quarantine-"}, "text": "NOT prefix:Quarantine-", "kind": "expression",
        }
        assert client.get(f"{API}/meta").json()["ui"]["quick_filters"] == body["quick_filters"]

    def test_presets_are_named_starting_points_that_post_runs_takes_as_they_are(self, make_client):
        client, service = make_client(ui=UI)
        listing = client.get(f"{API}/presets").json()
        gate, smoke = listing["presets"]
        assert gate == {
            "name": "release-gate", "label": "Release gate", "description": "What must be green before a release",
            "tag_filter": {"op": "OR", "items": ["Checkout", "Payments-Cards"]}, "text": "Checkout OR Payments-Cards",
            "kind": "expression", "runner": "demo", "parameters": {"seed": "release"},
            "title": "Release gate", "labels": {"suite": "release"},
        }
        assert smoke == {
            "name": "smoke", "label": "smoke", "description": "", "tag_filter": "prefix:Auth-", "text": "prefix:Auth-",
            "kind": "pattern", "runner": None, "parameters": {}, "title": "smoke", "labels": {},
        }
        assert listing["default_filter"]["text"] == "NOT prefix:Quarantine-" and len(listing["quick_filters"]) == 3
        # Everything a client needs to start the run is in the preset.
        created = client.post(f"{API}/runs", json={
            "selection": {"tag_filter": gate["tag_filter"]}, "title": gate["title"], "labels": gate["labels"],
            "dispatch": {"runner": gate["runner"], "parameters": gate["parameters"]},
        })
        assert created.status_code == 201, created.text
        run = service.wait(created.json()["run"]["id"], timeout=30)
        assert run.state == "COMPLETE" and dict(run.labels) == {"suite": "release"} and run.title == "Release gate"
        assert dict(run.latest_dispatch.parameters) == {"seed": "release"}

    def test_a_deployment_without_selections_serves_none(self, make_client):
        client, _service = make_client()
        assert client.get(f"{API}/presets").json() == {"presets": [], "default_filter": None, "quick_filters": []}

    @pytest.mark.parametrize(
        ("ui", "message"),
        [
            ({"quick_filters": [{"label": "x", "filter": {"op": "XOR", "items": ["a"]}}]},
             r"ui\.quick_filters\[0\]\.filter\.op: unknown operator 'XOR'"),
            ({"quick_filters": [{"label": "no filter"}]}, r"ui\.quick_filters\[0\] must be a mapping with a 'filter'"),
            ({"quick_filters": "Checkout"}, "ui.quick_filters must be a list"),
            ({"default_filter": {"op": "AND", "items": []}}, r"ui\.default_filter\.items: must be a non-empty list"),
            ({"presets": [{"name": "a", "filter": "x"}, {"name": "a", "filter": "y"}]}, "preset name 'a' is used twice"),
            ({"presets": [{"filter": "x"}]}, r"ui\.presets\[0\] must be a mapping with a 'name' and a 'filter'"),
            ({"presets": [{"name": "a", "filter": "x", "colour": "red"}]}, r"unknown key\(s\) \['colour'\]"),
            ({"presets": [{"name": "a", "filter": "x", "parameters": {"seed": "s"}}]}, "'parameters' need a 'runner'"),
            ({"presets": [{"name": "a", "filter": '"regex:("'}]}, r"ui\.presets\[0\]\.filter: invalid regular expression"),
        ],
    )
    def test_a_selection_that_does_not_parse_refuses_the_start_naming_where(self, tmp_path, ui, message):
        with pytest.raises(ConfigError, match=message):
            load_config(str(write_config(tmp_path, ui=ui)))

    @pytest.mark.parametrize(
        ("preset", "message"),
        [
            ({"name": "a", "filter": "x", "runner": "nightly-ci"},
             r"ui\.presets\[0\] \('a'\): runner 'nightly-ci' is not configured"),
            ({"name": "a", "filter": "x", "runner": "demo", "parameters": {"seed": 7}},
             r"ui\.presets\[0\] \('a'\): parameter 'seed' must be a string"),
            ({"name": "a", "filter": "x", "runner": "demo", "parameters": {"speed": "fast"}},
             r"ui\.presets\[0\] \('a'\): unknown parameter 'speed'"),
        ],
    )
    def test_a_preset_the_api_would_refuse_refuses_the_start_instead(self, make_service, preset, message):
        with pytest.raises(ConfigError, match=message):
            make_service(ui={"presets": [preset]}).validate()


class TestDoctorDetails:
    def config(self, tmp_path: Path) -> str:
        return str(write_config(tmp_path, runners={"pathy": {"module": f"{HERE}:PathyRunner", "root": "checkout"}}))

    def test_doctor_v_prints_what_a_runner_s_health_checks_found(self, tmp_path, capsys):
        assert cli_main(["doctor", "-v", "--config", self.config(tmp_path)]) == 1  # the runner is degraded
        out = capsys.readouterr().out
        assert "  pathy (pathy): degraded — 1 of 3 checks failed" in out
        assert f"      root: {tmp_path / 'checkout'}" in out
        assert f"        [ok  ] checkout present — {tmp_path / 'checkout'}" in out
        assert "        [FAIL] browser installed — chromium not found" in out
        assert "        [ok  ] license\n" in out
        assert "      limits:\n        workers: 4\n        regions:\n          - eu\n          - us\n" in out

    def test_without_it_the_status_line_is_all(self, tmp_path, capsys):
        cli_main(["doctor", "--config", self.config(tmp_path)])
        out = capsys.readouterr().out
        assert "pathy (pathy): degraded — 1 of 3 checks failed" in out and "browser installed" not in out

    def test_the_same_data_is_in_json_and_in_the_api(self, tmp_path, capsys, make_client):
        cli_main(["doctor", "--json", "--config", self.config(tmp_path)])
        report = json.loads(capsys.readouterr().out)
        (runner,) = report["runners"]
        assert runner["health"]["data"]["checks"][1] == {
            "check": "browser installed", "ok": False, "detail": "chromium not found"}
        client, _service = make_client(runners={"pathy": {"module": f"{HERE}:PathyRunner", "root": "checkout"}})
        (listed,) = client.get(f"{API}/runners?refresh=true").json()
        assert listed["health"]["data"]["limits"] == {"workers": 4, "regions": ["eu", "us"]}

    def test_the_global_verbose_flag_shows_them_too(self, tmp_path, capsys):
        cli_main(["-v", "doctor", "--config", self.config(tmp_path)])
        assert "[FAIL] browser installed" in capsys.readouterr().out


class TestMappingParametersOnTheCommandLine:
    """A `mapping` parameter is name → text. On the command line its values
    are taken as text, exactly as typed — a number or `true` among them is
    not a reason to refuse (the API stays strict: JSON says what it means)."""

    def config(self, tmp_path: Path) -> str:
        return str(write_config(tmp_path, runners={"pytest": {
            "plugin": "command", "command": "true",
            "parameters": [{"name": "overrides", "kind": "mapping"}, {"name": "retries", "kind": "integer"}],
        }}))

    def test_scalars_in_a_mapping_are_text_as_typed(self, tmp_path, capsys):
        code = cli_main(["plan", "Cart", "--runner", "pytest", "--json", "--config", self.config(tmp_path),
                         "--param", "overrides={PRICE: 1300, VAT: 7.5, GIFT: true, NOTE: as is}",
                         "--param", "retries=2"])
        answer = json.loads(capsys.readouterr().out)
        assert code == 0, answer
        assert answer["parameters"] == {
            "overrides": {"PRICE": "1300", "VAT": "7.5", "GIFT": "true", "NOTE": "as is"}, "retries": 2,
        }

    def test_what_is_not_a_mapping_is_still_refused_naming_the_parameter(self, tmp_path, capsys):
        config = self.config(tmp_path)
        assert cli_main(["plan", "Cart", "--runner", "pytest", "--config", config,
                         "--param", "overrides={PRICE: {EUR: 13}}"]) == 1
        assert "parameter 'overrides' must be a mapping of names to strings" in capsys.readouterr().err
        assert cli_main(["plan", "Cart", "--runner", "pytest", "--config", config, "--param", "retries={a: 1}"]) == 1
        assert "parameter 'retries' must be an integer" in capsys.readouterr().err

    def test_the_api_stays_strict(self, make_client):
        client, _service = make_client(runners={"pytest": {
            "plugin": "command", "command": "true", "parameters": [{"name": "overrides", "kind": "mapping"}]}})
        response = client.post(f"{API}/plan", json={
            "selection": {"tag_filter": "Cart"}, "runner": "pytest", "parameters": {"overrides": {"PRICE": 1300}}})
        assert (response.status_code, response.json()["code"]) == (400, "invalid-parameter")
        assert response.json()["parameter"] == "overrides"
