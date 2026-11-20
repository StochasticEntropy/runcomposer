"""``runcomposer-exec`` (DESIGN.md §6.2c): the single-file, stdlib-only
consumer that executes a run spec somewhere runcomposer is not installed.

The file is imported and its ``main`` called — exactly what the console
script does. Commands run for real, through the shell.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

from .exec_loader import EXEC_FILE, runcomposer_exec

REPO = Path(__file__).resolve().parent.parent

IDS = ["Shop.Auth.SSO.T001", "Shop.Auth.SSO.T002", "Shop.Auth.SSO.T003"]
RUN_ID = "01HEXECTESTRUN000000000000"

posix_shell = pytest.mark.skipif(os.name == "nt", reason="the command templates are POSIX shell")


@pytest.fixture()
def spec_file(tmp_path: Path) -> Path:
    document = {
        "runspec": "1.0",
        "run": {"id": RUN_ID, "title": "Anmeldung prüfen", "created_at": "2026-07-06T09:14:03Z"},
        "selection": {
            "tag_filter": "Auth-SSO",
            "materialized": {"item_ids": IDS, "at": "2026-07-06T09:14:02Z", "count": 3},
        },
        "source": {"provider": "manifest", "snapshot": "sha256:0"},
        "results": {"expect": [{"format": "runcomposer-verdicts"}], "shards": 1, "deliver": "none"},
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def marker_of(out: Path) -> dict:
    return json.loads((out / runcomposer_exec.MARKER_FILENAME).read_text(encoding="utf-8"))


# -- simulate --------------------------------------------------------------------


class TestSimulate:
    def test_it_writes_results_and_the_marker(self, spec_file, tmp_path, capsys):
        out = tmp_path / "bundle"
        assert runcomposer_exec.main([str(spec_file), "--out", str(out), "--simulate"]) == 0
        results = json.loads((out / "results.json").read_text(encoding="utf-8"))
        assert results["format"] == "runcomposer-verdicts" and results["run_id"] == RUN_ID
        # Exactly the materialized ids, in order — the executor contract (§3.3).
        assert [verdict["name"] for verdict in results["verdicts"]] == IDS
        assert {verdict["status"] for verdict in results["verdicts"]} <= {"PASS", "FAIL", "SKIP"}
        assert marker_of(out) == {
            "run_id": RUN_ID,
            "spec_sha256": hashlib.sha256(spec_file.read_bytes()).hexdigest(),
        }
        err = capsys.readouterr().err
        assert "simulated 3 items" in err and "bundle ready at" in err

    def test_the_hash_is_of_the_file_bytes_not_of_the_parsed_document(self, spec_file, tmp_path):
        """A non-ASCII title is where "re-serialize and hash" goes wrong."""
        assert "prüfen".encode() in spec_file.read_bytes()
        out = tmp_path / "bundle"
        runcomposer_exec.main([str(spec_file), "--out", str(out), "--simulate"])
        reserialized = json.dumps(json.loads(spec_file.read_text(encoding="utf-8"))).encode()
        assert marker_of(out)["spec_sha256"] == hashlib.sha256(spec_file.read_bytes()).hexdigest()
        assert marker_of(out)["spec_sha256"] != hashlib.sha256(reserialized).hexdigest()

    def test_the_same_seed_gives_the_same_verdicts(self, spec_file, tmp_path):
        def statuses(name: str, seed: str) -> list[str]:
            out = tmp_path / name
            runcomposer_exec.main([str(spec_file), "--out", str(out), "--simulate", "--seed", seed])
            return [v["status"] for v in json.loads((out / "results.json").read_text())["verdicts"]]

        assert statuses("one", "a") == statuses("two", "a") == ["PASS", "PASS", "PASS"]
        assert statuses("three", "b") != statuses("one", "a")

    def test_the_marker_carries_dispatch_and_shard_when_given(self, spec_file, tmp_path):
        out = tmp_path / "bundle"
        runcomposer_exec.main(
            [str(spec_file), "--out", str(out), "--simulate", "--dispatch", "01DISPATCH", "--shard", "env2-1"]
        )
        marker = marker_of(out)
        assert (marker["dispatch_id"], marker["shard"]) == ("01DISPATCH", "env2-1")

    def test_a_yaml_spec_is_read_when_a_yaml_parser_is_around(self, spec_file, tmp_path):
        document = json.loads(spec_file.read_text(encoding="utf-8"))
        yaml_file = tmp_path / "spec.yaml"
        yaml_file.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        out = tmp_path / "bundle"
        assert runcomposer_exec.main([str(yaml_file), "--out", str(out), "--simulate"]) == 0
        assert marker_of(out)["spec_sha256"] == hashlib.sha256(yaml_file.read_bytes()).hexdigest()


# -- command ---------------------------------------------------------------------


class TestCommand:
    def test_only_the_three_placeholders_are_replaced(self):
        rendered = runcomposer_exec.render_command(
            "awk '{print $1}' {ids_file} > {out_dir}/col.txt && echo ${HOME} {run_id} {unknown} {}",
            {"ids_file": "/tmp/ids.txt", "run_id": "RUN", "out_dir": "/tmp/out"},
        )
        assert rendered == (
            "awk '{print $1}' /tmp/ids.txt > /tmp/out/col.txt && echo ${HOME} RUN {unknown} {}"
        )

    @posix_shell
    def test_a_template_full_of_shell_braces_runs_unchanged(self, spec_file, tmp_path, monkeypatch):
        """Regression: `str.format` raised KeyError on `{print $1}`."""
        monkeypatch.setenv("SHOP_REGION", "eu-central")
        out = tmp_path / "bundle"
        template = (
            "awk -F. '{print $NF}' {ids_file} > {out_dir}/last-segment.txt"
            " && echo \"${SHOP_REGION}\" > {out_dir}/region.txt"
            " && echo {run_id} > {out_dir}/run.txt"
        )
        assert runcomposer_exec.main([str(spec_file), "--out", str(out), "--command", template]) == 0
        assert (out / "last-segment.txt").read_text().split() == ["T001", "T002", "T003"]
        assert (out / "region.txt").read_text().strip() == "eu-central"
        assert (out / "run.txt").read_text().strip() == RUN_ID
        assert (out / "item_ids.txt").read_text(encoding="utf-8").splitlines() == IDS
        assert marker_of(out)["run_id"] == RUN_ID

    def test_the_command_gets_the_ids_file_and_writes_its_own_results(self, spec_file, tmp_path):
        out = tmp_path / "bundle"
        script = tmp_path / "runner.py"
        script.write_text(
            "import json, sys\n"
            "ids = open(sys.argv[1], encoding='utf-8').read().split()\n"
            "json.dump({'format': 'runcomposer-verdicts', 'verdicts':"
            " [{'name': i, 'status': 'PASS'} for i in ids]},"
            " open(sys.argv[2] + '/results.json', 'w'))\n",
            encoding="utf-8",
        )
        template = f'"{sys.executable}" "{script}" "{{ids_file}}" "{{out_dir}}"'
        assert runcomposer_exec.main([str(spec_file), "--out", str(out), "--command", template]) == 0
        verdicts = json.loads((out / "results.json").read_text())["verdicts"]
        assert [verdict["name"] for verdict in verdicts] == IDS
        assert (out / runcomposer_exec.MARKER_FILENAME).is_file()

    def test_a_failed_command_writes_no_marker(self, spec_file, tmp_path, capsys):
        """The marker says "this bundle is complete". A failed execution
        cannot say that — and a marker left over from an earlier attempt in
        the same directory must not say it either."""
        out = tmp_path / "bundle"
        out.mkdir()
        stale = out / runcomposer_exec.MARKER_FILENAME
        stale.write_text(json.dumps({"run_id": RUN_ID}), encoding="utf-8")
        template = f'"{sys.executable}" -c "import sys; sys.exit(3)"'
        code = runcomposer_exec.main([str(spec_file), "--out", str(out), "--command", template])
        assert code == 3
        assert not stale.exists()
        err = capsys.readouterr().err
        assert "exited 3" in err and "NOT a complete bundle" in err and "bundle ready" not in err

    def test_accepted_exit_codes_still_produce_a_bundle(self, spec_file, tmp_path):
        """A framework that exits non-zero when tests fail: its results are
        valid, and `--ok-exit-codes` says which codes mean that."""
        out = tmp_path / "bundle"
        template = f'"{sys.executable}" -c "import sys; sys.exit(3)"'
        code = runcomposer_exec.main(
            [str(spec_file), "--out", str(out), "--command", template, "--ok-exit-codes", "0-249"]
        )
        assert code == 0 and marker_of(out)["run_id"] == RUN_ID
        refused = tmp_path / "refused"
        code = runcomposer_exec.main(
            [str(spec_file), "--out", str(refused), "--command", template, "--ok-exit-codes", "0,1,250"]
        )
        assert code == 3 and not (refused / runcomposer_exec.MARKER_FILENAME).exists()

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("0", {0}), ("0,1", {0, 1}), ("0-3", {0, 1, 2, 3}), (" 0-1 , 250 ", {0, 1, 250})],
    )
    def test_exit_code_sets(self, text, expected):
        assert runcomposer_exec.parse_exit_codes(text) == expected

    @pytest.mark.parametrize("text", ["zero", "5-1", "", ",", "1-x"])
    def test_bad_exit_code_sets_are_refused(self, spec_file, tmp_path, text):
        with pytest.raises(SystemExit, match="--ok-exit-codes"):
            runcomposer_exec.main(
                [str(spec_file), "--out", str(tmp_path / "o"), "--command", "true", "--ok-exit-codes", text]
            )


# -- what it refuses --------------------------------------------------------------


class TestRefusals:
    def _write(self, tmp_path: Path, document: dict) -> Path:
        path = tmp_path / "other.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_a_higher_major_is_refused(self, tmp_path):
        path = self._write(tmp_path, {"runspec": "2.0", "run": {"id": "x"}})
        with pytest.raises(SystemExit, match="refuses any other MAJOR"):
            runcomposer_exec.main([str(path), "--simulate"])

    def test_an_unmaterialized_spec_is_refused(self, tmp_path):
        path = self._write(
            tmp_path, {"runspec": "1.0", "run": {"id": "x"}, "selection": {"tag_filter": "Auth"}}
        )
        with pytest.raises(SystemExit, match="only a materialized spec is executable"):
            runcomposer_exec.main([str(path), "--simulate"])

    def test_a_spec_without_a_run_id_is_refused(self, tmp_path):
        path = self._write(
            tmp_path,
            {"runspec": "1.0", "run": {}, "selection": {"materialized": {"item_ids": IDS}}},
        )
        with pytest.raises(SystemExit, match=r"no run\.id"):
            runcomposer_exec.main([str(path), "--simulate"])

    def test_one_mode_is_required(self, spec_file):
        with pytest.raises(SystemExit):
            runcomposer_exec.main([str(spec_file)])
        with pytest.raises(SystemExit):
            runcomposer_exec.main([str(spec_file), "--simulate", "--command", "true"])


# -- single file, stdlib only — verified, not asserted ----------------------------


class TestConstruction:
    def test_it_is_one_file(self):
        assert EXEC_FILE.is_file()
        assert not (REPO / "src" / "runcomposer_exec").exists(), "no package sibling"
        assert Path(runcomposer_exec.__file__).resolve() == EXEC_FILE, "tests load the source file"

    def test_its_imports_are_stdlib_only(self):
        tree = ast.parse(EXEC_FILE.read_text(encoding="utf-8"))
        stdlib = set(sys.stdlib_module_names)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, "no relative imports in a single file"
                imported.add((node.module or "").split(".")[0])
        assert imported - stdlib == {"yaml"}, (
            "runcomposer_exec.py must stay stdlib-only (yaml is allowed only inside the "
            f"guarded best-effort import) — found {sorted(imported - stdlib)}"
        )
        assert "runcomposer" not in imported

    def test_the_yaml_import_is_guarded(self):
        text = EXEC_FILE.read_text(encoding="utf-8")
        assert "try:\n        import yaml" in text and "except ImportError" in text

    def test_it_runs_as_a_script_with_nothing_but_the_interpreter(self, spec_file, tmp_path):
        """Vendored next to an executor: one file, started with `python3 file`,
        isolated from this environment's packages (-I, no site)."""
        import subprocess

        vendored = tmp_path / "vendored" / "runcomposer_exec.py"
        vendored.parent.mkdir()
        vendored.write_bytes(EXEC_FILE.read_bytes())
        out = tmp_path / "bundle"
        completed = subprocess.run(
            [sys.executable, "-I", "-S", str(vendored), str(spec_file), "--out", str(out), "--simulate"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr
        assert marker_of(out)["run_id"] == RUN_ID
