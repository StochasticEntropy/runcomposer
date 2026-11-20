"""The CLI's read paths and a dispatch whose owning process is gone
(docs/cli.md: runs, show, watch).

Restart recovery marks such a dispatch ``STALE`` — but only where it runs:
``serve`` at startup, ``doctor``, the dispatching commands. A reader that
merely lists runs must not rewrite state that a live server may own, so it
does not mark anything; it says what it sees: ``RUNNING (owner gone)``, a
hint how to reconcile, and ``owner_gone`` in its JSON. Ownership is judged
by the rule recovery applies (``owner_alive``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from runcomposer.sdk import Service, load_config

from .conftest import compose, wait_until, write_config
from .test_cli import Cli
from .test_live_rows_after_dispatch import ROOT, runners

_CHILD = """
import sys
from runcomposer.sdk import Service, load_config

service = Service(load_config(sys.argv[1]))
service.dispatch(sys.argv[2], "streaming", foreground=True)
"""


@pytest.fixture()
def owned(tmp_path: Path, capsys):
    """A run whose dispatch a real child process executes: yields
    ``(cli, run_id, child)``; the child is killed at teardown."""
    config = write_config(tmp_path, runners=runners(tmp_path))
    with Service(load_config(str(config))) as service:
        run_id = compose(service, "Auth-SSO", runner_id="streaming")
    child = subprocess.Popen([sys.executable, "-c", _CHILD, str(config), run_id], cwd=ROOT)
    try:
        wait_until(lambda: (tmp_path / "streamed").exists() or child.poll() is not None,
                   what="the child executing the dispatch", timeout=60)
        assert child.poll() is None, f"the child ended early ({child.returncode})"
        yield Cli(config, capsys), run_id, child
    finally:
        child.kill()
        child.wait(60)


def snapshot(cli: Cli, run_id: str) -> tuple[Any, ...]:
    with cli.service() as service:
        dispatch = service.require_run(run_id).latest_dispatch
        return dispatch.state, dispatch.owner, dispatch.error, len(service.store.events(run_id))


class TestOwnerGone:
    def test_runs_and_show_flag_a_dispatch_whose_owner_was_killed(self, owned):
        cli, run_id, child = owned
        child.kill()
        child.wait(60)
        before = snapshot(cli, run_id)
        assert before[0] == "RUNNING"

        code, out, _err = cli("runs")
        assert code == 0
        (row,) = [line for line in out.splitlines() if line.startswith(run_id)]
        assert "RUNNING (owner gone)" in row
        header = out.splitlines()[0]
        assert row.index("-") == header.index("RESULT"), "the columns stay aligned"
        assert f"`runcomposer cancel --force {run_id}`" in out and "`runcomposer doctor`" in out
        assert f":{child.pid}:" in out, "the hint names the owner that is gone"

        code, out, _err = cli("show", run_id)
        assert code == 0
        assert "state: RUNNING (owner gone)" in out
        assert any(line.startswith("dispatch ") and "RUNNING (owner gone)" in line
                   for line in out.splitlines())
        assert f"# {run_id}: the process executing dispatch" in out

        listed = cli.json("runs")["runs"][0]
        assert listed["id"] == run_id and listed["state"] == "RUNNING"
        assert listed["owner_gone"] is True and listed["dispatches"][-1]["owner_gone"] is True
        detail = cli.json("show", run_id)
        assert detail["owner_gone"] is True and detail["dispatches"][-1]["owner_gone"] is True

        code, _out, err = cli("watch", run_id, "--no-follow")
        assert code == 0 and "warning:" in err and "is gone" in err

        # Only read: the dispatch is as the dead process left it.
        assert snapshot(cli, run_id) == before

    def test_a_dispatch_a_live_process_executes_is_not_flagged(self, owned):
        cli, run_id, child = owned
        code, out, _err = cli("runs")
        (row,) = [line for line in out.splitlines() if line.startswith(run_id)]
        assert code == 0 and "RUNNING" in row and "owner gone" not in out
        code, out, _err = cli("show", run_id)
        assert code == 0 and "state: RUNNING —" in out and "owner gone" not in out
        assert cli.json("runs")["runs"][0]["owner_gone"] is False
        detail = cli.json("show", run_id)
        assert detail["owner_gone"] is False and detail["dispatches"][-1]["owner_gone"] is False
        _code, _out, err = cli("watch", run_id, "--no-follow")
        assert "owner" not in err
        assert child.poll() is None, "the child was executing it the whole time"

    def test_runs_that_are_over_carry_owner_gone_false(self, tmp_path, capsys):
        cli = Cli(write_config(tmp_path), capsys)
        with cli.service() as service:
            run_id = compose(service, "Auth-SSO")
            service.dispatch(run_id, "demo", foreground=True)
        listed = cli.json("runs")["runs"][0]
        assert listed["state"] == "COMPLETE" and listed["owner_gone"] is False
        assert json.dumps(listed["dispatches"][0]["owner_gone"]) == "false"
