"""Config tests (DESIGN.md §8): core-only validation, loud plugin failures,
zero-config defaults."""

import pytest

from runcomposer.config import ConfigError, load_config
from runcomposer.plugins.manifest_source import ManifestSource
from runcomposer.plugins.sqlite_store import SqliteRunStore


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
