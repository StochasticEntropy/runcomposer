"""``runcomposer catalog --manifest FILE`` builds its source the way every
configured source is built (DESIGN.md §6.1, §8): through the
``sources.manifest`` entry of the configuration when there is one — its
``plugin:`` / ``module:`` and options honoured — else through the plugin
registered under the name ``manifest``; options checked, errors naming the
entry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from runcomposer.cli.main import main as cli_main
from runcomposer.plugins.manifest_source import ManifestSource
from runcomposer.sdk import Item

from .conftest import write_config

HERE = "tests.test_catalog_manifest_entry"


class StampedManifest(ManifestSource):
    """A deployment's own manifest reader: every item it reads carries the
    configured ``stamp`` tag as well."""

    def __init__(self, path: Any, stamp: str = "Smoke"):
        self._stamp_tag = stamp
        super().__init__(path)

    def items(self) -> list[Item]:
        return [Item(id=item.id, tags=(*item.tags, self._stamp_tag)) for item in super().items()]


def a_manifest(directory: Path) -> Path:
    path = directory / "gifts.json"
    path.write_text(json.dumps({"items": [{"id": "Shop.Gift.T001", "tags": ["Checkout-Gift"]}]}),
                    encoding="utf-8")
    return path


def catalog(capsys: Any, *argv: str) -> tuple[int, str, str]:
    code = cli_main(["catalog", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_the_configured_manifest_entry_reads_the_file(tmp_path, capsys):
    manifest = a_manifest(tmp_path)
    config = write_config(tmp_path, sources={"manifest": {"module": f"{HERE}:StampedManifest", "stamp": "Regression"}})
    code, out, err = catalog(capsys, "--manifest", str(manifest), "--json", "--config", str(config))
    assert code == 0, err
    assert json.loads(out)["entries"] == [{"id": "Shop.Gift.T001", "tags": ["Checkout-Gift", "Regression"]}]


def test_a_relative_file_is_read_from_the_working_directory(tmp_path, capsys, monkeypatch):
    a_manifest(tmp_path)
    (tmp_path / "etc").mkdir()
    config = write_config(tmp_path / "etc")
    monkeypatch.chdir(tmp_path)
    code, out, err = catalog(capsys, "--manifest", "gifts.json", "--json", "--config", str(config))
    assert code == 0, err
    assert json.loads(out)["entries"] == [{"id": "Shop.Gift.T001", "tags": ["Checkout-Gift"]}]


def test_the_entry_is_checked_like_any_configured_source(tmp_path, capsys):
    manifest = a_manifest(tmp_path)
    config = write_config(tmp_path, sources={"manifest": {"module": f"{HERE}:StampedManifest",
                                                          "stmap": "Regression"}})
    code, _out, err = catalog(capsys, "--manifest", str(manifest), "--config", str(config))
    assert code == 2
    assert "sources.manifest: unknown option(s) ['stmap']" in err
