"""The plugin contract is declared, checked at startup, and then called
directly (DESIGN.md §6, §8).

Nothing of a plugin is discovered by attribute convention: every member the
application calls is a member of a protocol in ``runcomposer.sdk``, the base
classes there supply the optional ones, a plugin that lacks a member is
refused at startup with the member named — and a guard keeps the layers above
the core from probing plugin objects with ``getattr`` / ``hasattr``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import runcomposer
from runcomposer.sdk import (
    ConfigError,
    Configurable,
    Health,
    Item,
    ObserverBase,
    ParserBase,
    PluginBase,
    ResultParser,
    Runner,
    RunnerBase,
    RunnerInfo,
    RunObserver,
    RunStore,
    Service,
    SourceBase,
    StoreBase,
    TestSource,
    contract_members,
    load_config,
)

from .conftest import write_config

HERE = "tests.test_contract"
PACKAGE_DIR = Path(runcomposer.__file__).parent


# -- plugins that fall short of the contract, each in one way ---------------------


class SourceWithoutFingerprint(PluginBase):
    """Every required TestSource member, but not ``fingerprint`` /
    ``duplicate_ids`` — and not derived from SourceBase, which has them."""

    provider_id = "bare"

    def items(self) -> list[Item]:
        return [Item(id="Shop.Cart.Core.T001", tags=("Cart",))]

    def snapshot(self) -> str:
        return "sha256:0"

    def resolve(self, native_name: str) -> str | None:
        return native_name


class RunnerWithoutCancel(PluginBase):
    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="bare")

    def dispatch(self, ctx: Any) -> None:
        ctx.refuse("never")

    def health(self) -> Health:
        return Health(status="ok")


class PlainRunner:
    """Not derived from any base: lacks the configuration members too."""

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="plain")

    def dispatch(self, ctx: Any) -> None:
        ctx.refuse("never")

    def cancel(self, ctx: Any) -> None:
        pass

    def health(self) -> Health:
        return Health()


class StoreWithoutLatestEvents(StoreBase):
    store_id = "half"

    def __init__(self, path: str = "x.db"):
        self.path = path


class ObserverWithoutNotify(PluginBase):
    pass


class CompleteRunner(RunnerBase):
    """Derived from RunnerBase, defines only the two required members."""

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="complete")

    def dispatch(self, ctx: Any) -> None:
        ctx.refuse("not today")


class TestStartupChecksTheContract:
    def build(self, tmp_path: Path, **sections: Any) -> Service:
        service = Service(load_config(str(write_config(tmp_path, **sections))))
        try:
            service.validate()
        finally:
            service.close()
        return service

    def test_a_source_without_the_optional_members_is_refused_naming_them(self, tmp_path):
        with pytest.raises(ConfigError) as caught:
            self.build(tmp_path, sources={"bare": {"module": f"{HERE}:SourceWithoutFingerprint"}})
        message = str(caught.value)
        assert "sources.bare" in message and "SourceWithoutFingerprint" in message
        assert "'fingerprint'" in message and "'duplicate_ids'" in message
        assert "TestSource contract" in message and "runcomposer.sdk.SourceBase" in message

    def test_a_runner_without_cancel_is_refused(self, tmp_path):
        with pytest.raises(ConfigError, match=r"runners\.bare: RunnerWithoutCancel does not provide \['cancel'\]"):
            self.build(tmp_path, runners={"bare": {"module": f"{HERE}:RunnerWithoutCancel"}})

    def test_a_class_without_the_configuration_members_is_refused_before_it_is_built(self, tmp_path):
        with pytest.raises(ConfigError) as caught:
            self.build(tmp_path, runners={"plain": {"module": f"{HERE}:PlainRunner"}})
        message = str(caught.value)
        assert "'resolve_config_paths'" in message and "'OPTIONS'" in message and "'CONNECTOR'" in message
        assert "Configurable contract" in message

    def test_a_store_missing_one_member_is_refused_naming_exactly_that_member(self, tmp_path):
        with pytest.raises(ConfigError) as caught:
            self.build(tmp_path, store={"half": {"module": f"{HERE}:StoreWithoutLatestEvents"}})
        assert "StoreWithoutLatestEvents does not provide [" in str(caught.value)
        assert "'latest_events'" in str(caught.value)

    def test_an_observer_without_notify_is_refused(self, tmp_path):
        with pytest.raises(ConfigError, match=r"\['notify'\] of the RunObserver contract"):
            self.build(tmp_path, observers={"mute": {"module": f"{HERE}:ObserverWithoutNotify"}})

    def test_a_runner_derived_from_the_base_class_needs_only_its_own_two_members(self, tmp_path):
        service = Service(load_config(str(write_config(
            tmp_path, runners={"complete": {"module": f"{HERE}:CompleteRunner"}}
        ))))
        with service:
            service.validate()
            assert service.runner_health("complete").status == "unknown"  # RunnerBase.health


class TestTheContractIsData:
    def test_each_role_lists_its_members(self):
        assert contract_members(Runner) == ("describe", "dispatch", "cancel", "health")
        assert set(contract_members(TestSource)) == {
            "provider_id", "items", "snapshot", "resolve", "fingerprint", "duplicate_ids",
        }
        assert set(contract_members(ResultParser)) == {"format_id", "parse"}
        assert contract_members(RunObserver) == ("notify",)
        assert set(contract_members(Configurable)) == {"OPTIONS", "CONNECTOR", "resolve_config_paths"}
        assert {"close", "latest_events", "add_dispatch", "store_id"} <= set(contract_members(RunStore))

    def test_the_base_classes_supply_every_optional_member(self):
        assert SourceBase().fingerprint() is None and tuple(SourceBase.duplicate_ids) == ()
        assert RunnerBase().health() == Health()
        assert RunnerBase().cancel(None) is None  # type: ignore[arg-type]
        assert StoreBase().close() is None
        for base in (SourceBase, RunnerBase, ParserBase, StoreBase, ObserverBase):
            assert issubclass(base, PluginBase)
            assert base.OPTIONS is None and base.PATH_OPTIONS == ()

    def test_path_options_are_resolved_by_the_base_class_default(self):
        class Pathy(PluginBase):
            PATH_OPTIONS = ("root", "roots")

        resolved = Pathy.resolve_config_paths(
            {"root": "a", "roots": ["b", "c"], "url": "d"}, lambda value: ("@", value)
        )
        assert resolved == {"root": ("@", "a"), "roots": ("@", ["b", "c"]), "url": "d"}


# -- the guard: the layers above the core do not probe plugin objects ---------------

#: Every ``getattr`` / ``hasattr`` / ``getattr_static`` call permitted in the
#: application, transport and command-line layers and in the core, as
#: ``(file, first argument)``. None of them touches a plugin object: argparse
#: namespaces, exceptions, the web framework's request state, the logging
#: module, import-path and entry-point resolution — and the one place the
#: contract itself is checked (``missing_members``), which reads the member
#: list from the protocol rather than guessing it. Adding an entry here is a
#: design decision; a plugin member the application needs belongs in its
#: protocol instead.
ALLOWED_PROBES = {
    ("cli/admin.py", "getattr", "logging"),
    ("cli/common.py", "getattr", "args"),
    ("http/errors.py", "getattr", "exc"),
    ("http/errors.py", "getattr", "request.app.state"),
    ("core/registry.py", "getattr", "module"),
    ("core/registry.py", "getattr", "ep"),
    ("core/ports.py", "getattr_static", "plugin"),
}
_PROBES = {"getattr", "hasattr", "getattr_static"}


def probes_in(source: str) -> list[tuple[str, str, int]]:
    """``(function, first argument, line)`` for every attribute probe."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name in _PROBES:
            found.append((name, ast.unparse(node.args[0]), node.lineno))
    return found


def test_the_probe_scanner_sees_every_spelling():
    source = (
        "getattr(runner, 'cancel', None)\n"
        "x = hasattr(self.store, 'close')\n"
        "inspect.getattr_static(source, 'fingerprint')\n"
        "builtins.getattr(spec.cls, 'OPTIONS')\n"
    )
    assert probes_in(source) == [
        ("getattr", "runner", 1),
        ("hasattr", "self.store", 2),
        ("getattr_static", "source", 3),
        ("getattr", "spec.cls", 4),
    ]


def test_no_layer_probes_plugin_objects_by_attribute():
    offending = []
    for layer in ("app", "http", "cli", "core"):
        for path in sorted((PACKAGE_DIR / layer).rglob("*.py")):
            relative = path.relative_to(PACKAGE_DIR).as_posix()
            for name, target, line in probes_in(path.read_text(encoding="utf-8")):
                if (relative, name, target) not in ALLOWED_PROBES:
                    offending.append(f"{relative}:{line}: {name}({target}, …)")
    assert not offending, (
        "attribute probes outside the allowed list — a plugin member the application "
        "needs is declared in its protocol (runcomposer.core.ports) with a default in the "
        f"base class, and called directly: {offending}"
    )


# -- connectors: — settings a connector package's plugins share ---------------------


class ShopSettingsRunner(RunnerBase):
    CONNECTOR = "shop"

    def __init__(self, region: str = "eu", connector_settings: dict[str, Any] | None = None):
        self.region = region
        self.settings = dict(connector_settings or {})

    def describe(self) -> RunnerInfo:
        return RunnerInfo(id="shop-settings")

    def dispatch(self, ctx: Any) -> None:
        ctx.refuse("not today")


class ForgetfulSettingsRunner(CompleteRunner):
    CONNECTOR = "shop"


class TestConnectorSettings:
    def service(self, tmp_path: Path, **sections: Any) -> Service:
        return Service(load_config(str(write_config(tmp_path, **sections))))

    def test_a_plugin_that_declares_connector_receives_that_mapping(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHOP_API_KEY", "k-123456")
        with self.service(tmp_path, runners={"shop": {"module": f"{HERE}:ShopSettingsRunner", "region": "us"}},
                          connectors={"shop": {"base_url": "https://shop.example", "api_key": "${env:SHOP_API_KEY}"},
                                      "other": {"ignored": True}}) as service:
            runner = service.runner("shop")
            assert runner.region == "us"
            assert runner.settings == {"base_url": "https://shop.example", "api_key": "k-123456"}
            assert service.redact("key k-123456") == "key ‹from the environment›"

    def test_no_mapping_is_an_empty_one(self, tmp_path):
        with self.service(tmp_path, runners={"shop": {"module": f"{HERE}:ShopSettingsRunner"}}) as service:
            assert service.runner("shop").settings == {}

    def test_the_keyword_is_not_an_option_anyone_writes(self, tmp_path):
        with pytest.raises(ConfigError, match="'connector_settings' is not an option you write"):
            self.service(tmp_path, runners={"shop": {"module": f"{HERE}:ShopSettingsRunner",
                                                     "connector_settings": {}}}).validate()

    def test_declaring_connector_without_taking_the_keyword_is_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="declares CONNECTOR = 'shop' but its constructor takes no"):
            self.service(tmp_path, runners={"shop": {"module": f"{HERE}:ForgetfulSettingsRunner"}}).validate()
