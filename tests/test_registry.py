"""Plugin loading tests: import path + entry points (DESIGN.md §6)."""

import pytest

from runcomposer.core.registry import (
    RUNNER_GROUP,
    SOURCE_GROUP,
    PluginError,
    available_entry_points,
    load_import_path,
    resolve_plugin,
)
from runcomposer.plugins.demo_runner import DemoRunner
from runcomposer.plugins.manifest_source import ManifestSource


class TestImportPath:
    def test_loads_a_class_by_module_colon_attr(self):
        cls = load_import_path("runcomposer.plugins.manifest_source:ManifestSource")
        assert cls is ManifestSource

    @pytest.mark.parametrize(
        "ref",
        ["noseparator", "no.module:", ":NoModule", "does.not.exist:X", "runcomposer.cli:Missing"],
    )
    def test_bad_refs_raise_plugin_error(self, ref):
        with pytest.raises(PluginError):
            load_import_path(ref)


class TestEntryPoints:
    def test_reference_plugins_are_registered(self):
        assert available_entry_points(SOURCE_GROUP).get("manifest") == (
            "runcomposer.plugins.manifest_source:ManifestSource"
        )
        assert available_entry_points(RUNNER_GROUP).get("demo") == (
            "runcomposer.plugins.demo_runner:DemoRunner"
        )

    def test_resolve_plugin_by_entry_point_name(self):
        assert resolve_plugin("demo", RUNNER_GROUP) is DemoRunner

    def test_resolve_plugin_by_import_path(self):
        assert resolve_plugin("runcomposer.plugins.demo_runner:DemoRunner", RUNNER_GROUP) is DemoRunner

    def test_unknown_entry_point_raises(self):
        with pytest.raises(PluginError, match="no entry point"):
            resolve_plugin("nope", RUNNER_GROUP)


def test_plugins_satisfy_the_port_protocols():
    from importlib import resources

    from runcomposer.core.ports import Runner, TestSource

    source = ManifestSource(resources.files("runcomposer.demo") / "corpus.json")
    assert isinstance(source, TestSource)
    assert isinstance(DemoRunner(), Runner)
