#!/usr/bin/env python3
"""runcomposer-exec — the deliberately tiny run-spec consumer (DESIGN.md §6.2c).

A single self-contained **stdlib-only** Python file, by construction: it is
pipx-installable as part of the runcomposer distribution *and* vendorable by
copying this one file next to any executor (wget it from a release, drop it
into a CI checkout, ship it over git — no dependencies to install).

What it does:

1. reads a run spec (JSON always; YAML only if a YAML parser happens to be
   importable in the host environment),
2. executes exactly ``selection.materialized.item_ids`` — the embedded list
   is authoritative (executor contract, DESIGN.md §3.3),
3. writes results plus the ``runcomposer_run.json`` marker
   ``{run_id, dispatch_id?, shard?, spec_sha256}`` next to the outputs, so
   any bundle transport carries correlation home (§5).

Execution modes:

  --simulate           built-in fake execution emitting the
                       runcomposer-verdicts format (deterministic per --seed)
  --command TEMPLATE   run a shell command; placeholders: {ids_file} (a file
                       with one item id per line), {run_id}, {out_dir}. The
                       command writes its own native result files into the
                       output directory. Only these three placeholders are
                       replaced — any other braces in the template (an awk
                       program, a shell expansion) are left exactly as written.

The marker is the bundle's completion signal, so it is written **only when
the execution succeeded**: after --simulate, or when the command exited with
an accepted code (--ok-exit-codes, default 0). A command that failed leaves
no marker — a half-written bundle is then never mistaken for a result. If
your framework exits non-zero when tests fail, say which codes still mean
"the results are valid": --ok-exit-codes 0-249.

Talking back (optional): with --callback URL — the run's results URL, as a
runner hands it out — the consumer reports while it works, authorized by the
dispatch's ingest token (--token, or the environment variable
RUNCOMPOSER_INGEST_TOKEN): log lines for the run's event stream, and with
--simulate the live status of every item (RUNNING, then how it ended). With
--push it also uploads the finished bundle to that URL, so no other
transport is needed. Progress is best-effort: a report that cannot be
delivered is dropped and the execution goes on; a failed --push is an error.

Over HTTPS the server's certificate is verified against the system's trust
store, or against --ca-bundle PATH (a file of the CA certificates to
trust, as text — a private CA; default: the environment variable
RUNCOMPOSER_CA_BUNDLE). --no-verify-tls skips the check, for a lab server
whose certificate nobody signed (default: the environment variable
RUNCOMPOSER_VERIFY_TLS — true or false; unset means true).
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import random
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

MARKER_FILENAME = "runcomposer_run.json"
RESULTS_FORMAT = "runcomposer-verdicts"


def load_spec(path: Path) -> tuple[dict, str]:
    """Return (document, sha256-of-file-bytes). JSON first; YAML best-effort."""
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8")
    try:
        return json.loads(text), sha
    except json.JSONDecodeError:
        pass
    try:
        import yaml  # not a dependency — used only if the host happens to have it
    except ImportError:
        raise SystemExit(
            f"error: {path} is not JSON and no YAML parser is available in this "
            "environment — export the spec as JSON (runcomposer spec --format json)"
        ) from None
    return yaml.safe_load(text), sha


def materialized_ids(spec: dict, path: Path) -> list[str]:
    version = str(spec.get("runspec", ""))
    if not version.startswith("1."):
        raise SystemExit(
            f"error: {path}: unsupported runspec version {version!r} — "
            "this consumer knows runspec 1.x and refuses any other MAJOR"
        )
    try:
        ids = spec["selection"]["materialized"]["item_ids"]
    except (KeyError, TypeError):
        raise SystemExit(
            f"error: {path}: spec has no selection.materialized.item_ids — "
            "only a materialized spec is executable (DESIGN.md §3.3)"
        ) from None
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise SystemExit(f"error: {path}: selection.materialized.item_ids must be a list of strings")
    return ids


def simulate(item_ids: list[str], seed: str) -> list[dict]:
    """Deterministic fake verdicts: same (seed, id) -> same verdict."""
    verdicts = []
    for item_id in item_ids:
        roll = random.Random(f"{seed}|{item_id}").random()
        digest = hashlib.sha256(item_id.encode("utf-8")).digest()
        duration_ms = 80 + int.from_bytes(digest[:2], "big") % 3920
        if roll < 0.15:
            verdict = {"name": item_id, "status": "FAIL", "duration_ms": duration_ms,
                       "message": "simulated failure (runcomposer-exec)"}
        elif roll < 0.18:
            verdict = {"name": item_id, "status": "SKIP", "duration_ms": 0,
                       "message": "simulated skip (runcomposer-exec)"}
        else:
            verdict = {"name": item_id, "status": "PASS", "duration_ms": duration_ms}
        verdicts.append(verdict)
    return verdicts


TRUE_WORDS = ("1", "true", "yes", "on")
FALSE_WORDS = ("0", "false", "no", "off")


def tls_context(ca_bundle: str | None, verify_tls: bool) -> ssl.SSLContext:
    """The TLS context for an https callback: the system's trust store, or
    the CA bundle given; no verification at all with ``verify_tls`` false."""
    try:
        context = ssl.create_default_context(cafile=ca_bundle)
    except FileNotFoundError:
        raise SystemExit(f"error: --ca-bundle: no such CA bundle: {ca_bundle}") from None
    except ssl.SSLError as exc:
        raise SystemExit(f"error: --ca-bundle: {ca_bundle} holds no certificate that can be loaded: {exc}") from None
    if not verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def unreachable(exc: BaseException) -> str:
    """What to say about a request that did not reach the server; for a
    certificate that could not be verified, also what to do about it."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        return (f"{reason} — the server's certificate is not trusted: give --ca-bundle (a file "
                "holding the certificate of the CA that signed it), or --no-verify-tls for a "
                "server whose certificate nobody signed")
    return str(exc)


class Progress:
    """Reports to a runcomposer API while the execution is under way: live
    status per item (``POST …/live``) and log lines (``POST …/log``). With no
    callback URL every call is a no-op."""

    def __init__(self, callback: str | None, token: str | None, dispatch: str | None, shard: str | None,
                 ca_bundle: str | None = None, verify_tls: bool = True):
        base = (callback or "").rstrip("/")
        for suffix in ("/results", "/live", "/log"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        self.base = base
        self.token = token
        self.dispatch = dispatch
        self.shard = shard or "1"
        self.failures = 0
        # Built now, so that a CA bundle that cannot be used is an error
        # before anything executes — not a run of silently dropped reports.
        if base and ca_bundle and not os.path.isfile(ca_bundle):
            raise SystemExit(f"error: --ca-bundle: no such CA bundle: {ca_bundle}")
        self.context = tls_context(ca_bundle, verify_tls) if base.startswith("https:") else None

    def _post(self, path: str, body: dict) -> None:
        if not self.base:
            return
        if self.dispatch:
            body["dispatch"] = self.dispatch
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Runcomposer-Token"] = self.token
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=10, context=self.context) as response:
                response.read()
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
            self.failures += 1
            if self.failures == 1:  # say it once; progress never stops the execution
                print(f"runcomposer-exec: progress could not be reported ({unreachable(exc)})",
                      file=sys.stderr)

    def live(self, item_id: str, status: str, duration_ms: int = 0, message: str = "") -> None:
        self._post("/live", {"updates": [{"shard": self.shard, "item_id": item_id, "status": status,
                                          "duration_ms": duration_ms, "message": message}]})

    def log(self, message: str, level: str = "info") -> None:
        self._post("/log", {"lines": [{"message": message, "level": level}]})

    def push_bundle(self, out_dir: Path) -> int:
        """Upload every file of the bundle directory as one multipart POST to
        the results endpoint. Returns the HTTP status (0 when the server
        could not be reached)."""
        boundary = "runcomposer-" + uuid.uuid4().hex
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )

        for file in sorted(path for path in out_dir.iterdir() if path.is_file()):
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
                f'filename="{file.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                + file.read_bytes() + b"\r\n"
            )
        if self.dispatch:
            field("dispatch", self.dispatch)
        field("shard", self.shard)
        parts.append(f"--{boundary}--\r\n".encode())
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if self.token:
            headers["X-Runcomposer-Token"] = self.token
        request = urllib.request.Request(
            self.base + "/results", data=b"".join(parts), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=120, context=self.context) as response:
                print(f"runcomposer-exec: bundle pushed: {response.read().decode('utf-8', 'replace')}",
                      file=sys.stderr)
                return int(response.status)
        except urllib.error.HTTPError as exc:
            print(f"runcomposer-exec: the push was refused ({exc.code}): "
                  f"{exc.read().decode('utf-8', 'replace')}", file=sys.stderr)
            return int(exc.code)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            print(f"runcomposer-exec: the push failed: {unreachable(exc)}", file=sys.stderr)
            return 0


PLACEHOLDERS = ("ids_file", "run_id", "out_dir")


def render_command(template: str, values: dict) -> str:
    """Replace the known {placeholders} literally; touch nothing else.

    Deliberately not str.format: a template is a shell command line, and
    shell is full of braces that are not ours."""
    command = template
    for name in PLACEHOLDERS:
        command = command.replace("{" + name + "}", values[name])
    return command


def run_command(template: str, item_ids: list[str], run_id: str, out_dir: Path) -> int:
    ids_file = out_dir / "item_ids.txt"
    ids_file.write_text("\n".join(item_ids) + "\n", encoding="utf-8")
    command = render_command(
        template, {"ids_file": str(ids_file), "run_id": run_id, "out_dir": str(out_dir)}
    )
    print(f"runcomposer-exec: running: {command}", file=sys.stderr)
    return subprocess.run(command, shell=True).returncode


def parse_exit_codes(text: str) -> set[int]:
    """'0', '0,1', '0-249', '0-5,250' -> the set of accepted exit codes."""
    codes: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        low, sep, high = part.partition("-")
        try:
            first, last = int(low), int(high) if sep else int(low)
        except ValueError:
            raise SystemExit(f"error: --ok-exit-codes: not a code or range: {part!r}") from None
        if first > last:
            raise SystemExit(f"error: --ok-exit-codes: empty range {part!r}")
        codes.update(range(first, last + 1))
    if not codes:
        raise SystemExit("error: --ok-exit-codes: no exit code given")
    return codes


def verify_tls_setting(flag: bool | None) -> bool:
    """--verify-tls / --no-verify-tls when given, else RUNCOMPOSER_VERIFY_TLS
    (true or false), else true."""
    if flag is not None:
        return flag
    word = os.environ.get("RUNCOMPOSER_VERIFY_TLS", "").strip().lower()
    if not word or word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise SystemExit(f"error: RUNCOMPOSER_VERIFY_TLS: not true or false: {word!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="runcomposer-exec",
        description="Execute a runcomposer run spec and write a correlated results bundle.",
    )
    parser.add_argument("spec", help="path to the run spec (JSON; YAML if available)")
    parser.add_argument("--out", default="runcomposer_results",
                        help="output directory for results + marker (default: runcomposer_results)")
    parser.add_argument("--dispatch", default=None, help="dispatch id to record in the marker")
    parser.add_argument("--shard", default=None, help="shard label to record in the marker")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--simulate", action="store_true",
                      help="fake execution (deterministic; see --seed)")
    mode.add_argument("--command", default=None,
                      help="shell command template; placeholders {ids_file} {run_id} {out_dir}")
    parser.add_argument("--seed", default="exec", help="seed for --simulate (default: exec)")
    parser.add_argument("--ok-exit-codes", default="0", metavar="CODES",
                        help="exit codes of --command after which the bundle is complete and "
                        "the marker is written, e.g. '0', '0,1' or '0-249' (default: 0)")
    parser.add_argument("--callback", default=None, metavar="URL",
                        help="the run's results URL (…/api/v1/runs/<id>/results): report log "
                        "lines — and with --simulate live status per item — while executing")
    parser.add_argument("--token", default=None,
                        help="the dispatch's ingest token for --callback/--push (default: the "
                        "environment variable RUNCOMPOSER_INGEST_TOKEN)")
    parser.add_argument("--push", action="store_true",
                        help="upload the finished bundle to --callback (exit 4 if refused)")
    parser.add_argument("--ca-bundle", default=None, metavar="PATH",
                        help="a file of the CA certificates to trust for an https "
                        "--callback, instead of the system's (default: the environment variable "
                        "RUNCOMPOSER_CA_BUNDLE)")
    parser.add_argument("--verify-tls", action=argparse.BooleanOptionalAction, default=None,
                        help="verify the certificate of an https --callback; --no-verify-tls "
                        "skips the check, for a lab server (default: the environment variable "
                        "RUNCOMPOSER_VERIFY_TLS, true or false; unset: verify)")
    args = parser.parse_args(argv)
    ok_codes = parse_exit_codes(args.ok_exit_codes)
    if args.push and not args.callback:
        raise SystemExit("error: --push needs --callback (where to push)")
    progress = Progress(
        args.callback, args.token or os.environ.get("RUNCOMPOSER_INGEST_TOKEN"),
        args.dispatch, args.shard,
        ca_bundle=args.ca_bundle or os.environ.get("RUNCOMPOSER_CA_BUNDLE") or None,
        verify_tls=verify_tls_setting(args.verify_tls),
    )

    spec_path = Path(args.spec)
    spec, spec_sha256 = load_spec(spec_path)
    item_ids = materialized_ids(spec, spec_path)
    run_id = spec.get("run", {}).get("id")
    if not run_id:
        raise SystemExit(f"error: {spec_path}: spec has no run.id")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    exit_code = 0
    progress.log(f"runcomposer-exec: shard {progress.shard}: {len(item_ids)} item(s) to execute")
    if args.simulate:
        verdicts = simulate(item_ids, args.seed)
        for verdict in verdicts:
            progress.live(verdict["name"], "RUNNING")
            progress.live(verdict["name"], verdict["status"], verdict["duration_ms"],
                          verdict.get("message", ""))
        results = {"format": RESULTS_FORMAT, "run_id": run_id, "verdicts": verdicts}
        (out_dir / "results.json").write_text(
            json.dumps(results, indent=2) + "\n", encoding="utf-8"
        )
        counts: dict[str, int] = {}
        for verdict in verdicts:
            counts[verdict["status"]] = counts.get(verdict["status"], 0) + 1
        summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
        print(f"runcomposer-exec: simulated {len(verdicts)} items ({summary})", file=sys.stderr)
    else:
        exit_code = run_command(args.command, item_ids, run_id, out_dir)
        progress.log(f"runcomposer-exec: shard {progress.shard}: the command exited {exit_code}",
                     "info" if exit_code in ok_codes else "error")
        if exit_code not in ok_codes:
            # No marker: the marker says "this bundle is complete", and a
            # failed execution cannot say that.
            stale = out_dir / MARKER_FILENAME
            if stale.exists():
                stale.unlink()
            print(
                f"runcomposer-exec: the command exited {exit_code} (accepted: "
                f"{args.ok_exit_codes}) — no marker written, {out_dir} is NOT a complete bundle",
                file=sys.stderr,
            )
            return exit_code or 1

    # Written last, on purpose: its presence is what tells a file-drop inbox
    # that everything before it is in place.
    marker = {"run_id": run_id, "spec_sha256": spec_sha256}
    if args.dispatch:
        marker["dispatch_id"] = args.dispatch
    if args.shard:
        marker["shard"] = args.shard
    # …and atomically: it appears complete or not at all, never half-written.
    pending = out_dir / (MARKER_FILENAME + ".tmp")
    pending.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    os.replace(pending, out_dir / MARKER_FILENAME)
    print(f"runcomposer-exec: bundle ready at {out_dir} (marker: {MARKER_FILENAME})", file=sys.stderr)
    if args.push:
        status = progress.push_bundle(out_dir)
        if not 200 <= status < 300:
            return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
