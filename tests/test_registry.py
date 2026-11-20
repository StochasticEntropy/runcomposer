"""Plugin loading (DESIGN.md §6): import paths and entry points — the two
first-class mechanisms — and the inventory of what is installed."""

from __future__ import annotations

import pytest

import runcomposer
from runcomposer.core.errors import ConfigError
from runcomposer.core.registry import (
    GROUPS,
    OBSERVER_GROUP,
    PARSER_GROUP,
    RUNNER_GROUP,
    SOURCE_GROUP,
    STORE_GROUP,
    PluginError,
    PluginInfo,
    installed_plugins,
    load_entry_point,
    load_import_path,
)
from runcomposer.plugins.demo_runner import DemoRunner
from runcomposer.plugins.manifest_source import ManifestSource


class TestImportPath:
    def test_loads_a_class_by_module_colon_attr(self):
        cls = load_import_path("runcomposer.plugins.manifest_source:ManifestSource")
        assert cls is ManifestSource

    @pytest.mark.parametrize(
        ("ref", "fragment"),
        [
            ("noseparator", "must look like 'package.module:ClassName'"),
            ("runcomposer.plugins.demo_runner.DemoRunner", "must look like"),  # the dotted form
            ("no.module:", "must look like"),
            (":NoModule", "must look like"),
            ("does.not.exist:X", "cannot import plugin module 'does.not.exist'"),
            ("runcomposer.cli:Missing", "module 'runcomposer.cli' has no attribute 'Missing'"),
        ],
    )
    def test_bad_refs_raise_plugin_error_saying_what_is_wrong(self, ref, fragment):
        with pytest.raises(PluginError, match=fragment):
            load_import_path(ref)

    def test_a_plugin_error_is_a_config_error(self):
        """An unloadable plugin is a deployment mistake, reported like one."""
        assert issubclass(PluginError, ConfigError)
        assert PluginError.code == "plugin-error"


class TestEntryPoints:
    def test_load_by_group_and_name(self):
        assert load_entry_point(RUNNER_GROUP, "demo") is DemoRunner
        assert load_entry_point(SOURCE_GROUP, "manifest") is ManifestSource

    def test_an_unknown_name_lists_what_is_installed(self):
        with pytest.raises(PluginError, match="no entry point 'nope'") as caught:
            load_entry_point(RUNNER_GROUP, "nope")
        message = str(caught.value)
        assert RUNNER_GROUP in message
        assert "demo" in message and "robot-pool" in message

    def test_names_are_per_group(self):
        """`command` is a runner and an observer — two plugins, one name."""
        runner = load_entry_point(RUNNER_GROUP, "command")
        observer = load_entry_point(OBSERVER_GROUP, "command")
        assert runner is not observer
        with pytest.raises(PluginError):
            load_entry_point(STORE_GROUP, "demo")

    def test_the_five_groups(self):
        assert GROUPS == (SOURCE_GROUP, RUNNER_GROUP, STORE_GROUP, PARSER_GROUP, OBSERVER_GROUP)
        assert all(group.startswith("runcomposer.") for group in GROUPS)


class TestInstalledPlugins:
    def test_the_reference_connectors_are_registered(self):
        names = {(plugin.group, plugin.name) for plugin in installed_plugins()}
        expected = {
            (SOURCE_GROUP, "manifest"), (SOURCE_GROUP, "robotframework"), (SOURCE_GROUP, "demo-corpus"),
            (RUNNER_GROUP, "demo"), (RUNNER_GROUP, "robot-pool"), (RUNNER_GROUP, "jenkins"),
            (RUNNER_GROUP, "command"),
            (STORE_GROUP, "sqlite"),
            (PARSER_GROUP, "runcomposer-verdicts"), (PARSER_GROUP, "robot-output-xml"),
            (PARSER_GROUP, "junit-xml"),
            (OBSERVER_GROUP, "webhook"), (OBSERVER_GROUP, "jsonl"), (OBSERVER_GROUP, "command"),
        }
        assert expected <= names

    def test_the_replaced_runner_id_is_gone(self):
        assert (RUNNER_GROUP, "ci-trigger") not in {
            (plugin.group, plugin.name) for plugin in installed_plugins()
        }

    def test_each_plugin_says_which_distribution_provides_it_at_which_version(self):
        demo = next(
            plugin for plugin in installed_plugins(RUNNER_GROUP) if plugin.name == "demo"
        )
        assert demo == PluginInfo(
            group=RUNNER_GROUP,
            name="demo",
            target="runcomposer.plugins.demo_runner:DemoRunner",
            distribution="runcomposer",
            version=runcomposer.__version__,
        )

    def test_one_group_or_all_sorted(self):
        runners = installed_plugins(RUNNER_GROUP)
        assert {plugin.group for plugin in runners} == {RUNNER_GROUP}
        everything = installed_plugins()
        assert everything == sorted(everything, key=lambda plugin: (plugin.group, plugin.name))
        assert len(everything) > len(runners)

    def test_every_registered_target_is_loadable(self):
        """An entry point that cannot be imported would only show at the
        first use; the reference set must all load (Robot ones need not be
        *constructible* without the extra — importing them is enough)."""
        for plugin in installed_plugins():
            if plugin.distribution == "runcomposer":
                assert callable(load_entry_point(plugin.group, plugin.name)), plugin
