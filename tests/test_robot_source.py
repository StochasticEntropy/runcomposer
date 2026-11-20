"""The `robotframework` TestSource (DESIGN.md §6.1): ids are Robot longnames,
several suite roots form one catalog, and the corpus can change under a
running process without a restart.

Needs Robot Framework (`pip install 'runcomposer[robot]'`) and skips cleanly
without it — the extra is a plugin dependency, never a core one.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from runcomposer.sdk import ConfigError, Selection, Service, load_config

from .conftest import ROBOT_SHOP, needs_robot, write_config

pytestmark = needs_robot


def robot_source(**options):
    from runcomposer.plugins.robot_source import RobotFrameworkSource

    return RobotFrameworkSource(**options)


def write_suite(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def two_roots(tmp_path: Path) -> tuple[Path, Path]:
    """Two sibling suite trees of the web shop: its API and its storefront."""
    for area, test in (("Api", "Cart Total Responds"), ("Storefront", "Login Works")):
        write_suite(
            tmp_path / area,
            "s.robot",
            f"*** Test Cases ***\n{test}\n    [Tags]    {area}    Smoke\n    Log    ok\n",
        )
    return tmp_path / "Api", tmp_path / "Storefront"


class TestItems:
    def test_ids_are_longnames_with_tags(self):
        items = {item.id: item for item in robot_source(root=str(ROBOT_SHOP)).items()}
        visa = items["Tests.Payments.Visa Payment Succeeds"]
        assert visa.name == "Visa Payment Succeeds"
        # area + sub-area + suite + sprint + ticket: the corpus carries the same
        # tag world as the bundled demo corpus (DESIGN.md §12).
        assert set(visa.tags) == {"Payments", "Payments-Cards", "Smoke", "Sprint-12", "SHOP-1200"}
        assert visa.hierarchy == ("Tests", "Payments")
        assert any("Quarantine-Flaky" in item.tags for item in items.values())

    def test_items_say_which_file_and_which_root_they_came_from(self):
        """What lets a runner execute each item from its own root."""
        source = robot_source(root=str(ROBOT_SHOP))
        visa = next(i for i in source.items() if i.id == "Tests.Payments.Visa Payment Succeeds")
        assert Path(visa.meta["source"]) == ROBOT_SHOP / "payments.robot"
        assert visa.meta["root"] == str(ROBOT_SHOP)

    def test_corpus_shape(self):
        """The shipped corpus is big enough to be worth demoing: every area
        file contributes, and the deliberate fixtures are all present."""
        items = list(robot_source(root=str(ROBOT_SHOP)).items())
        assert len(items) == 58
        assert {item.hierarchy[1] for item in items} == {
            "Account", "Auth", "Cart", "Catalog", "Checkout", "Payments",
        }
        assert len({tag for item in items for tag in item.tags}) == 50

        def by_tag(tag):
            return [item.id for item in items if tag in item.tags]

        assert len(by_tag("SlowLane")) == 2  # the sleeping pair (live status)
        assert by_tag("VarCheck") == ["Tests.Checkout.Stage Variable Reaches The Test"]
        assert len(by_tag("Quarantine-Flaky")) == 3

    def test_resolve_is_exact_plus_strip(self):
        source = robot_source(root=str(ROBOT_SHOP))
        longname = "Tests.Payments.Visa Payment Succeeds"
        assert source.resolve(longname) == longname
        assert source.resolve(f"  {longname}  ") == longname
        assert source.resolve("Tests.Payments.No Such Test") is None
        assert source.resolve(longname.lower()) is None  # ids are compared, not normalized


class TestSeveralRoots:
    def test_several_roots_form_one_catalog_and_keep_their_ids(self, tmp_path):
        """A corpus split over sibling suite trees is still one corpus — and
        each root names its own top-level suite, so the ids are exactly the
        ones a run over those roots reports back: no common-parent segment."""
        api, storefront = two_roots(tmp_path)
        source = robot_source(roots=[str(api), str(storefront)])
        assert [item.id for item in source.items()] == [
            "Api.S.Cart Total Responds",
            "Storefront.S.Login Works",
        ]
        assert {tag for item in source.items() for tag in item.tags} == {
            "Api", "Storefront", "Smoke",
        }
        assert source.duplicate_ids == []
        # Pointing Robot at the common parent instead is a different id space.
        parent = robot_source(root=str(tmp_path))
        assert all(item.id.count(".") == 3 for item in parent.items())

    def test_root_and_roots_combine(self, tmp_path):
        api, storefront = two_roots(tmp_path)
        source = robot_source(root=str(api), roots=[str(storefront)])
        assert len(source.items()) == 2
        assert [item.meta["root"] for item in source.items()] == [str(api), str(storefront)]

    def test_a_filter_reaches_across_roots(self, tmp_path):
        api, storefront = two_roots(tmp_path)
        selection = Selection.from_data({"tag_filter": "Smoke"})
        both = robot_source(roots=[str(api), str(storefront)])
        assert len(selection.compile(both.items())) == 2
        # …and a single root sees only its own half, which is the whole point.
        assert len(selection.compile(robot_source(root=str(api)).items())) == 1

    def test_two_tests_under_one_id_are_reported_not_raised(self, tmp_path):
        write_suite(
            tmp_path / "Api", "s.robot",
            "*** Test Cases ***\nSame\n    Log    ok\nSame\n    Log    ok\n",
        )
        source = robot_source(root=str(tmp_path / "Api"))
        assert len(source.items()) == 2
        assert source.duplicate_ids == ["Api.S.Same"]

    def test_the_same_tree_given_twice_is_all_duplicates(self, tmp_path):
        api, _ = two_roots(tmp_path)
        source = robot_source(roots=[str(api), str(api)])
        assert source.duplicate_ids == ["Api.S.Cart Total Responds"]


class TestRefusals:
    def test_a_missing_root_is_a_config_error_naming_it(self, tmp_path):
        api, _ = two_roots(tmp_path)
        with pytest.raises(ConfigError, match="Gone"):
            robot_source(roots=[str(api), str(tmp_path / "Gone")])

    def test_a_source_without_any_root_fails_loudly(self):
        with pytest.raises(ConfigError, match="'root' or 'roots'"):
            robot_source()

    def test_roots_must_be_a_list(self, tmp_path):
        with pytest.raises(ConfigError, match="must be a list"):
            robot_source(roots=str(tmp_path))

    def test_a_missing_root_refuses_startup_naming_the_config_entry(self, tmp_path):
        config = write_config(tmp_path, sources={"robotframework": {"root": "nowhere"}})
        with Service(load_config(str(config))) as service, pytest.raises(
            ConfigError, match=r"sources\.robotframework.*nowhere"
        ):
            service.validate()


class TestSnapshotAndFingerprint:
    def test_snapshot_is_stable_and_changes_when_the_corpus_changes(self, tmp_path):
        copy = tmp_path / "tests"
        shutil.copytree(ROBOT_SHOP, copy)
        before = robot_source(root=str(copy)).snapshot()
        assert before.startswith("sha256:")
        assert robot_source(root=str(copy)).snapshot() == before  # a re-parse is the same catalog
        with open(copy / "payments.robot", "a", encoding="utf-8") as handle:
            handle.write("\nAdded Test\n    Log    new\n")
        assert robot_source(root=str(copy)).snapshot() != before

    def test_snapshot_is_about_ids_and_tags_not_about_test_bodies(self, tmp_path):
        """Editing what a test *does* is not drift: a selection depends on ids
        and tags, and so does the snapshot."""
        suite = write_suite(
            tmp_path / "Api", "s.robot",
            "*** Test Cases ***\nCart Total Responds\n    [Tags]    Cart\n    Log    ok\n",
        )
        before = robot_source(root=str(tmp_path / "Api")).snapshot()
        suite.write_text(suite.read_text(encoding="utf-8").replace("Log    ok", "Log    fine"), "utf-8")
        assert robot_source(root=str(tmp_path / "Api")).snapshot() == before
        suite.write_text(suite.read_text(encoding="utf-8").replace("Cart\n", "Cart    Smoke\n"), "utf-8")
        assert robot_source(root=str(tmp_path / "Api")).snapshot() != before

    def test_fingerprint_changes_when_a_suite_file_changes_and_not_otherwise(self, tmp_path):
        api, _ = two_roots(tmp_path)
        source = robot_source(root=str(api))
        first = source.fingerprint()
        assert source.fingerprint() == first  # asking twice changes nothing
        assert robot_source(root=str(api)).fingerprint() == first  # nor does re-parsing

        # A file that is not part of a suite tree does not count…
        (api / "notes.md").write_text("not a suite", encoding="utf-8")
        assert source.fingerprint() == first

        # …a touched suite file does (size or modification time)…
        suite = api / "s.robot"
        stat = suite.stat()
        os.utime(suite, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        touched = source.fingerprint()
        assert touched != first

        # …and so does a new one, or a removed one.
        added = write_suite(api, "more.robot", "*** Test Cases ***\nAnother\n    Log    ok\n")
        with_added = source.fingerprint()
        assert with_added not in (first, touched)
        added.unlink()
        assert source.fingerprint() == touched

    def test_fingerprint_costs_no_parse(self, tmp_path, monkeypatch):
        """It is the cheap question; the catalog asks it often."""
        api, _ = two_roots(tmp_path)
        source = robot_source(root=str(api))
        import robot.api

        def no_parsing(*_args, **_kwargs):
            raise AssertionError("fingerprint() must not build suites")

        monkeypatch.setattr(robot.api, "TestSuiteBuilder", no_parsing)
        assert source.fingerprint()


class TestThroughConfiguration:
    def test_relative_roots_resolve_against_the_config_file(self, tmp_path, monkeypatch):
        """§8: the same `--config` sees the same corpus from any working
        directory — both spellings, `root` and `roots`."""
        deployment = tmp_path / "deployment"
        shutil.copytree(ROBOT_SHOP, deployment / "suite" / "tests")
        write_suite(
            deployment / "extra" / "Api", "s.robot",
            "*** Test Cases ***\nCart Total Responds\n    [Tags]    Cart\n    Log    ok\n",
        )
        deployment.mkdir(exist_ok=True)
        config = write_config(
            deployment,
            sources={"robotframework": {"root": "suite/tests", "roots": ["extra/Api"]}},
        )
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        with Service(load_config(str(config))) as service:
            ids = {item.id for item in service.catalog.items()}
        assert "Tests.Payments.Visa Payment Succeeds" in ids
        assert "Api.S.Cart Total Responds" in ids
        assert list(elsewhere.iterdir()) == []  # nothing landed in the working directory

    def test_an_absolute_root_is_left_as_written(self, tmp_path):
        config = write_config(tmp_path, sources={"robotframework": {"root": str(ROBOT_SHOP)}})
        with Service(load_config(str(config))) as service:
            assert service.catalog.stats()["items"] == 58
            assert service.catalog.provider_id == "robotframework"

    def test_an_edited_corpus_is_seen_without_a_restart(self, tmp_path):
        """Regression: the catalog used to be frozen at process start. The
        source's fingerprint is what lets the catalog notice on its own."""
        from runcomposer.app.catalog import Catalog

        api, _ = two_roots(tmp_path)
        config = load_config(str(write_config(tmp_path, sources={"robotframework": {"root": "Api"}})))
        catalog = Catalog(config, check_interval_s=0.0)  # ask the fingerprint on every use
        assert [item.id for item in catalog.items()] == ["Api.S.Cart Total Responds"]
        before = catalog.snapshot()

        write_suite(api, "more.robot", "*** Test Cases ***\nCart Is Emptied\n    [Tags]    Cart\n    Log    ok\n")
        assert [item.id for item in catalog.items()] == [
            "Api.More.Cart Is Emptied",
            "Api.S.Cart Total Responds",
        ]
        assert catalog.snapshot() != before
        assert catalog.resolve("Api.More.Cart Is Emptied") == "Api.More.Cart Is Emptied"

    def test_two_robot_sources_under_different_keys_merge(self, tmp_path):
        """Several `sources:` entries of one plugin: one catalog, one id space."""
        api, storefront = two_roots(tmp_path)
        config = write_config(
            tmp_path,
            sources={
                "api": {"plugin": "robotframework", "root": str(api)},
                "storefront": {"plugin": "robotframework", "root": str(storefront)},
            },
        )
        with Service(load_config(str(config))) as service:
            assert sorted(item.id for item in service.catalog.items()) == [
                "Api.S.Cart Total Responds",
                "Storefront.S.Login Works",
            ]
            assert service.catalog.resolve("Storefront.S.Login Works") == "Storefront.S.Login Works"
            assert service.catalog.stats()["sources"] == ["api", "storefront"]
