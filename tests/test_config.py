"""Config tests (DESIGN.md §8): core-only validation, loud plugin failures,
zero-config defaults, one path base."""

import json
from pathlib import Path

import pytest

from runcomposer.config import ConfigError, load_config
from runcomposer.plugins.manifest_source import ManifestSource
from runcomposer.plugins.sqlite_store import SqliteRunStore

THIS_MODULE = Path(__file__).stem


class LegacyStore:
    """A third-party store written against 0.1.0: it knows nothing about
    ``resolve_config_paths``, so the core must hand it its options verbatim."""

    def __init__(self, path: str = "legacy.db"):
        self.path = path


class TestDefaults:
    def test_no_config_file_yields_working_defaults(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # no ./config.yaml here
        config = load_config(None)
        assert config.path is None
        assert config.api["port"] == 8100
        assert config.locale_default == "en"
        source = config.build_source()
        assert isinstance(source, ManifestSource)
        assert len(source.items()) == 60  # bundled demo corpus

    def test_default_store_is_sqlite(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        store = load_config(None).build_store()
        assert isinstance(store, SqliteRunStore)
        assert (tmp_path / "runcomposer.db").exists()


class TestLoading:
    def test_config_file_sections_are_applied(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            f"""
core:
  api: {{ port: 9000 }}
  locale_default: de
store:
  sqlite: {{ path: {tmp_path / 'x.db'} }}
ui:
  quick_filters: [Payments]
""",
            encoding="utf-8",
        )
        config = load_config(str(config_file))
        assert config.api["port"] == 9000
        assert config.api["host"] == "127.0.0.1"  # defaults merged
        assert config.locale_default == "de"
        assert config.ui == {"quick_filters": ["Payments"]}
        assert isinstance(config.build_store(), SqliteRunStore)

    def test_missing_explicit_config_fails(self):
        with pytest.raises(ConfigError, match="not found"):
            load_config("does-not-exist.yaml")

    def test_unknown_top_level_section_fails_loudly(self, tmp_path):
        bad = tmp_path / "config.yaml"
        bad.write_text("stroe: {sqlite: {}}\n", encoding="utf-8")  # typo
        with pytest.raises(ConfigError, match="stroe"):
            load_config(str(bad))

    def test_unknown_core_key_fails_loudly(self, tmp_path):
        bad = tmp_path / "config.yaml"
        bad.write_text("core: {taxonomyfile: x}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="taxonomyfile"):
            load_config(str(bad))


class TestPluginResolution:
    def test_unknown_active_plugin_id_fails_loudly(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("store: {mongodb: {}}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="mongodb"):
            load_config(str(config_file)).build_store()

    def test_import_path_module_declaration(self, tmp_path):
        """The hack-it-in-an-afternoon path: module: pkg.mod:Class (§6)."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            f"""
store:
  my-store:
    module: "runcomposer.plugins.sqlite_store:SqliteRunStore"
    path: {tmp_path / 'custom.db'}
""",
            encoding="utf-8",
        )
        store = load_config(str(config_file)).build_store()
        assert isinstance(store, SqliteRunStore)
        assert (tmp_path / "custom.db").exists()

    def test_two_active_stores_is_an_error(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("store: {sqlite: {}, other: {}}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="exactly one"):
            load_config(str(config_file)).build_store()


class TestOnePathBase:
    """§8: *every* relative path in a config file resolves against the config
    file's directory — the core's own keys and the plugin sections alike.

    The regression: they used to resolve against two different bases, so the
    same ``--config`` invoked from two directories silently built a second,
    empty database and read a source root that was not there.
    """

    def _config(self, tmp_path, body: str) -> Path:
        env = tmp_path / "envs" / "staging"
        env.mkdir(parents=True)
        config_file = env / "config.yaml"
        config_file.write_text(body, encoding="utf-8")
        return config_file

    def test_same_config_from_two_directories_is_one_database(self, tmp_path, monkeypatch):
        from runcomposer.service import Service

        config_file = self._config(tmp_path, "store: {sqlite: {path: staging.db}}\n")
        here, there = tmp_path / "here", tmp_path / "there"
        here.mkdir()
        there.mkdir()

        monkeypatch.chdir(here)
        composed = Service(load_config(str(config_file))).compose_run(
            {"tag_filter": "Payments"}, title="Composed from here", origin="test"
        )
        monkeypatch.chdir(there)
        from_there = Service(load_config(str(config_file))).store.list_runs()

        assert [run.title for run in from_there] == ["Composed from here"]
        assert from_there[0].id == composed.run.id
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

        source = load_config(str(config_file)).build_source()
        assert [item.id for item in source.items()] == ["Shop.Payments.T001"]

    def test_runner_paths_resolve_too_and_non_paths_do_not(self, tmp_path, monkeypatch):
        """``suite_root``/``output_root`` are directories; a Robot listener
        spec and a shell hook are not, and the plugin says which is which."""
        config_file = self._config(
            tmp_path,
            "runners:\n"
            "  robot-pool:\n"
            "    suite_root: tests\n"
            "    output_root: /var/lib/runcomposer/artifacts\n"
            "    listener: MyListener:arg\n"
            "    pre_run_hooks: ['echo ready']\n",
        )
        monkeypatch.chdir(tmp_path)

        options = load_config(str(config_file)).runner_options("robot-pool")
        assert Path(options["suite_root"]) == config_file.parent / "tests"
        assert options["output_root"] == "/var/lib/runcomposer/artifacts"  # absolute: untouched
        assert options["listener"] == "MyListener:arg"  # not a path
        assert options["pre_run_hooks"] == ["echo ready"]  # not a path

    def test_core_and_plugin_keys_share_one_base(self, tmp_path, monkeypatch):
        config_file = self._config(
            tmp_path,
            "core:\n"
            "  artifact_dir: artifacts\n"
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
        config.build_store()
        assert (base / "staging.db").exists()

    def test_a_path_that_is_not_a_path_survives(self, tmp_path, monkeypatch):
        """``:memory:`` names no file on disk. Only the store can know that,
        which is why resolution is the plugin's decision, not the core's."""
        config_file = self._config(tmp_path, "store: {sqlite: {path: ':memory:'}}\n")
        monkeypatch.chdir(tmp_path)
        store = load_config(str(config_file)).build_store()
        assert store._path == ":memory:"
        assert not (config_file.parent / ":memory:").exists()

    def test_absolute_paths_are_untouched(self, tmp_path, monkeypatch):
        absolute = tmp_path / "somewhere" / "abs.db"
        absolute.parent.mkdir()
        config_file = self._config(tmp_path, f"store: {{sqlite: {{path: {absolute}}}}}\n")
        monkeypatch.chdir(tmp_path)
        load_config(str(config_file)).build_store()
        assert absolute.exists()

    def test_plugin_without_the_hook_keeps_0_1_0_behaviour(self, tmp_path, monkeypatch):
        """Opt-in: a third-party plugin that never heard of the hook gets its
        options exactly as written, so nothing written against 0.1.0 moves."""
        config_file = self._config(
            tmp_path,
            f"store:\n  legacy:\n    module: '{THIS_MODULE}:LegacyStore'\n    path: legacy.db\n",
        )
        monkeypatch.chdir(tmp_path)
        store = load_config(str(config_file)).build_store()
        assert store.path == "legacy.db"

    def test_no_config_file_still_anchors_to_the_working_directory(self, tmp_path, monkeypatch):
        """Built-in defaults have no file to anchor to (§8)."""
        monkeypatch.chdir(tmp_path)
        config = load_config(None)
        assert config.base_dir == Path(".")
        assert config.resolve_path("runcomposer.db") == "runcomposer.db"
        config.build_store()
        assert (tmp_path / "runcomposer.db").exists()
