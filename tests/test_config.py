"""Configuration (DESIGN.md §8): sections, plugin selection, option checking
at startup, secrets from the environment, one path base, zero-config mode."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from runcomposer.app.config import Config, accepted_options, load_config
from runcomposer.cli import main as cli_main
from runcomposer.http import create_app
from runcomposer.plugins.manifest_source import ManifestSource
from runcomposer.plugins.sqlite_store import SqliteRunStore
from runcomposer.sdk import (
    ConfigError,
    DispatchContext,
    Health,
    Parameter,
    RunnerBase,
    RunnerInfo,
    Service,
    UnknownRunner,
    Verdict,
)

from .conftest import compose, write_config

HERE = "tests.test_config"


# -- the small plugins these tests configure (module: "tests.test_config:Name") --------


class LegacyStore:
    """A store that declares no path options: it gets its options verbatim."""

    def __init__(self, path: str = "legacy.db"):
        self.path = path


class PathyRunner(RunnerBase):
    """Declares which of its options are paths — one of them a list."""

    PATH_OPTIONS = ("checkout", "extra_dirs")

    def __init__(self, checkout: str = "", extra_dirs: list[str] | None = None, command: str = ""):
        self.checkout = checkout
        self.extra_dirs = extra_dirs or []
        self.command = command

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="pathy")


class HookRunner(RunnerBase):
    """Resolves its own paths: ``cache`` is a path unless it is ``off``."""

    @staticmethod
    def resolve_config_paths(options: dict[str, Any], resolve: Any) -> dict[str, Any]:
        if options.get("cache") not in (None, "off"):
            options["cache"] = resolve(options["cache"])
        return options

    def __init__(self, cache: str = "off"):
        self.cache = cache

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="hook")


class BrokenHookRunner(HookRunner):
    @staticmethod
    def resolve_config_paths(options: dict[str, Any], resolve: Any) -> Any:
        return None


class StrictRunner(RunnerBase):
    def __init__(self, suite: str, retries: int = 0, *, verbose: bool = False):
        self.suite, self.retries, self.verbose = suite, retries, verbose

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="strict")


class OpenRunner(RunnerBase):
    """Takes ``**kwargs``: its option names cannot be checked from outside."""

    def __init__(self, **options: Any):
        self.options = options

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="open")


class DeclaredRunner(OpenRunner):
    """``**kwargs`` again, but with an explicit declaration of what it takes."""

    OPTIONS = ("region", "tier")


class ExplodingRunner(RunnerBase):
    def __init__(self, fuse: str = "lit"):
        raise RuntimeError("the checkout is on fire")


class SecretRunner(RunnerBase):
    """Holds credentials; simulates an execution that passes everything."""

    def __init__(
        self,
        api_token: str,
        endpoints: dict[str, Any] | None = None,
        mirrors: list[str] | None = None,
        retries: int = 0,
    ):
        self.api_token = api_token
        self.endpoints = endpoints or {}
        self.mirrors = mirrors or []
        self.retries = retries

    def describe(self) -> RunnerInfo:
        return RunnerInfo(
            id="secretive",
            title="Checkout staging",
            parameters=(Parameter("browser", kind="choice", choices=("firefox", "chromium"),
                                  default="firefox"),),
        )

    def health(self) -> Health:
        return Health(status="ok", detail="reachable", data={"region": "eu"})

    def dispatch(self, ctx: DispatchContext) -> None:
        ctx.declare(["1"])
        ctx.deliver(
            "1", verdicts=[Verdict(item, "PASS", 10) for item in ctx.item_ids],
            format="runcomposer-verdicts",
        )


def config_at(directory: Path, body: str | dict[str, Any]) -> Path:
    """Write a config file exactly as given (no defaults added)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.yaml"
    text = body if isinstance(body, str) else yaml.safe_dump(body, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return path


def load(tmp_path: Path, body: str | dict[str, Any]) -> Config:
    return load_config(str(config_at(tmp_path, body)))


def runner_entry(name: str, **options: Any) -> dict[str, Any]:
    return {"module": f"{HERE}:{name}", **options}


# -- zero-config mode --------------------------------------------------------------------


class TestZeroConfig:
    """No config file is one more config, spelled out — not special cases."""

    def test_no_config_file_yields_working_defaults(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # no ./config.yaml here
        config = load_config(None)
        assert config.path is None
        assert config.api["port"] == 8100 and config.api["host"] == "127.0.0.1"
        assert config.locale_default == "en"
        assert config.taxonomy_configured is False
        with Service(config) as service:
            assert len(service.catalog.items()) == 60  # the bundled demo corpus
            assert isinstance(service.store, SqliteRunStore)
        assert (tmp_path / "runcomposer.db").exists()

    def test_it_configures_the_demo_corpus_and_the_demo_runner_explicitly(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = load_config(None)
        assert config.data["sources"] == {"demo-corpus": {}}
        assert config.data["runners"] == {"demo": {}}
        assert config.runner_ids() == ["demo"]
        assert [spec.key for spec in config.source_specs()] == ["demo-corpus"]

    def test_a_config_yaml_in_the_working_directory_is_picked_up(self, tmp_path, monkeypatch):
        config_at(tmp_path, {"core": {"locale_default": "de"}})
        monkeypatch.chdir(tmp_path)
        config = load_config(None)
        assert config.path == Path("config.yaml") and config.locale_default == "de"

    def test_a_config_file_without_runners_has_nothing_to_dispatch_to(self, tmp_path):
        """Regression: with only a store configured, the bundled demo runner
        used to stay dispatchable and wrote fabricated history."""
        config = load(tmp_path, {"store": {"sqlite": {"path": "x.db"}}})
        assert config.runner_ids() == []
        with Service(config) as service:
            service.validate()
            assert service.runner_infos() == []
            with pytest.raises(UnknownRunner, match="no runner is configured under the id 'demo'"):
                service.runner("demo")
            run_id = compose(service)
            with pytest.raises(UnknownRunner):
                service.dispatch(run_id, "demo", foreground=True)
            assert service.require_run(run_id).state == "COMPOSED"
            assert service.store.verdicts_for(run_id) == []

    def test_a_config_file_still_defaults_the_store_and_the_catalog(self, tmp_path):
        config = load(tmp_path, {"core": {"locale_default": "de"}})
        assert config.store_spec().key == "sqlite"
        assert Path(config.store_spec().options["path"]) == tmp_path / "runcomposer.db"
        assert [spec.key for spec in config.source_specs()] == ["demo-corpus"]


# -- loading and the closed set of sections ------------------------------------------------


class TestLoading:
    def test_config_file_sections_are_applied(self, tmp_path):
        config = load(
            tmp_path,
            f"""
core:
  api: {{ port: 9000 }}
  locale_default: de
store:
  sqlite: {{ path: {tmp_path / 'x.db'} }}
ui:
  quick_filters: [Payments]
""",
        )
        assert config.api["port"] == 9000
        assert config.api["host"] == "127.0.0.1"  # defaults merged
        assert config.locale_default == "de"
        assert config.ui == {"quick_filters": ["Payments"]}
        assert isinstance(config.build(config.store_spec()), SqliteRunStore)

    def test_missing_explicit_config_fails(self):
        with pytest.raises(ConfigError, match="not found"):
            load_config("does-not-exist.yaml")

    def test_invalid_yaml_and_non_mappings_fail(self, tmp_path):
        with pytest.raises(ConfigError, match="not valid YAML"):
            load(tmp_path, "store: {sqlite: [\n")
        with pytest.raises(ConfigError, match="config must be a mapping"):
            load(tmp_path, "- just\n- a list\n")
        with pytest.raises(ConfigError, match="'core' must be a mapping"):
            load(tmp_path, "core: [1, 2]\n")

    def test_unknown_top_level_section_fails_and_points_at_connectors(self, tmp_path):
        """A top-level key the config does not know is refused, and the
        error says where a connector package's own settings belong."""
        with pytest.raises(ConfigError) as caught:
            load(tmp_path, "shop_alerts: {channel: checkout}\n")
        message = str(caught.value)
        assert "unknown top-level section(s) ['shop_alerts']" in message
        assert "'connectors:'" in message and "connectors.<name>" in message
        assert "runners" in message  # …and lists the sections that do exist

    def test_a_typo_in_a_section_name_fails_loudly(self, tmp_path):
        with pytest.raises(ConfigError, match="stroe"):
            load(tmp_path, "stroe: {sqlite: {}}\n")

    def test_unknown_core_key_fails_loudly_and_lists_the_known(self, tmp_path):
        with pytest.raises(ConfigError, match="taxonomyfile") as caught:
            load(tmp_path, "core: {taxonomyfile: x}\n")
        assert "taxonomy_file" in str(caught.value)

    def test_every_documented_section_is_accepted(self, tmp_path):
        config = load(
            tmp_path,
            {
                "core": {"dispatch": {"max_concurrent": 3}},
                "store": {"sqlite": {"path": "x.db"}},
                "sources": {"demo-corpus": {}},
                "runners": {"demo": {}},
                "parsers": {"junit-xml": {"pattern": "report*.xml"}},
                "observers": {"audit": {"plugin": "jsonl", "path": "events.jsonl"}},
                "ui": {"quick_filters": ["Checkout"]},
                "connectors": {"shop": {"region": "eu"}},
            },
        )
        config.validate()
        assert config.dispatch["max_concurrent"] == 3


class TestConnectorsNamespace:
    def test_connector_settings_reads_what_the_core_never_interprets(self, tmp_path):
        config = load(
            tmp_path,
            {"connectors": {"shop": {"alerts": {"channel": "checkout"}, "retries": 3},
                            "other": {"x": 1}}},
        )
        assert config.connector_settings("shop") == {
            "alerts": {"channel": "checkout"}, "retries": 3,
        }
        config.validate()  # nothing in it is validated as a plugin

    def test_an_absent_connector_has_empty_settings(self, tmp_path):
        assert load(tmp_path, {"connectors": {"shop": {"a": 1}}}).connector_settings("nope") == {}
        assert load(tmp_path, {}).connector_settings("shop") == {}

    def test_the_namespace_itself_has_a_shape(self, tmp_path):
        with pytest.raises(ConfigError, match="'connectors' must be a mapping"):
            load(tmp_path, "connectors: [shop]\n")
        with pytest.raises(ConfigError, match=r"connectors\.shop must be a mapping"):
            load(tmp_path, "connectors: {shop: just-a-string}\n").connector_settings("shop")


class TestCoreValues:
    def test_retention_null_means_no_limit(self, tmp_path):
        config = load(tmp_path, {"core": {"retention": {"max_age_days": None, "max_runs": None}}})
        assert config.retention == {"max_age_days": None, "max_runs": None}

    def test_retention_defaults(self, tmp_path):
        assert load(tmp_path, {}).retention == {"max_age_days": 90, "max_runs": None}

    @pytest.mark.parametrize("key", ["max_age_days", "max_runs"])
    @pytest.mark.parametrize("value", [-1, "ninety", True])
    def test_retention_must_be_a_non_negative_number_or_null(self, tmp_path, key, value):
        with pytest.raises(ConfigError, match=f"core.retention.{key} must be a non-negative number"):
            load(tmp_path, {"core": {"retention": {key: value}}})

    def test_dispatch_defaults(self, tmp_path):
        assert load(tmp_path, {}).dispatch == {"max_concurrent": 2, "handoff_wait_s": 2.0}

    @pytest.mark.parametrize("value", [0, -2, "two", 1.5, True, None])
    def test_max_concurrent_must_be_a_positive_integer(self, tmp_path, value):
        with pytest.raises(ConfigError, match=r"core\.dispatch\.max_concurrent must be an integer"):
            load(tmp_path, {"core": {"dispatch": {"max_concurrent": value}}})

    def test_tokens_is_required_or_disabled(self, tmp_path):
        assert load(tmp_path, {}).tokens_required is True
        disabled = load(tmp_path, {"core": {"ingestion": {"tokens": "disabled"}}})
        assert disabled.tokens_required is False
        with pytest.raises(ConfigError, match=r"core\.ingestion\.tokens must be 'required' or 'disabled'"):
            load(tmp_path, {"core": {"ingestion": {"tokens": "optional"}}})

    def test_inbox_null_disables_the_watcher(self, tmp_path):
        assert load(tmp_path, {"core": {"ingestion": {"inbox": None}}}).inbox_dir is None
        assert load(tmp_path, {}).inbox_dir == tmp_path / "results_inbox"


# -- plugin selection: the key is the id ------------------------------------------------------


class TestPluginSelection:
    def test_the_key_names_the_entry_point_by_default(self, tmp_path):
        config = load(tmp_path, {"runners": {"demo": {}}})
        spec = config.plugin_spec("runners", "demo")
        assert spec.cls.__name__ == "DemoRunner" and spec.where == "runners.demo"

    def test_plugin_selects_an_entry_point_under_another_key(self, tmp_path):
        config = load(tmp_path, {"runners": {"smoke-sim": {"plugin": "demo", "seed": "smoke"}}})
        spec = config.plugin_spec("runners", "smoke-sim")
        assert spec.cls.__name__ == "DemoRunner"
        assert spec.options == {"seed": "smoke"}  # the selector is not an option
        assert config.build(spec).seed == "smoke"

    def test_module_selects_an_import_path(self, tmp_path):
        """The hack-it-in-an-afternoon path: module: pkg.mod:Class (§6)."""
        config = load(
            tmp_path,
            {"store": {"my-store": {"module": "runcomposer.plugins.sqlite_store:SqliteRunStore",
                                    "path": str(tmp_path / "custom.db")}}},
        )
        assert isinstance(config.build(config.store_spec()), SqliteRunStore)
        assert (tmp_path / "custom.db").exists()

    def test_one_plugin_may_be_configured_several_times(self, tmp_path):
        """The key IS the runner id: two instances of one class
        are two runners, each listed and dispatched under its own key."""
        config = load(
            tmp_path,
            {
                "store": {"sqlite": {"path": "x.db"}},
                "runners": {
                    "nightly-sim": {"plugin": "demo", "seed": "nightly"},
                    "smoke-sim": {"plugin": "demo", "seed": "smoke"},
                },
                "core": {"ingestion": {"inbox": None}},
            },
        )
        assert config.runner_ids() == ["nightly-sim", "smoke-sim"]
        with Service(config) as service:
            service.validate()
            assert service.runner("nightly-sim").seed == "nightly"
            assert service.runner("smoke-sim").seed == "smoke"
            assert service.runner("nightly-sim") is not service.runner("smoke-sim")
            assert service.runner("smoke-sim") is service.runner("smoke-sim")  # one instance per id
            infos = service.runner_infos()
            assert [(info["id"], info["plugin"]) for info in infos] == [
                ("nightly-sim", "demo"), ("smoke-sim", "demo"),
            ]
            with pytest.raises(UnknownRunner):
                service.runner("demo")  # the plugin's own name is not an id here
            run_id = compose(service)
            service.dispatch(run_id, "smoke-sim", foreground=True)
            assert service.require_run(run_id).latest_dispatch.mode == "smoke-sim"

    def test_both_selectors_at_once_is_an_error(self, tmp_path):
        config = load(
            tmp_path, {"runners": {"x": {"plugin": "demo", "module": f"{HERE}:OpenRunner"}}}
        )
        with pytest.raises(ConfigError, match=r"runners\.x: give 'module'.*or 'plugin'.*not both"):
            config.plugin_specs("runners")

    def test_unknown_plugin_id_fails_loudly_naming_what_is_installed(self, tmp_path):
        config = load(tmp_path, {"store": {"mongodb": {}}})
        with pytest.raises(ConfigError, match=r"store\.mongodb: no entry point 'mongodb'") as caught:
            config.store_spec()
        assert "sqlite" in str(caught.value)

    @pytest.mark.parametrize(
        ("ref", "fragment"),
        [
            ("tests.test_config.OpenRunner", "must look like 'package.module:ClassName'"),
            ("tests.no_such_module:OpenRunner", "cannot import plugin module"),
            (f"{HERE}:NoSuchClass", "has no attribute 'NoSuchClass'"),
        ],
    )
    def test_a_bad_import_path_names_the_entry(self, tmp_path, ref, fragment):
        config = load(tmp_path, {"runners": {"mine": {"module": ref}}})
        with pytest.raises(ConfigError, match=r"runners\.mine: ") as caught:
            config.plugin_specs("runners")
        assert fragment in str(caught.value)

    def test_the_store_is_exactly_one(self, tmp_path):
        config = load(tmp_path, "store: {sqlite: {}, other: {plugin: sqlite}}\n")
        with pytest.raises(ConfigError, match="'store' must configure exactly one plugin"):
            config.store_spec()

    def test_sources_may_be_several(self, tmp_path):
        """Regression: a second corpus used to be a config error."""
        (tmp_path / "extra.json").write_text(
            json.dumps({"items": [{"id": "Shop.Returns.T001", "tags": ["Returns"]}]}),
            encoding="utf-8",
        )
        config = load(
            tmp_path,
            {"sources": {"demo-corpus": {}, "returns": {"plugin": "manifest", "path": "extra.json"}},
             "store": {"sqlite": {"path": "x.db"}}},
        )
        assert [spec.key for spec in config.source_specs()] == ["demo-corpus", "returns"]
        with Service(config) as service:
            service.validate()
            assert len(service.catalog.items()) == 61
            assert service.catalog.resolve("Shop.Returns.T001") == "Shop.Returns.T001"

    def test_sources_must_be_at_least_one(self, tmp_path):
        with pytest.raises(ConfigError, match="'sources' must configure at least one plugin"):
            load(tmp_path, "sources: {}\n").source_specs()

    def test_a_section_and_an_entry_must_be_mappings(self, tmp_path):
        with pytest.raises(ConfigError, match="'runners' must be a mapping"):
            load(tmp_path, "runners: [demo]\n").plugin_specs("runners")
        with pytest.raises(ConfigError, match=r"runners\.demo must be a mapping of options"):
            load(tmp_path, "runners: {demo: fast}\n").plugin_specs("runners")

    def test_an_entry_without_options_is_fine(self, tmp_path):
        assert load(tmp_path, "runners: {demo: }\n").plugin_spec("runners", "demo").options == {}


# -- options are checked at startup ---------------------------------------------------------


class TestOptionChecking:
    """A mistyped plugin option stops the start with the accepted keys in
    the message, instead of failing later, at the first dispatch."""

    def test_an_unknown_option_names_the_accepted_keys(self, tmp_path):
        config = load(tmp_path, {"runners": {"sim": {"plugin": "demo", "sede": "x"}}})
        with pytest.raises(ConfigError) as caught:
            config.validate()
        message = str(caught.value)
        assert "runners.sim: unknown option(s) ['sede']" in message
        assert "DemoRunner accepts: ['delay_s', 'seed']" in message

    def test_a_missing_required_option_is_named(self, tmp_path):
        config = load(tmp_path, {"runners": {"strict": runner_entry("StrictRunner", retries=2)}})
        with pytest.raises(ConfigError) as caught:
            config.validate()
        message = str(caught.value)
        assert "runners.strict: missing required option(s) ['suite']" in message
        assert "['retries', 'suite', 'verbose']" in message

    def test_positional_and_keyword_only_parameters_are_both_options(self):
        assert accepted_options(StrictRunner) == (["suite", "retries", "verbose"], ["suite"], False)

    def test_a_kwargs_plugin_is_exempt(self, tmp_path):
        config = load(tmp_path, {"runners": {"open": runner_entry("OpenRunner", anything="goes")}})
        config.validate()
        assert config.build(config.plugin_spec("runners", "open")).options == {"anything": "goes"}
        assert accepted_options(OpenRunner) == ([], [], True)

    def test_an_explicit_options_declaration_is_checked_instead(self, tmp_path):
        good = load(tmp_path / "good", {"runners": {"d": runner_entry("DeclaredRunner", region="eu")}})
        good.validate()
        bad = load(tmp_path / "bad", {"runners": {"d": runner_entry("DeclaredRunner", regoin="eu")}})
        with pytest.raises(ConfigError, match=r"runners.d: unknown option\(s\) \['regoin'\]") as caught:
            bad.validate()
        assert "['region', 'tier']" in str(caught.value)

    def test_the_documented_wrong_option_name_now_fails_at_startup(self, tmp_path):
        """The case from the field: a config written from a stale document."""
        config = load(
            tmp_path,
            {"runners": {"jenkins": {"base_url": "https://ci.shop.example", "job_template": "x"}}},
        )
        with pytest.raises(ConfigError, match="job_template") as caught:
            config.validate()
        assert "'job'" in str(caught.value)

    def test_every_section_is_checked(self, tmp_path):
        cases = {
            "store": {"store": {"sqlite": {"paht": "x.db"}}},
            "sources": {"sources": {"manifest": {"path": "c.json", "strict": True}}},
            "parsers": {"parsers": {"junit-xml": {"glob": "*.xml"}}},
            "observers": {"observers": {"audit": {"plugin": "jsonl", "file": "e.jsonl"}}},
        }
        for section, body in cases.items():
            with pytest.raises(ConfigError, match=rf"{section}\.[\w-]+: unknown option"):
                load(tmp_path / section, body).validate()

    def test_validate_constructs_nothing(self, tmp_path):
        config = load(tmp_path, {"runners": {"boom": runner_entry("ExplodingRunner")}})
        config.validate()  # option names only — the constructor is not called

    def test_service_validate_builds_every_plugin_so_a_bad_one_fails_the_start(self, tmp_path):
        config = load(
            tmp_path,
            {"store": {"sqlite": {"path": "x.db"}},
             "runners": {"boom": runner_entry("ExplodingRunner")}},
        )
        with Service(config) as service, pytest.raises(
            ConfigError, match=r"runners\.boom: RuntimeError: the checkout is on fire"
        ):
            service.validate()

    def test_a_plugin_that_refuses_its_values_fails_the_start(self, tmp_path):
        config = load(
            tmp_path,
            {"store": {"sqlite": {"path": "x.db"}},
             "runners": {"ci": {"plugin": "jenkins", "base_url": "ci.shop.example", "job": "x"}}},
        )
        with Service(config) as service, pytest.raises(ConfigError, match=r"runners\.ci: jenkins: base_url must start with http"):
            service.validate()


@pytest.fixture()
def pristine_logging():
    """``serve`` installs a handler on the ``runcomposer`` logger; put the
    logger back so the rest of the test session is not affected."""
    import logging

    logger = logging.getLogger("runcomposer")
    before = (list(logger.handlers), logger.level, logger.propagate)
    yield
    logger.handlers[:], logger.level, logger.propagate = before


class TestStartupFailsLoudly:
    """§8: a server that boots healthy and fails on first touch is the
    forbidden failure mode."""

    @pytest.mark.parametrize(
        ("body", "needle"),
        [
            ({"store": {"nosuchstore": {}}}, "nosuchstore"),
            ({"sources": {"nosuchsource": {}}}, "nosuchsource"),
            ({"runners": {"nosuchrunner": {}}}, "nosuchrunner"),
            ({"parsers": {"nosuchformat": {}}}, "nosuchformat"),
            ({"observers": {"nosuchobserver": {}}}, "nosuchobserver"),
            ({"runners": {"sim": {"plugin": "demo", "sede": "x"}}}, "unknown option"),
            ({"observers": {"hook": {"plugin": "webhook", "url": "ftp://x"}}}, "url must start with http"),
        ],
    )
    def test_create_app_refuses(self, tmp_path, body, needle):
        body.setdefault("store", {"sqlite": {"path": "x.db"}})
        body.setdefault("core", {"ingestion": {"inbox": None}})
        with pytest.raises(ConfigError, match=needle):
            create_app(load(tmp_path, body))

    def test_serve_exits_cleanly_on_a_bad_plugin(self, tmp_path, capsys, pristine_logging):
        bad = config_at(tmp_path, "store: {nosuchstore: {}}\n")
        assert cli_main(["serve", "--config", str(bad), "--port", "8399"]) == 2
        err = capsys.readouterr().err
        assert err.startswith("error:") and "nosuchstore" in err

    def test_serve_exits_cleanly_on_an_option_typo(self, tmp_path, capsys, pristine_logging):
        bad = config_at(tmp_path, {"runners": {"sim": {"plugin": "demo", "sede": "x"}}})
        assert cli_main(["serve", "--config", str(bad), "--port", "8399"]) == 2
        err = capsys.readouterr().err
        assert "runners.sim: unknown option(s) ['sede']" in err and "Traceback" not in err

    def test_a_cli_config_error_is_a_clean_one_liner(self, tmp_path, capsys):
        bad = config_at(tmp_path, "store: {nosuchstore: {}}\n")
        assert cli_main(["runs", "--config", str(bad)]) == 2
        err = capsys.readouterr().err
        assert err.startswith("error:") and "nosuchstore" in err
        assert "Traceback" not in err and len(err.strip().splitlines()) == 1

    def test_an_unknown_top_level_section_is_exit_2_naming_connectors(self, tmp_path, capsys):
        bad = config_at(tmp_path, "shop_alerts: {}\n")
        assert cli_main(["catalog", "--config", str(bad)]) == 2
        assert "connectors:" in capsys.readouterr().err


# -- parsers and observers --------------------------------------------------------------------


class TestParsersSection:
    """Result parsers are configured under `parsers:`, by format id."""

    def test_options_reach_the_parser_of_that_format_id(self, make_service):
        service = make_service(parsers={"junit-xml": {"pattern": "report-*.xml"}})
        service.validate()
        assert service.ingestion.parser("junit-xml").pattern == "report-*.xml"

    def test_an_unconfigured_format_falls_back_to_its_entry_point(self, make_service):
        service = make_service(parsers={"junit-xml": {"pattern": "report-*.xml"}})
        assert service.ingestion.parser("robot-output-xml").pattern == "output*.xml"
        assert service.ingestion.parser("runcomposer-verdicts").format_id == "runcomposer-verdicts"

    def test_a_format_id_of_ones_own_is_a_configured_name_not_an_import_path(self, make_service):
        service = make_service(
            parsers={"shop-junit": {"module": "runcomposer.plugins.junit_parser:JunitXmlParser",
                                    "pattern": "shop*.xml"}}
        )
        service.validate()
        assert service.ingestion.parser("shop-junit").pattern == "shop*.xml"


class TestObserversSection:
    def test_several_observers_each_under_its_own_key(self, make_service):
        service = make_service(
            observers={
                "audit": {"plugin": "jsonl", "path": "audit.jsonl"},
                "second": {"plugin": "jsonl", "path": "second.jsonl"},
            }
        )
        service.validate()
        assert [spec.key for spec in service.config.plugin_specs("observers")] == ["audit", "second"]

    def test_events_is_reserved_for_the_bus_and_not_an_option(self, make_service, tmp_path):
        service = make_service(
            observers={"audit": {"plugin": "jsonl", "path": "audit.jsonl",
                                 "events": ["run.completed"]}}
        )
        service.validate()  # `events` is not handed to JsonlObserver(path=…)
        run_id = compose(service)
        service.dispatch(run_id, "demo", foreground=True)
        assert service.bus.flush()
        lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["kind"] for line in lines] == ["run.completed"]

    def test_events_must_be_a_list_of_kinds(self, make_service):
        service = make_service(
            observers={"audit": {"plugin": "jsonl", "path": "a.jsonl", "events": "run.completed"}}
        )
        with pytest.raises(ConfigError, match=r"observers\.audit\.events must be a list"):
            service.validate()

    def test_events_is_not_reserved_anywhere_else(self, tmp_path):
        config = load(tmp_path, {"runners": {"sim": {"plugin": "demo", "events": ["x"]}}})
        with pytest.raises(ConfigError, match=r"runners.sim: unknown option\(s\) \['events'\]"):
            config.validate()


# -- secrets -----------------------------------------------------------------------------------

SECRET = "s3cr3t-checkout-token"


class TestSecrets:
    def secretive(self, **options: Any) -> dict[str, Any]:
        return {"store": {"sqlite": {"path": "x.db"}},
                "runners": {"staging": runner_entry("SecretRunner", **options)},
                "core": {"ingestion": {"inbox": None}}}

    def test_env_reference_is_resolved_when_the_plugin_is_built(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHOP_CI_TOKEN", SECRET)
        config = load(tmp_path, self.secretive(api_token="${env:SHOP_CI_TOKEN}"))
        spec = config.plugin_spec("runners", "staging")
        assert spec.options["api_token"] == "${env:SHOP_CI_TOKEN}"  # as written, until built
        assert config.build(spec).api_token == SECRET

    def test_a_default_makes_the_variable_optional(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SHOP_CI_TOKEN", raising=False)
        config = load(tmp_path, self.secretive(api_token="${env:SHOP_CI_TOKEN:-anonymous}"))
        assert config.build(config.plugin_spec("runners", "staging")).api_token == "anonymous"
        monkeypatch.setenv("SHOP_CI_TOKEN", SECRET)
        assert config.build(config.plugin_spec("runners", "staging")).api_token == SECRET

    def test_an_empty_default_is_allowed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SHOP_CI_TOKEN", raising=False)
        config = load(tmp_path, self.secretive(api_token="${env:SHOP_CI_TOKEN:-}"))
        assert config.build(config.plugin_spec("runners", "staging")).api_token == ""

    def test_references_resolve_inside_strings_mappings_and_lists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHOP_CI_TOKEN", SECRET)
        monkeypatch.setenv("SHOP_REGION", "eu")
        config = load(
            tmp_path,
            self.secretive(
                api_token="Bearer ${env:SHOP_CI_TOKEN}",
                endpoints={"primary": {"url": "https://${env:SHOP_REGION}.ci.shop.example",
                                       "key": "${env:SHOP_CI_TOKEN}"}},
                mirrors=["https://${env:SHOP_REGION}-a.shop.example", "plain"],
                retries=3,
            ),
        )
        runner = config.build(config.plugin_spec("runners", "staging"))
        assert runner.api_token == f"Bearer {SECRET}"
        assert runner.endpoints == {"primary": {"url": "https://eu.ci.shop.example", "key": SECRET}}
        assert runner.mirrors == ["https://eu-a.shop.example", "plain"]
        assert runner.retries == 3  # non-strings pass through untouched

    def test_a_missing_variable_is_a_config_error_naming_it(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SHOP_CI_TOKEN", raising=False)
        config = load(tmp_path, self.secretive(api_token="${env:SHOP_CI_TOKEN}"))
        config.validate()  # names are fine…
        with Service(config) as service, pytest.raises(ConfigError) as caught:
            service.validate()  # …building is where the environment is needed
        message = str(caught.value)
        assert "runners.staging: environment variable 'SHOP_CI_TOKEN' is not set" in message
        assert "${env:SHOP_CI_TOKEN:-default}" in message

    def test_a_missing_variable_fails_the_server_start(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SHOP_CI_TOKEN", raising=False)
        with pytest.raises(ConfigError, match="SHOP_CI_TOKEN"):
            create_app(load(tmp_path, self.secretive(api_token="${env:SHOP_CI_TOKEN}")))

    def test_text_that_only_looks_like_a_reference_is_left_alone(self, tmp_path):
        config = load(tmp_path, self.secretive(api_token="${HOME} $env:X ${env:}"))
        assert config.build(config.plugin_spec("runners", "staging")).api_token == "${HOME} $env:X ${env:}"

    def test_a_resolved_secret_never_reaches_a_document_or_an_api_answer(
        self, make_client, monkeypatch
    ):
        """Configured options stay in the process: they are not written into
        the run spec, not served by the read API, not put into events."""
        monkeypatch.setenv("SHOP_CI_TOKEN", SECRET)
        client, service = make_client(
            self.secretive(api_token="${env:SHOP_CI_TOKEN}",
                           endpoints={"primary": {"key": "${env:SHOP_CI_TOKEN}"}})
        )
        assert service.runner("staging").api_token == SECRET  # it did arrive where it belongs

        created = client.post(
            "/api/v1/runs",
            json={"selection": {"tag_filter": "Checkout"}, "title": "Checkout staging",
                  "dispatch": {"runner": "staging", "parameters": {"browser": "chromium"}}},
        )
        assert created.status_code == 201, created.text
        run_id = created.json()["run"]["id"]
        assert service.wait(run_id, timeout=30).state == "COMPLETE"

        spec = service.require_spec(run_id)
        assert spec["runner"] == {"staging": {"browser": "chromium"}}  # parameters only
        answers = [
            created.text,
            json.dumps(spec),
            client.get(f"/api/v1/runs/{run_id}/spec").text,
            client.get(f"/api/v1/runs/{run_id}/spec?format=yaml").text,
            client.get(f"/api/v1/runs/{run_id}").text,
            client.get(f"/api/v1/runs/{run_id}/events").text,
            client.get("/api/v1/runs").text,
            client.get("/api/v1/meta").text,
            client.get("/api/v1/runners").text,
            client.get("/api/v1/runners?refresh=true").text,
            client.get("/api/v1/ui-config").text,
            client.post("/api/v1/selection/spec-preview",
                        json={"selection": {"tag_filter": "Checkout"}}).text,
        ]
        for answer in answers:
            assert SECRET not in answer
            assert "SHOP_CI_TOKEN" not in answer  # nor the reference to it
            assert "api_token" not in answer
        # The runner is still described — by what it declares, not by its options.
        (listed,) = client.get("/api/v1/runners").json()
        assert listed["id"] == "staging" and listed["plugin"] == "secretive"
        assert [p["name"] for p in listed["parameters"]] == ["browser"]
        assert listed["health"] == {"status": "ok", "detail": "reachable", "data": {"region": "eu"}}
        meta = client.get("/api/v1/meta").json()
        assert [runner["id"] for runner in meta["runners"]] == ["staging"]

    def test_an_exported_document_carries_no_configured_option_either(self, make_service, monkeypatch):
        monkeypatch.setenv("SHOP_CI_TOKEN", SECRET)
        service = make_service(self.secretive(api_token="${env:SHOP_CI_TOKEN}"))
        run_id = compose(service, "Checkout", runner_id="staging", parameters={"browser": "firefox"})
        exported = service.export(run_id)
        assert SECRET.encode() not in exported.content
        assert b"api_token" not in exported.content


# -- one path base -------------------------------------------------------------------------------


class TestOnePathBase:
    """§8: *every* relative path in a config file resolves against the config
    file's directory — the core's own keys and the plugin sections alike
    (there used to be two bases)."""

    def _config(self, tmp_path: Path, body: str | dict[str, Any]) -> Path:
        return config_at(tmp_path / "envs" / "staging", body)

    def test_same_config_from_two_directories_is_one_database(self, tmp_path, monkeypatch):
        config_file = self._config(tmp_path, "store: {sqlite: {path: staging.db}}\n")
        here, there = tmp_path / "here", tmp_path / "there"
        here.mkdir()
        there.mkdir()

        monkeypatch.chdir(here)
        with Service(load_config(str(config_file))) as service:
            composed = compose(service, title="Composed from here")
        monkeypatch.chdir(there)
        with Service(load_config(str(config_file))) as service:
            from_there = service.list_runs().runs

        assert [run.title for run in from_there] == ["Composed from here"]
        assert from_there[0].id == composed
        assert (config_file.parent / "staging.db").exists()
        # The symptom: a second, empty database beside the working directory.
        assert not (here / "staging.db").exists()
        assert not (there / "staging.db").exists()

    def test_source_root_is_found_from_any_directory(self, tmp_path, monkeypatch):
        config_file = self._config(
            tmp_path, "sources: {manifest: {path: corpus.json}}\nstore: {sqlite: {path: s.db}}\n"
        )
        (config_file.parent / "corpus.json").write_text(
            json.dumps({"items": [{"id": "Shop.Payments.T001", "tags": ["Payments"]}]}),
            encoding="utf-8",
        )
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        config = load_config(str(config_file))
        source = config.build(config.source_specs()[0])
        assert isinstance(source, ManifestSource)
        assert [item.id for item in source.items()] == ["Shop.Payments.T001"]

    def test_core_and_plugin_keys_share_one_base(self, tmp_path, monkeypatch):
        config_file = self._config(
            tmp_path,
            "core:\n"
            "  artifact_dir: artifacts\n"
            "  taxonomy_file: taxonomy.yaml\n"
            "  ingestion: {inbox: inbox, quarantine_dir: quarantine}\n"
            "store: {sqlite: {path: staging.db}}\n",
        )
        monkeypatch.chdir(tmp_path)
        config = load_config(str(config_file))

        base = config_file.parent
        assert config.base_dir == base
        assert config.artifact_dir == base / "artifacts"
        assert config.inbox_dir == base / "inbox"
        assert config.quarantine_dir == base / "quarantine"
        assert config.taxonomy_source == base / "taxonomy.yaml" and config.taxonomy_configured
        config.build(config.store_spec())
        assert (base / "staging.db").exists()

    def test_declared_path_options_resolve_and_the_rest_do_not(self, tmp_path, monkeypatch):
        """A plugin says which options are paths (``PATH_OPTIONS``); a
        command line or a listener spec next to them is left as written."""
        config_file = self._config(
            tmp_path,
            {"runners": {
                "robot-pool": {
                    "suite_root": "tests",
                    "pythonpath": ["libs", "/opt/shop/keywords"],
                    "listener": "MyListener:arg",
                    "pre_run_hooks": ["echo ready"],
                },
                "pathy": runner_entry("PathyRunner", checkout="checkout",
                                      extra_dirs=["a", "/abs/b", "c/d"], command="./run.sh tests"),
            }},
        )
        monkeypatch.chdir(tmp_path)
        config = load_config(str(config_file))
        base = config_file.parent

        robot = config.plugin_spec("runners", "robot-pool").options
        assert Path(robot["suite_root"]) == base / "tests"
        assert robot["pythonpath"] == [str(base / "libs"), "/opt/shop/keywords"]  # list, entry by entry
        assert robot["listener"] == "MyListener:arg"  # not a path
        assert robot["pre_run_hooks"] == ["echo ready"]  # not a path

        pathy = config.build(config.plugin_spec("runners", "pathy"))
        assert Path(pathy.checkout) == base / "checkout"
        assert pathy.extra_dirs == [str(base / "a"), "/abs/b", str(base / "c" / "d")]
        assert pathy.command == "./run.sh tests"

    def test_the_hook_decides_when_the_rule_has_exceptions(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        on = load_config(
            str(self._config(tmp_path / "on", {"runners": {"h": runner_entry("HookRunner", cache="c")}}))
        )
        assert Path(on.plugin_spec("runners", "h").options["cache"]) == on.base_dir / "c"
        off = load_config(
            str(self._config(tmp_path / "off", {"runners": {"h": runner_entry("HookRunner", cache="off")}}))
        )
        assert off.plugin_spec("runners", "h").options["cache"] == "off"

    def test_a_hook_must_hand_its_options_back(self, tmp_path):
        config = load_config(
            str(self._config(tmp_path, {"runners": {"h": runner_entry("BrokenHookRunner", cache="c")}}))
        )
        with pytest.raises(ConfigError, match="resolve_config_paths must return a dict"):
            config.plugin_specs("runners")

    def test_a_path_that_is_not_a_path_survives(self, tmp_path, monkeypatch):
        """``:memory:`` names no file on disk. Only the store can know that,
        which is why this resolution is the plugin's decision."""
        config_file = self._config(tmp_path, "store: {sqlite: {path: ':memory:'}}\n")
        monkeypatch.chdir(tmp_path)
        config = load_config(str(config_file))
        assert config.store_spec().options["path"] == ":memory:"
        store = config.build(config.store_spec())
        assert store.list_runs().total == 0
        store.close()
        assert not (config_file.parent / ":memory:").exists()
        assert not (tmp_path / ":memory:").exists()

    def test_absolute_paths_are_untouched(self, tmp_path, monkeypatch):
        absolute = tmp_path / "somewhere" / "abs.db"
        absolute.parent.mkdir()
        config_file = self._config(tmp_path, f"store: {{sqlite: {{path: {absolute}}}}}\n")
        monkeypatch.chdir(tmp_path)
        config = load_config(str(config_file))
        assert config.store_spec().options["path"] == str(absolute)
        config.build(config.store_spec())
        assert absolute.exists()

    def test_a_plugin_that_declares_no_paths_gets_its_options_verbatim(self, tmp_path, monkeypatch):
        config_file = self._config(
            tmp_path, {"store": {"legacy": {"module": f"{HERE}:LegacyStore", "path": "legacy.db"}}}
        )
        monkeypatch.chdir(tmp_path)
        config = load_config(str(config_file))
        assert config.build(config.store_spec()).path == "legacy.db"

    def test_resolve_path_is_total_and_conservative(self, tmp_path):
        config = load_config(str(self._config(tmp_path, {})))
        base = config.base_dir
        assert config.resolve_path("tests") == str(base / "tests")
        assert config.resolve_path("/abs/tests") == "/abs/tests"
        assert config.resolve_path("") == ""
        assert config.resolve_path(None) is None
        assert config.resolve_path(5) == 5
        assert config.resolve_path(["a", "/b", 3]) == [str(base / "a"), "/b", 3]
        assert isinstance(config.resolve_path("tests"), str)  # never a Path: options are data

    def test_no_config_file_anchors_to_the_working_directory(self, tmp_path, monkeypatch):
        """Built-in defaults have no file to anchor to (§8)."""
        monkeypatch.chdir(tmp_path)
        config = load_config(None)
        assert config.base_dir == Path(".")
        assert config.resolve_path("runcomposer.db") == "runcomposer.db"
        config.build(config.store_spec()).close()
        assert (tmp_path / "runcomposer.db").exists()

    def test_write_config_helper_and_load_agree_on_the_base(self, tmp_path):
        config = load_config(str(write_config(tmp_path)))
        assert config.base_dir == tmp_path
        assert Path(config.store_spec().options["path"]) == tmp_path / "runcomposer.db"
