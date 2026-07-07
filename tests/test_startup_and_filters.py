"""Regression tests for the P1 judge's findings: §8 fail-loudly-at-startup
and the §9 runs-list filters (labels, time window)."""

import pytest

from runcomposer.api import create_app
from runcomposer.cli import main as cli_main
from runcomposer.config import Config, ConfigError
from runcomposer.core.spec import build_spec
from runcomposer.plugins.sqlite_store import SqliteRunStore


class TestFailLoudlyAtStartup:
    """§8: unknown active plugin ids fail startup loudly — a server that
    boots healthy and 500s on first touch is the forbidden failure mode."""

    def test_create_app_refuses_unknown_store(self, tmp_path):
        with pytest.raises(ConfigError, match="nosuchstore"):
            create_app(Config(data={"store": {"nosuchstore": {}}}))

    def test_create_app_refuses_unknown_source(self, tmp_path):
        config = Config(
            data={
                "store": {"sqlite": {"path": str(tmp_path / "x.db")}},
                "sources": {"nosuchsource": {}},
            }
        )
        with pytest.raises(ConfigError, match="nosuchsource"):
            create_app(config)

    def test_create_app_refuses_unknown_runner(self, tmp_path):
        config = Config(
            data={
                "store": {"sqlite": {"path": str(tmp_path / "x.db")}},
                "runners": {"nosuchrunner": {}},
            }
        )
        with pytest.raises(ConfigError, match="nosuchrunner"):
            create_app(config)

    def test_serve_command_exits_cleanly_on_bad_plugin(self, tmp_path, capsys):
        bad = tmp_path / "config.yaml"
        bad.write_text("store: {nosuchstore: {}}\n", encoding="utf-8")
        assert cli_main(["serve", "--config", str(bad), "--port", "8399"]) == 2
        err = capsys.readouterr().err
        assert err.startswith("error:") and "nosuchstore" in err

    def test_cli_plugin_error_is_a_clean_one_liner_not_a_traceback(self, tmp_path, capsys):
        bad = tmp_path / "config.yaml"
        bad.write_text("store: {nosuchstore: {}}\n", encoding="utf-8")
        assert cli_main(["runs", "--config", str(bad)]) == 2
        err = capsys.readouterr().err
        assert err.startswith("error:") and "nosuchstore" in err
        assert "Traceback" not in err


class TestRunsListFilters:
    """§9: GET /runs filters by state, labels, and time."""

    @pytest.fixture()
    def store(self, tmp_path):
        store = SqliteRunStore(path=tmp_path / "filters.db")
        for n, labels in enumerate([{"origin": "ui"}, {"origin": "cli"}, {"origin": "ui", "env": "x"}]):
            spec = build_spec(
                title=f"run {n}",
                tag_filter="Smoke",
                materialized_ids=["A.T001"],
                source_provider="manifest",
                snapshot="sha256:" + "ab" * 32,
                labels=labels,
                run_id=f"01FILTERRUN00000000000000{n}"[:26],
                created_at=f"2026-07-07T10:00:0{n}Z",
            )
            store.create_run(spec, origin="test")
        return store

    def test_label_filter_requires_all_pairs(self, store):
        ui_runs = store.list_runs(labels={"origin": "ui"})
        assert [run.title for run in ui_runs] == ["run 2", "run 0"]
        both = store.list_runs(labels={"origin": "ui", "env": "x"})
        assert [run.title for run in both] == ["run 2"]

    def test_time_window_filter(self, store):
        assert [r.title for r in store.list_runs(since="2026-07-07T10:00:01Z")] == ["run 2", "run 1"]
        assert [r.title for r in store.list_runs(until="2026-07-07T10:00:00Z")] == ["run 0"]
        window = store.list_runs(since="2026-07-07T10:00:01Z", until="2026-07-07T10:00:01Z")
        assert [r.title for r in window] == ["run 1"]

    def test_api_exposes_label_and_time_filters(self, tmp_path):
        from fastapi.testclient import TestClient

        config = Config(data={"store": {"sqlite": {"path": str(tmp_path / "api.db")}}})
        client = TestClient(create_app(config))
        for origin in ("ui", "cli"):
            client.post(
                "/api/v1/runs",
                json={"selection": {"tag_filter": "Smoke"}, "title": origin, "labels": {"origin": origin}},
            )
        filtered = client.get("/api/v1/runs?label=origin=ui").json()["runs"]
        assert [run["title"] for run in filtered] == ["ui"]
        assert client.get("/api/v1/runs?label=notapair").status_code == 400
        assert client.get("/api/v1/runs?until=2000-01-01T00:00:00Z").json()["runs"] == []

    def test_cli_runs_label_filter(self, tmp_path, capsys):
        config = tmp_path / "config.yaml"
        config.write_text(f"store:\n  sqlite: {{ path: {tmp_path / 'cli.db'} }}\n", encoding="utf-8")
        assert cli_main(["spec", "Smoke", "--title", "Labeled", "--label", "env=staging",
                         "-o", str(tmp_path / "s.yaml"), "--config", str(config)]) == 0
        capsys.readouterr()
        assert cli_main(["runs", "--label", "env=staging", "--config", str(config)]) == 0
        assert "Labeled" in capsys.readouterr().out
        assert cli_main(["runs", "--label", "env=other", "--config", str(config)]) == 0
        assert "no runs stored" in capsys.readouterr().out
