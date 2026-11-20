"""app.catalog.Catalog (DESIGN.md §6.1, §8): every configured TestSource as
one id space, kept current without a restart."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from runcomposer.app.catalog import Catalog
from runcomposer.app.config import load_config
from runcomposer.sdk import ConfigError, Item, SourceBase, canonical_json, sha256_hex

from .conftest import compose, write_config

HERE = "tests.test_catalog"


class CountingSource(SourceBase):
    """A source whose ``fingerprint`` is the default ``None``: it cannot say whether it changed.
    It reads a JSON list of ids on construction and counts its constructions."""

    provider_id = "counting"
    PATH_OPTIONS = ("path",)
    built = 0

    def __init__(self, path: str):
        type(self).built += 1
        self._ids = json.loads(Path(path).read_text(encoding="utf-8"))

    def items(self) -> list[Item]:
        return [Item(id=item_id, tags=("Checkout",)) for item_id in self._ids]

    def snapshot(self) -> str:
        return "sha256:" + sha256_hex(canonical_json(self._ids))

    def resolve(self, native_name: str) -> str | None:
        return native_name if native_name in self._ids else None


class AliasSource(CountingSource):
    """Resolves upper-cased spellings too — its own native-name quirk."""

    provider_id = "aliasing"

    def resolve(self, native_name: str) -> str | None:
        for item_id in self._ids:
            if native_name in (item_id, item_id.upper()):
                return item_id
        return None


class BrokenProbeSource(CountingSource):
    def fingerprint(self) -> str:
        raise OSError("the corpus share is unmounted")


class EditedWhileReadSource(CountingSource):
    """A fingerprinted source whose corpus can be edited *while it is being
    read*: ``during_read`` runs right after the constructor read the file."""

    during_read: Any = None

    def __init__(self, path: str):
        self._path = path
        super().__init__(path)
        hook, type(self).during_read = type(self).during_read, None
        if hook is not None:
            hook()

    def fingerprint(self) -> str:
        stat = os.stat(self._path)
        return f"{stat.st_size}:{stat.st_mtime_ns}"


def manifest(path: Path, items: list[dict[str, Any]]) -> Path:
    """Write a manifest so that the file's fingerprint (size + mtime) is
    certain to differ from the previous write, whatever the clock's
    resolution."""
    previous = path.stat().st_mtime_ns if path.exists() else 0
    path.write_text(json.dumps({"items": items}), encoding="utf-8")
    bumped = max(path.stat().st_mtime_ns, previous + 1_000_000_000)
    os.utime(path, ns=(bumped, bumped))
    return path


def item(item_id: str, *tags: str) -> dict[str, Any]:
    return {"id": item_id, "tags": list(tags)}


PAYMENTS = [item("Shop.Payments.Cards.T001", "Payments", "Smoke"),
            item("Shop.Payments.Cards.T002", "Payments")]
CHECKOUT = [item("Shop.Checkout.Standard.T001", "Checkout", "Smoke")]


def catalog_for(tmp_path: Path, sources: dict[str, Any], **kwargs: Any) -> Catalog:
    config = load_config(str(write_config(tmp_path, sources=sources)))
    kwargs.setdefault("check_interval_s", 0)
    return Catalog(config, **kwargs)


@pytest.fixture(autouse=True)
def _reset_counters():
    for cls in (CountingSource, AliasSource, BrokenProbeSource, EditedWhileReadSource):
        cls.built = 0
    EditedWhileReadSource.during_read = None


class TestOneSource:
    def test_it_is_that_source(self, tmp_path):
        manifest(tmp_path / "payments.json", PAYMENTS)
        catalog = catalog_for(tmp_path, {"manifest": {"path": "payments.json"}})
        direct = load_config(str(tmp_path / "config.yaml"))
        source = direct.build(direct.source_specs()[0])
        assert [i.id for i in catalog.items()] == [i.id for i in source.items()]
        assert catalog.snapshot() == source.snapshot()  # verbatim, not re-hashed
        assert catalog.provider_id == "manifest"
        assert catalog.duplicate_ids == []
        assert catalog.item_ids() == frozenset(i["id"] for i in PAYMENTS)

    def test_the_bundled_corpus_is_the_default_catalog(self, service):
        assert len(service.catalog.items()) == 60
        assert service.catalog.provider_id == "manifest"
        assert service.source is service.catalog  # the same object under its role's name

    def test_nothing_is_loaded_until_it_is_asked_for(self, tmp_path):
        (tmp_path / "ids.json").write_text('["Shop.Cart.Core.T001"]', encoding="utf-8")
        catalog = catalog_for(tmp_path, {"c": {"module": f"{HERE}:CountingSource", "path": "ids.json"}})
        assert CountingSource.built == 0 and catalog.loaded_at is None
        catalog.items()
        assert CountingSource.built == 1 and catalog.loaded_at is not None


class TestSeveralSources:
    @pytest.fixture()
    def merged(self, tmp_path):
        manifest(tmp_path / "payments.json", PAYMENTS)
        manifest(tmp_path / "checkout.json", CHECKOUT)
        return catalog_for(
            tmp_path,
            {"payments": {"plugin": "manifest", "path": "payments.json"},
             "checkout": {"plugin": "manifest", "path": "checkout.json"}},
        )

    def test_items_merge_in_configuration_order(self, merged):
        assert [i.id for i in merged.items()] == [
            "Shop.Payments.Cards.T001", "Shop.Payments.Cards.T002", "Shop.Checkout.Standard.T001",
        ]
        assert merged.tag_counts() == {"Payments": 2, "Smoke": 2, "Checkout": 1}

    def test_the_snapshot_covers_every_source(self, merged, tmp_path):
        before = merged.snapshot()
        assert before.startswith("sha256:")
        manifest(tmp_path / "checkout.json", [*CHECKOUT, item("Shop.Checkout.Gift.T001", "Checkout")])
        after_checkout = merged.snapshot()
        assert after_checkout != before
        manifest(tmp_path / "payments.json", PAYMENTS[:1])
        assert merged.snapshot() not in (before, after_checkout)

    def test_the_snapshot_is_stable_while_nothing_changes(self, merged):
        assert merged.snapshot() == merged.snapshot()

    def test_the_snapshot_is_not_any_single_sources(self, merged, tmp_path):
        config = load_config(str(tmp_path / "config.yaml"))
        singles = {config.build(spec).snapshot() for spec in config.source_specs()}
        assert merged.snapshot() not in singles

    def test_provider_ids_join(self, tmp_path):
        manifest(tmp_path / "payments.json", PAYMENTS)
        (tmp_path / "ids.json").write_text('["Shop.Cart.Core.T001"]', encoding="utf-8")
        catalog = catalog_for(
            tmp_path,
            {"payments": {"plugin": "manifest", "path": "payments.json"},
             "more": {"plugin": "manifest", "path": "payments.json"},
             "cart": {"module": f"{HERE}:CountingSource", "path": "ids.json"}},
        )
        assert catalog.provider_id == "manifest+counting"  # each provider once, in order

    def test_an_id_in_two_sources_is_reported(self, tmp_path):
        manifest(tmp_path / "payments.json", PAYMENTS)
        manifest(tmp_path / "overlap.json", [PAYMENTS[1], *CHECKOUT])
        catalog = catalog_for(
            tmp_path,
            {"payments": {"plugin": "manifest", "path": "payments.json"},
             "overlap": {"plugin": "manifest", "path": "overlap.json"}},
        )
        assert catalog.duplicate_ids == ["Shop.Payments.Cards.T002"]
        assert catalog.stats()["duplicate_ids"] == 1
        assert len(catalog.items()) == 4  # both are listed; the report says which collide
        assert len(catalog.item_ids()) == 3

    def test_a_sources_own_duplicates_are_reported_too(self, tmp_path):
        (tmp_path / "ids.json").write_text(
            '["Shop.Cart.Core.T001", "Shop.Cart.Core.T001", "Shop.Cart.Core.T002"]', encoding="utf-8"
        )
        catalog = catalog_for(tmp_path, {"c": {"module": f"{HERE}:CountingSource", "path": "ids.json"}})
        assert catalog.duplicate_ids == ["Shop.Cart.Core.T001"]

    def test_resolve_asks_each_source(self, tmp_path):
        manifest(
            tmp_path / "payments.json",
            [{"id": "Shop.Payments.Cards.T001", "tags": ["Payments"], "aliases": ["cards.visa"]}],
        )
        (tmp_path / "ids.json").write_text('["Shop.Cart.Core.T001"]', encoding="utf-8")
        catalog = catalog_for(
            tmp_path,
            {"payments": {"plugin": "manifest", "path": "payments.json"},
             "cart": {"module": f"{HERE}:AliasSource", "path": "ids.json"}},
        )
        assert catalog.resolve("Shop.Payments.Cards.T001") == "Shop.Payments.Cards.T001"
        assert catalog.resolve("cards.visa") == "Shop.Payments.Cards.T001"  # the first source's alias
        assert catalog.resolve("SHOP.CART.CORE.T001") == "Shop.Cart.Core.T001"  # the second's quirk
        assert catalog.resolve("Shop.Returns.T001") is None

    def test_stats(self, merged):
        stats = merged.stats()
        assert stats["items"] == 3 and stats["tags"] == 3
        assert stats["sources"] == ["payments", "checkout"]
        assert stats["provider"] == "manifest" and stats["snapshot"] == merged.snapshot()
        assert stats["duplicate_ids"] == 0 and stats["loaded_at"] == merged.loaded_at


class TestStayingCurrent:
    """Regression: the catalog was frozen at process start, so after any
    corpus edit every dispatch was refused until a restart."""

    def test_an_edited_manifest_is_picked_up_by_itself(self, tmp_path):
        path = manifest(tmp_path / "payments.json", PAYMENTS)
        catalog = catalog_for(tmp_path, {"manifest": {"path": "payments.json"}})
        before = catalog.snapshot()
        assert len(catalog.items()) == 2

        manifest(path, [*PAYMENTS, item("Shop.Payments.Refunds.T001", "Payments", "Refunds")])
        assert [i.id for i in catalog.items()][-1] == "Shop.Payments.Refunds.T001"
        assert catalog.snapshot() != before
        assert catalog.resolve("Shop.Payments.Refunds.T001") == "Shop.Payments.Refunds.T001"
        assert catalog.tag_counts()["Refunds"] == 1

    def test_an_unchanged_file_is_not_reloaded(self, tmp_path):
        manifest(tmp_path / "payments.json", PAYMENTS)
        catalog = catalog_for(tmp_path, {"manifest": {"path": "payments.json"}})
        catalog.items()
        loaded = [entry.source for entry in catalog._sources()]
        for _ in range(5):
            catalog.items()
            catalog.snapshot()
        assert [entry.source for entry in catalog._sources()] == loaded  # the very same objects
        assert catalog.refresh() is False

    def test_the_check_is_throttled_by_the_interval(self, tmp_path):
        """Between checks the catalog is served as loaded — the fingerprint
        probe costs a stat, and it is still not paid on every call."""
        path = manifest(tmp_path / "payments.json", PAYMENTS)
        catalog = catalog_for(tmp_path, {"manifest": {"path": "payments.json"}}, check_interval_s=3600)
        assert len(catalog.items()) == 2
        manifest(path, PAYMENTS[:1])
        assert len(catalog.items()) == 2  # not yet looked at again
        assert catalog.refresh() is True  # an explicit refresh does not wait
        assert len(catalog.items()) == 1

    def test_a_source_that_cannot_tell_is_reloaded_on_request_only(self, tmp_path):
        ids = tmp_path / "ids.json"
        ids.write_text('["Shop.Cart.Core.T001"]', encoding="utf-8")
        catalog = catalog_for(tmp_path, {"c": {"module": f"{HERE}:CountingSource", "path": "ids.json"}})
        assert len(catalog.items()) == 1 and CountingSource.built == 1

        ids.write_text('["Shop.Cart.Core.T001", "Shop.Cart.Core.T002"]', encoding="utf-8")
        assert len(catalog.items()) == 1  # nothing told it
        assert catalog.refresh() is False and CountingSource.built == 1
        assert catalog.refresh(force=True) is True and CountingSource.built == 2
        assert len(catalog.items()) == 2

    def test_fresh_is_what_a_dispatch_gets_the_corpus_as_it_is_now(self, tmp_path):
        ids = tmp_path / "ids.json"
        ids.write_text('["Shop.Cart.Core.T001"]', encoding="utf-8")
        catalog = catalog_for(tmp_path, {"c": {"module": f"{HERE}:CountingSource", "path": "ids.json"}})
        composed = catalog.snapshot()
        ids.write_text('["Shop.Cart.Core.T009"]', encoding="utf-8")
        fresh = catalog.fresh()
        assert fresh is catalog  # one catalog: ingestion sees what the dispatch saw
        assert fresh.snapshot() != composed
        assert catalog.resolve("Shop.Cart.Core.T009") == "Shop.Cart.Core.T009"

    def test_only_the_source_that_changed_is_rebuilt(self, tmp_path):
        path = manifest(tmp_path / "payments.json", PAYMENTS)
        (tmp_path / "ids.json").write_text('["Shop.Cart.Core.T001"]', encoding="utf-8")
        catalog = catalog_for(
            tmp_path,
            {"payments": {"plugin": "manifest", "path": "payments.json"},
             "cart": {"module": f"{HERE}:CountingSource", "path": "ids.json"}},
        )
        catalog.items()
        manifest(path, PAYMENTS[:1])
        assert len(catalog.items()) == 2
        assert CountingSource.built == 1  # the unfingerprinted one was left alone

    def test_a_broken_fingerprint_means_cannot_tell(self, tmp_path):
        ids = tmp_path / "ids.json"
        ids.write_text('["Shop.Cart.Core.T001"]', encoding="utf-8")
        catalog = catalog_for(
            tmp_path, {"c": {"module": f"{HERE}:BrokenProbeSource", "path": "ids.json"}}
        )
        assert len(catalog.items()) == 1  # the failing probe does not break reading
        ids.write_text('["Shop.Cart.Core.T001", "Shop.Cart.Core.T002"]', encoding="utf-8")
        assert len(catalog.items()) == 1
        assert catalog.refresh(force=True) is True and len(catalog.items()) == 2

    def test_a_corpus_caught_mid_save_does_not_fail_whoever_is_reading(self, tmp_path, caplog):
        """The automatic check may meet a file that is being written. It
        keeps serving what is loaded and looks again; only an explicit
        refresh reports the corpus as broken."""
        path = manifest(tmp_path / "payments.json", PAYMENTS)
        catalog = catalog_for(tmp_path, {"manifest": {"path": "payments.json"}})
        before = catalog.snapshot()

        path.write_text('{"items": [{"id": "Shop.Pay', encoding="utf-8")  # half a save
        with caplog.at_level("WARNING", logger="runcomposer.catalog"):
            assert [i.id for i in catalog.items()] == [i["id"] for i in PAYMENTS]
        assert catalog.snapshot() == before
        assert "keeping the loaded catalog" in caplog.text and "payments.json" in caplog.text
        with pytest.raises(ConfigError, match="cannot parse manifest"):
            catalog.refresh()
        assert catalog.snapshot() == before  # a failed refresh changes nothing either

        manifest(path, [*PAYMENTS, *CHECKOUT])  # the save completes
        assert len(catalog.items()) == 3

    def test_a_reload_is_all_or_nothing_across_sources(self, tmp_path):
        payments = manifest(tmp_path / "payments.json", PAYMENTS)
        checkout = manifest(tmp_path / "checkout.json", CHECKOUT)
        catalog = catalog_for(
            tmp_path,
            {"payments": {"plugin": "manifest", "path": "payments.json"},
             "checkout": {"plugin": "manifest", "path": "checkout.json"}},
        )
        before = (catalog.snapshot(), [i.id for i in catalog.items()])
        manifest(payments, PAYMENTS[:1])  # a good edit…
        checkout.write_text("not a manifest", encoding="utf-8")  # …and a broken file
        with pytest.raises(ConfigError):
            catalog.refresh()
        # Neither half was applied: the catalog is not a mixture of new and old.
        assert (catalog.snapshot(), [i.id for i in catalog.items()]) == before
        manifest(checkout, CHECKOUT)
        assert [i.id for i in catalog.items()] == [PAYMENTS[0]["id"], CHECKOUT[0]["id"]]

    def test_an_edit_made_while_the_corpus_is_being_read_is_not_lost(self, tmp_path):
        """The fingerprint a reload is remembered by is taken *before* the
        read. Taken after, an edit landing mid-read would mark the stale
        content as current — and nothing would notice until the next edit."""
        ids = tmp_path / "ids.json"

        def save(content: list[str]) -> None:
            previous = ids.stat().st_mtime_ns if ids.exists() else 0
            ids.write_text(json.dumps(content), encoding="utf-8")
            bumped = max(ids.stat().st_mtime_ns, previous + 1_000_000_000)
            os.utime(ids, ns=(bumped, bumped))

        save(["Shop.Cart.Core.T001"])
        catalog = catalog_for(
            tmp_path, {"c": {"module": f"{HERE}:EditedWhileReadSource", "path": "ids.json"}}
        )
        assert [i.id for i in catalog.items()] == ["Shop.Cart.Core.T001"]

        save(["Shop.Cart.Core.T001", "Shop.Cart.Core.T002"])
        third = ["Shop.Cart.Core.T001", "Shop.Cart.Core.T002", "Shop.Cart.Core.T003"]
        EditedWhileReadSource.during_read = lambda: save(third)
        # This read reloads (second version) and the third lands during it…
        assert len(catalog.items()) == 2
        # …so the very next look reloads again instead of trusting what it has.
        assert [i.id for i in catalog.items()] == third
        assert EditedWhileReadSource.built == 3

    def test_loaded_at_moves_with_a_reload(self, tmp_path):
        ids = tmp_path / "ids.json"
        ids.write_text('["Shop.Cart.Core.T001"]', encoding="utf-8")
        catalog = catalog_for(tmp_path, {"c": {"module": f"{HERE}:CountingSource", "path": "ids.json"}})
        assert catalog.refresh() is True  # the first refresh is the load
        first = catalog.loaded_at
        assert first is not None
        assert catalog.refresh(force=True) is True
        assert catalog.loaded_at is not None and catalog.loaded_at >= first

    def test_a_corpus_edit_does_not_need_a_restart_end_to_end(self, make_service, tmp_path):
        """The symptom itself: compose, edit the corpus, compose and run
        again — in one process."""
        path = manifest(tmp_path / "shop.json", PAYMENTS)
        service = make_service(sources={"manifest": {"path": "shop.json"}})
        first = compose(service, "Payments")
        assert service.require_run(first).planned_count == 2

        manifest(path, [*PAYMENTS, item("Shop.Payments.Refunds.T001", "Payments")])
        second = compose(service, "Payments")
        assert service.require_run(second).planned_count == 3
        service.dispatch(second, "demo", foreground=True)
        after = service.require_run(second)
        assert (after.state, sum(after.latest_dispatch.summary.values())) == ("COMPLETE", 3)


class TestThreadSafety:
    def test_readers_during_reloads_always_see_a_whole_catalog(self, tmp_path):
        """Many threads read while the corpus flips between two versions:
        every read is one version or the other, never a mixture, never an
        error."""
        path = manifest(tmp_path / "shop.json", PAYMENTS)
        catalog = catalog_for(tmp_path, {"manifest": {"path": "shop.json"}})
        small = frozenset(i["id"] for i in PAYMENTS)
        big_items = [*PAYMENTS, *CHECKOUT]
        big = frozenset(i["id"] for i in big_items)
        errors: list[BaseException] = []
        seen: set[frozenset[str]] = set()
        done = threading.Event()

        def read() -> None:
            try:
                while not done.is_set():
                    ids = frozenset(i.id for i in catalog.items())
                    seen.add(ids)
                    assert ids in (small, big)
                    assert catalog.item_ids() in (small, big)
                    assert catalog.snapshot().startswith("sha256:")
            except BaseException as exc:
                errors.append(exc)

        readers = [threading.Thread(target=read) for _ in range(8)]
        for reader in readers:
            reader.start()
        try:
            for round_ in range(20):
                manifest(path, big_items if round_ % 2 == 0 else PAYMENTS)
                catalog.refresh()
        finally:
            done.set()
            for reader in readers:
                reader.join(timeout=30)
        assert not errors, errors
        assert seen <= {small, big}
        assert frozenset(i.id for i in catalog.items()) == small  # round 19 wrote PAYMENTS
