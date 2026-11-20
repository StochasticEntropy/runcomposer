"""The README's "Try it", as one repeatable script (CI runs it).

Starts the example shop on a free port, runs ``runcomposer doctor`` and
``runcomposer run Smoke`` against a copy of ``example/`` in a temporary
directory, and checks what came back: exit code 0 (the run is PASS) and the
observer's summary line. Nothing waits on the clock: the shop is up before
the first command, and ``runcomposer run`` returns when the run is over.

    python example/try_it.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import shop_server  # noqa: E402  (the stand-in target next to this file)


def runcomposer(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "runcomposer.cli", *args]
    print("$ runcomposer " + " ".join(args), flush=True)
    done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=300)
    print(done.stdout + done.stderr, end="", flush=True)
    return done


def main() -> int:
    server, url = shop_server.start_in_background()
    workdir = Path(tempfile.mkdtemp(prefix="shop-connector-"))
    try:
        example = workdir / "example"
        shutil.copytree(HERE, example, ignore=shutil.ignore_patterns("state", "__pycache__"))
        env = {**os.environ, "SHOP_LOCAL_URL": url}
        config = str(example / "config.yaml")
        print(f"example shop: {url}", flush=True)
        if runcomposer("doctor", "--config", config, env=env).returncode != 0:
            return 1
        run = runcomposer("run", "Smoke", "--runner", "shop", "--title", "Smoke via try_it",
                          "--config", config, env=env)
        summary = (example / "state" / "summary.txt").read_text(encoding="utf-8")
        print("summary.txt:\n" + summary, end="", flush=True)
        if run.returncode != 0 or "run.completed" not in summary or "PASS" not in summary:
            print("FAILED: expected exit 0 and a PASS line in the summary", file=sys.stderr)
            return 1
        print("OK: the connector ran end to end through `runcomposer run`")
        return 0
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
