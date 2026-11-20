# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.2.x | yes |
| 0.1.x | no — superseded by 0.2; fixes are not backported |

runcomposer is a young project maintained by one person. Fixes land on `main`
and go out in the next release; there is no backport branch.

## Reporting a vulnerability

Use **[private vulnerability reporting](https://github.com/StochasticEntropy/runcomposer/security/advisories/new)**
on this repository. That opens a draft advisory only you and the maintainer can
see.

If that form is unavailable to you, open a normal issue that says only *"security
report, needs a private channel"* — no details, no reproducer — and you will get
somewhere private to send them.

**Please do not** put a working exploit in a public issue, a pull request, or a
discussion before it is fixed.

What to expect, honestly: one maintainer, no on-call rotation. Acknowledgement
within about a week, and a fix timeline in that reply. If a week passes with
silence, ping the issue — it means the notification was missed, not ignored.
You will be credited in the advisory and the changelog unless you ask not to be.

## What is in scope

runcomposer's threat model is *"the results are attacker-influenced; the network
is not"*. These are the parts that carry real weight:

**1. Result-bundle parsing.** `robot-output-xml` and `junit-xml` parse XML that
arrives from wherever a test ran — a CI job, a colleague's laptop, a file drop.
Both read through one defused reader that refuses any document carrying a DTD
or an entity declaration — in the parser itself, wherever the declaration sits
and whatever the document's encoding. In scope: any
way to get external entity resolution, DTD expansion, an entity-expansion or
quadratic-blowup DoS, or SSRF out of a parser; also any path in a bundle that
escapes the extraction directory.

**2. Artifact serving.** `/artifacts/{run_id}/{dispatch_id}/…` serves bytes a
runner produced, from the same origin as the API and the UI. Paths are resolved
fully and refused unless they land strictly inside `core.artifact_dir`; the bytes
go out with `Content-Security-Policy: default-src 'none'; sandbox; base-uri 'none'`
and `X-Content-Type-Options: nosniff`. In scope: any traversal that reads a file
outside `artifact_dir` (via `..`, an absolute path, or a symlink), and any way an
artifact scripts against this origin, reaches the API, or steals an ingest token
despite those headers.

**3. Result ingestion and correlation.** `POST /api/v1/runs/{id}/results` requires
the ingest token of the dispatch being delivered to. A token is minted when a
spec is handed out (an export, or a runner handing off to an external executor),
returned once, bound to that dispatch, and stored only as a hash — it is not in
the spec document and no API returns it. The token is checked before anything
is read or written: a push for an unknown run is a `404` that writes nothing.
An authenticated bundle that does not match its dispatch lands in the
quarantine, never in run history, and the quarantine is bounded on write.
Attaching a quarantined bundle to a run is an explicit action, recorded as an
event on that run. In scope: ingesting without a valid token, reading a token
back, delivering into another dispatch with a token that is not its own,
forging a marker so a bundle silently enters another run's history, writing to
disk without authenticating, escaping the quarantine step, and bypassing the
configured upload-size or quarantine-count limits to exhaust disk.

**4. A run spec changing what the executing machine does.** A spec is a document
that travels. An in-process runner honours from its `runner.<id>` section only
the per-run parameters the runner declares; configured options (hooks,
listeners, commands, credentials) cannot be overridden from a document, and a
result format is a name resolved through configuration, never an import path.
In scope: any way a spec — or a results bundle — makes runcomposer execute or
import something its configuration did not name.

**5. The usual.** SQL injection into the sqlite store, XSS in the UI, secrets
(ingest tokens, `${env:…}` config values) leaking into logs, spec documents or
API responses.

## What is *not* a vulnerability

These are documented design decisions, not bugs. Reporting them is welcome as a
normal issue, but they will not get an advisory:

- **There is no login.** Full authN/authZ is explicitly out of scope for v1
  (DESIGN.md §13). runcomposer expects to sit on a trusted network or behind a
  reverse proxy that authenticates. The ingest token exists so the results
  *write* path is not open by default — it is not a user authentication system,
  and every other endpoint (composing, dispatching, cancelling, deleting,
  attaching a quarantined bundle) is open to whoever can reach the API.
- **Runners execute commands you configured.** `robot-pool` runs your suites and
  its `pre_run_hooks`, the `command` runner and the `command` observer run the
  command lines in your config, and `runcomposer-exec` runs the `--command` you
  give it. Someone who can already edit your config can already run code as
  you. That is the feature. (A run *spec* is not config — see 4 above.)
- **`--simulate` fabricates verdicts.** It is a transport test. It is documented
  as such wherever it is used, and it never claims a test ran.
- **`runcomposer demo` writes a database in the working directory.** It is a
  demo; it says so.
- Findings from an automated scanner with no demonstrated impact on the above.

## Handling a report about your own corpus

If you hit something that requires showing us your test names, ids, or internal
hostnames to reproduce, say so in the private advisory and send a reduced case
instead. We do not need your corpus, and would rather not have it.
