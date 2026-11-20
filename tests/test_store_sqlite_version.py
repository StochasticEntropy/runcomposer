"""What the sqlite store needs from the SQLite library it runs on, and how a
label is matched whatever that library's version (DESIGN.md §6.3).

The store relies on the upsert (SQLite 3.24.0) and on the JSON functions, which
are a compile-time option before 3.38.0. An older library, or one built
without JSON, is refused when the store opens — with the version found and
the version required — rather than failing later on some query.
"""

from __future__ import annotations

import sqlite3

import pytest

from runcomposer.plugins.sqlite_store import (
    MIN_SQLITE_VERSION,
    SqliteRunStore,
    sqlite_shortfall,
)
from runcomposer.sdk import StoreError

from .test_store import make_spec


class SqliteOneReleaseTooOld(SqliteRunStore):
    sqlite_version = (3, 23, 1)


class SqliteAtTheMinimum(SqliteRunStore):
    sqlite_version = (3, 24, 0)


class TestTheLibraryIsCheckedWhenTheStoreOpens:
    def test_the_minimum_is_the_upsert_release(self):
        assert MIN_SQLITE_VERSION == (3, 24, 0)

    @pytest.mark.parametrize("where", ["file", "memory"])
    def test_an_older_library_is_refused_naming_both_versions(self, tmp_path, where):
        path = str(tmp_path / "runcomposer.db") if where == "file" else ":memory:"
        with pytest.raises(StoreError) as refused:
            SqliteOneReleaseTooOld(path)
        message = str(refused.value)
        assert "SQLite 3.24.0 or newer" in message and "links SQLite 3.23.1" in message
        if where == "file":
            assert not (tmp_path / "runcomposer.db").exists() or (
                (tmp_path / "runcomposer.db").stat().st_size == 0
            ), "nothing is written by a store that refused to open"

    def test_the_minimum_itself_opens(self, tmp_path):
        store = SqliteAtTheMinimum(str(tmp_path / "runcomposer.db"))
        try:
            run = store.create_run(make_spec(labels={"suite": "nightly"}), origin="test")
            assert store.list_runs(labels={"suite": "nightly"}).runs[0].id == run.id
        finally:
            store.close()

    def test_a_library_without_json_functions_is_refused(self):
        message = sqlite_shortfall((3, 31, 1), json=False)
        assert message is not None
        assert "JSON functions" in message and "SQLite 3.31.1" in message
        assert sqlite_shortfall((3, 24, 0), json=True) is None
        assert sqlite_shortfall((3, 46, 1), json=True) is None
        # The version is the first thing said when both are lacking.
        assert "3.24.0 or newer" in (sqlite_shortfall((3, 8, 0), json=False) or "")

    def test_the_library_this_python_links_meets_the_requirements(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("SELECT COUNT(*) FROM json_each('{}')").fetchone()
        finally:
            connection.close()
        assert sqlite3.sqlite_version_info >= MIN_SQLITE_VERSION
        assert SqliteRunStore.sqlite_version == sqlite3.sqlite_version_info


class TestLabelsAreMatchedAsData:
    """A label key is matched literally, whatever characters it holds —
    there is no JSON path for a library version to read differently."""

    ODD_KEYS = ('we"ird.key', "back\\slash", "$.dollar", "brack[0]", "spa ce", "ümlaut", "'quote'")

    def test_every_odd_key_matches_only_itself(self, tmp_path):
        store = SqliteRunStore(str(tmp_path / "runcomposer.db"))
        try:
            for n, key in enumerate(self.ODD_KEYS):
                store.create_run(
                    make_spec(f"01K0000000000000000000000{n}", labels={key: f"v{n}"}), origin="test"
                )
            for n, key in enumerate(self.ODD_KEYS):
                page = store.list_runs(labels={key: f"v{n}"})
                assert [run.labels for run in page.runs] == [{key: f"v{n}"}], key
                assert store.list_runs(labels={key: "other"}).total == 0
            assert store.list_runs(labels={"we": "v0"}).total == 0, "no path is followed into a key"
        finally:
            store.close()
