"""A machine that only executes needs none of what reading, validating and
serving need.

The SDK, both toolkits, the remote channel and the single-file consumer are
imported — and used for what an executor does with them — in a fresh
interpreter in which ``yaml``, ``jsonschema`` and the web stack cannot be
imported at all. The one place that does need such a package names it when
it is reached.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BLOCKED = ("yaml", "jsonschema", "fastapi", "uvicorn", "starlette", "pydantic")

_BLOCKER = textwrap.dedent(
    """
    import sys
    from importlib.abc import MetaPathFinder

    BLOCKED = {blocked!r}

    class Blocker(MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ImportError(f"{{name}} is blocked in this interpreter", name=name)
            return None

    for name in list(sys.modules):
        if name.split(".")[0] in BLOCKED:
            del sys.modules[name]
    sys.meta_path.insert(0, Blocker())
    """
).format(blocked=BLOCKED)


def run_blocked(body: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "executor.py"
    script.write_text(_BLOCKER + textwrap.dedent(body), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
    )


def closed_port() -> int:
    """A local port nothing listens on (bound, then released)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_an_executor_imports_and_works_without_the_reading_and_serving_packages(tmp_path):
    port = closed_port()
    (tmp_path / "output.xml").write_text("<robot/>", encoding="utf-8")
    result = run_blocked(
        f"""
        import importlib.util, json

        import runcomposer.sdk
        import runcomposer.sdk.robot as robot_toolkit
        import runcomposer.sdk.jenkins
        import runcomposer.core.remote
        from runcomposer.sdk import RemoteLiveChannel, ResultsPushRefused, push_results

        spec = importlib.util.spec_from_file_location(
            "runcomposer_exec", {str(REPO / "src" / "runcomposer_exec.py")!r}
        )
        consumer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(consumer)

        payload = robot_toolkit.ShardPayload(
            shard="eu-1",
            out_dir="out",
            executions=(robot_toolkit.Execution("tests", ("Shop.Checkout.Cards.Pay By Card",)),),
        )
        callback = "http://127.0.0.1:{port}/api/v1/runs/01JSHOPRUN/results"
        channel = RemoteLiveChannel(callback, "rct_shop_token", timeout_s=2)
        channel.live("eu-1", "Shop.Checkout.Cards.Pay By Card", "RUNNING")
        channel.log("checkout suite started")
        try:
            push_results(callback, "rct_shop_token", "output.xml", shard="eu-1")
        except ResultsPushRefused as exc:
            pushed = "refused: " + str(exc)[:40]
        else:
            pushed = "sent"
        import sys
        loaded = sorted({{name.split(".")[0] for name in sys.modules}} & set({BLOCKED!r}))
        print(json.dumps({{"shard": payload.shard, "pushed": pushed,
                           "failures": channel.failures, "loaded": loaded,
                           "consumer": hasattr(consumer, "main")}}))
        """,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout.strip().splitlines()[-1])
    assert answer["shard"] == "eu-1"
    assert answer["pushed"].startswith("refused: "), "nothing listens on the port"
    assert answer["failures"] == 2, "best-effort progress is counted, not raised"
    assert answer["loaded"] == [] and answer["consumer"] is True


def test_reading_yaml_or_validating_names_the_missing_package(tmp_path):
    (tmp_path / "spec.yaml").write_text("runspec: '1.0'\n", encoding="utf-8")
    result = run_blocked(
        """
        from runcomposer.sdk import load_document, validate_document

        for call in (lambda: load_document("spec.yaml"), lambda: validate_document({"runspec": "1.0"})):
            try:
                call()
            except ModuleNotFoundError as exc:
                print(f"{exc.name}|{exc}")
        """,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0].startswith("yaml|reading the YAML spec file spec.yaml needs the 'yaml' package")
    assert lines[1].startswith("jsonschema|validating a run-spec document needs the 'jsonschema' package")
    assert all("pip install runcomposer" in line for line in lines)


def test_a_json_document_is_read_without_yaml(tmp_path):
    (tmp_path / "spec.json").write_text(json.dumps({"runspec": "1.0"}), encoding="utf-8")
    result = run_blocked(
        """
        from runcomposer.sdk import load_document
        print(load_document("spec.json")["runspec"])
        """,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1.0"
