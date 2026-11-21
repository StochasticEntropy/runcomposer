"""A config file named by a relative path anchors every path in it to an
absolute place (DESIGN.md §8, "One path base").

A relative ``--config`` is read against the working directory once, when
the file is loaded. Every path the configuration hands out afterwards —
the core's directories, the store file, the taxonomy, a plugin's
``PATH_OPTIONS`` — is absolute, so a later change of working directory or a
worker process started in another directory still finds the same files.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from runcomposer.sdk import Service, load_config

from .conftest import compose, run_to_end

ITEMS = ["Shop.Checkout.Cards.Pay By Card", "Shop.Cart.Add Item"]

#: The executor a command runner starts: reports every id it was given as
#: passed, into the shard's output directory — both read from its arguments,
#: which are relative to nothing but what runcomposer handed over.
EXECUTOR = r"""
import json, pathlib, sys
ids_file, out_dir = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
ids = [line for line in ids_file.read_text(encoding="utf-8").split("\n") if line]
verdicts = [{"name": i, "status": "PASS", "duration_ms": 5} for i in ids]
(out_dir / "results.json").write_text(
    json.dumps({"format": "runcomposer-verdicts", "verdicts": verdicts}), encoding="utf-8")
"""


def deployment(root: Path) -> Path:
    """A config directory ``root/deploy`` whose every path is relative."""
    deploy = root / "deploy"
    (deploy / "tools").mkdir(parents=True)
    (deploy / "catalog.json").write_text(
        json.dumps({"items": [
            {"id": ITEMS[0], "tags": ["Checkout", "Payments-Cards"]},
            {"id": ITEMS[1], "tags": ["Cart"]},
        ]}),
        encoding="utf-8",
    )
    (deploy / "taxonomy.yaml").write_text(
        yaml.safe_dump({"taxonomy": [
            {"label": "Checkout", "filter": "Checkout"},
            {"label": "Cards", "filter": "Payments-Cards"},
            {"label": "Cart", "filter": "Cart"},
        ]}),
        encoding="utf-8",
    )
    (deploy / "tools" / "executor.py").write_text(EXECUTOR, encoding="utf-8")
    config = {
        "core": {
            "taxonomy_file": "taxonomy.yaml",
            "artifact_dir": "files/artifacts",
            "private_dir": "files/private",
            "ingestion": {"inbox": "files/inbox", "quarantine_dir": "files/quarantine"},
        },
        "store": {"sqlite": {"path": "files/runcomposer.db"}},
        "sources": {"shop": {"plugin": "manifest", "path": "catalog.json"}},
        "runners": {
            "shop-tests": {
                "plugin": "command",
                # The command runs in its own working directory — the
                # config directory's `tools` — and is handed runcomposer's
                # paths as arguments.
                "command": [sys.executable, "executor.py", "{ids_file}", "{out_dir}"],
                "cwd": "tools",
                "format": "runcomposer-verdicts",
            }
        },
    }
    (deploy / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (deploy / "files").mkdir()
    return deploy


def test_every_configured_path_is_absolute_and_survives_a_change_of_directory(tmp_path, monkeypatch):
    deploy = deployment(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(tmp_path)
    config = load_config(os.path.join("deploy", "config.yaml"))

    assert config.path == deploy / "config.yaml"
    core_paths = {
        "artifact_dir": config.artifact_dir,
        "private_dir": config.private_dir,
        "inbox": config.inbox_dir,
        "quarantine_dir": config.quarantine_dir,
        "taxonomy_file": config.taxonomy_source,
    }
    for name, path in core_paths.items():
        assert Path(path).is_absolute(), (name, path)
    assert config.artifact_dir == deploy / "files" / "artifacts"
    assert config.taxonomy_source == deploy / "taxonomy.yaml"
    plugin_paths = {
        "store.sqlite.path": config.store_spec().options["path"],
        "sources.shop.path": config.source_specs()[0].options["path"],
        "runners.shop-tests.cwd": config.plugin_spec("runners", "shop-tests").options["cwd"],
    }
    assert plugin_paths == {
        "store.sqlite.path": str(deploy / "files" / "runcomposer.db"),
        "sources.shop.path": str(deploy / "catalog.json"),
        "runners.shop-tests.cwd": str(deploy / "tools"),
    }

    # The working directory moves on; the deployment's files do not.
    monkeypatch.chdir(elsewhere)
    with Service(config) as service:
        service.validate()
        assert sorted(item.id for item in service.catalog.items()) == sorted(ITEMS)
        assert [node["label"] for node in service.taxonomy()["taxonomy"]] == ["Checkout", "Cards", "Cart"]
        run_id = compose(service, "Checkout", title="Card payments")
        run = run_to_end(service, run_id, "shop-tests")
        assert (run.state, run.completion) == ("COMPLETE", "PASS")
        workdir = config.artifact_dir / run.id / run.latest_dispatch.dispatch_id
        assert (workdir / "shard-1" / "results.json").is_file()
    assert (deploy / "files" / "runcomposer.db").is_file()
    assert sorted(path.name for path in elsewhere.iterdir()) == []


def test_built_in_defaults_anchor_to_the_working_directory_of_the_load(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(None)
    monkeypatch.chdir(tmp_path.parent)
    assert config.store_spec().options["path"] == str(tmp_path / "runcomposer.db")
    assert config.artifact_dir == tmp_path / "artifacts"


def test_the_cli_with_a_relative_config_runs_a_command_in_its_own_directory(tmp_path):
    """The command runner starts its executor in ``cwd`` (the config
    directory's ``tools``) and hands it the shard's paths: those name the
    deployment's files from any working directory."""
    deploy = deployment(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-m", "runcomposer.cli", "run", "Checkout", "--runner", "shop-tests",
         "--json", "--config", os.path.join("deploy", "config.yaml")],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    run = json.loads(completed.stdout)
    assert (run["state"], run["completion"]) == ("COMPLETE", "PASS")
    assert (deploy / "files" / "runcomposer.db").is_file()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["deploy"]
    assert not (deploy / "tools" / "deploy").exists()

    # `doctor` names the file it read as the absolute path it resolved to.
    doctor = subprocess.run(
        [sys.executable, "-m", "runcomposer.cli", "doctor", "--json",
         "--config", os.path.join("deploy", "config.yaml")],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert json.loads(doctor.stdout)["config"] == str(deploy / "config.yaml")
