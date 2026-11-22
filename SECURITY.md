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
runner produced, from the same origin as the API and the UI. Only a file a
runner recorded — itself, or under a directory it recorded — is served. Paths
are resolved fully and refused unless they land strictly inside
`core.artifact_dir`; a refusal, and a path no file system can hold (a NUL
byte, an over-long name), is the same `404` as a miss. A path with a root, a
drive or a share of its own (`//host/share`, `\\host\share`, `C:x`; on
Windows any `:`) is refused before it is resolved, since resolving a share's
path on Windows connects to that host and offers it the server's credentials. Every artifact goes
out with these headers:

```
Content-Security-Policy: default-src 'none'; sandbox; base-uri 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
```

A `text/html` artifact is the one exception. A test framework's log or report
is an HTML application, a blank page without its own script, so it may run
its inline script and style and show embedded images — inside a sandbox
without `allow-same-origin`, which gives it an origin of its own: it cannot
read or call this server as the user, `default-src 'none'` lets it load and
connect to nothing, and `form-action 'none'` lets it post nothing:

```
Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; sandbox allow-scripts; base-uri 'none'; form-action 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
```

(These two blocks are compared with what the server sends by
`tests/test_api_browser_safety.py`.)

What that leaves a hostile HTML artifact, precisely: it cannot read or call
this server as the user (its origin is opaque: no cookies, no
`localStorage`, no API), cannot fetch, load or post anything
(`default-src 'none'`, `form-action 'none'`), and cannot open windows
(`sandbox` without `allow-popups`). It **can navigate its own tab** — by
script (`location = …`) or a link the user follows — to any address, and
put what it holds into that address: the report's own content, nothing of
the user's. In a frame, `sandbox` without `allow-top-navigation` keeps it
from moving the page around it; opened in a tab of its own, the tab is its
top-level page and navigating it is allowed. No standard header restricts
where a page may navigate itself (CSP's `navigate-to` was dropped from the
specification and no browser enforces it), so this residual stays; it is
why the run page links artifacts rather than embedding them. In scope: any traversal that reads a
file outside `artifact_dir` (via `..`, an absolute path, or a symlink), and
any way an artifact scripts against this origin, reaches the API, or steals
an ingest token despite those headers.

**3. Result ingestion and correlation.** `POST /api/v1/runs/{id}/results` requires
the ingest token of the dispatch being delivered to. A token is minted when a
spec is handed out (an export, or a runner handing off to an external executor),
returned once, bound to that dispatch, and stored only as a hash — it is not in
the spec document and no API returns it. The run and the token are checked
from the request's path and headers before anything is read or written: a
push for an unknown run (`404`), without a token (`401`) or with a wrong one
(`403`) is answered before runcomposer reads any of its body, and the
connection is closed — after the answer is out, and after what the client
still sends has been discarded, unparsed and unstored, for at most two
seconds, so that a client in the middle of sending reads the refusal instead
of a reset connection.
The same holds for an executor's progress (`/live`, `/log`). The body of an
authenticated push is then read as a stream, each file into a temporary
bundle directory under its base name — a name that is not a file name (`..`,
empty, a separator, a control character) is a `400` on every server; on a
Windows server so is a name Windows cannot keep (a character or a device
name it refuses, a trailing dot or space); and so are two files the server's
file system would keep as one (each is created exclusively) — and `max_upload_mb` is counted while
the bytes arrive: the push is refused (`413`) as soon as it passes the limit,
whatever length it declared, or none. A bundle refused for what it contains
is named by its files' names in the answer and in the quarantine, never by
where it lay on the server. An authenticated bundle that does not match its dispatch lands in the
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

**6. Requests a browser sends for another site.** There is no login (below),
so whoever's browser can reach the API can be made to send a request by any
page that browser opens. Every state-changing request — any method but
`GET`, `HEAD` and `OPTIONS`, on every route, the body-less ones such as
`cancel`, `rerun` and `finalize` included — is refused with `403`
(`cross-site-request`) when the browser says it comes from another site:

- its `Origin` is neither this server's own origin nor listed in
  `core.api.cors` (an `Origin` of `null`, a sandboxed document's, is never
  this server's), or
- it carries no `Origin` and `Sec-Fetch-Site: cross-site`.

"This server's own origin" is the `Host` the request was sent to: an
`Origin` passes when its host and port are the `Host` header's (the scheme's
default port when either leaves it out); the scheme is not compared. A
sibling host of the same site is another origin — list it in `core.api.cors`
to let it in. Clients that are not browsers — curl, the CLI, an executor
pushing results with its token — send neither header and are unaffected. A
token-authorized push that carries a foreign `Origin` is refused like any
other request: pushing from a web page is not a use case. In scope: any
state-changing request a page on another origin can get accepted.

*Behind a reverse proxy*, pass the `Host` the browser used through to
runcomposer unchanged, and list that name in `core.api.hosts` (below) — or,
if the proxy rewrites it, list the public origin
(`https://runcomposer.shop.example`) in `core.api.cors`; otherwise the UI's
own requests are refused.

*The `Host` header is restricted to the names the server answers to.* A
page on a host name its author controls that resolves to runcomposer's
address (DNS rebinding) is, to the browser, the same origin as that name,
so comparing `Origin` with `Host` cannot stop it — and listening on
`127.0.0.1` alone does not either, since the browser making the requests
runs on that machine. `core.api.hosts` lists the names runcomposer answers
to (`runcomposer.shop.example`, `*.shop.example`, `localhost:8100`; a name
without a port matches any port). A request for any other name, or without
a `Host`, is refused with `400` (`unknown-host`) before any route sees it,
whatever its method — `GET` included. Left unset, a server bound to a
loopback address (`runcomposer serve` on its default `127.0.0.1`) answers
only to `localhost`, `127.0.0.1`, `[::1]` and the address it is bound to,
which closes rebinding for the default local setup. A server bound to any
other address (`0.0.0.0` in the Docker image) answers to every name until
`core.api.hosts` is set — set it wherever a browser that visits untrusted
pages can reach the port, or put a reverse proxy in front that refuses
every other `Host`. An application embedded in another server without its
bind address answers to every name unless the list is set. In scope: a
request for a name the server does not answer to that it accepts anyway.

**7. API documentation.** `/openapi.json` — the API's description, from this
origin, with nothing in it but the schema — is always served. The two
viewers, `/docs` and `/redoc`, are off unless `core.api.docs: true`: they load
their script and style from a public CDN (`cdn.jsdelivr.net`) into the
browser that opens them — third-party script on this origin, and a blank
page in a closed network. `GET /api/v1/meta` says whether they are on
(`api.docs`).

## What is *not* a vulnerability

These are documented design decisions, not bugs. Reporting them is welcome as a
normal issue, but they will not get an advisory:

- **There is no login.** Full authN/authZ is explicitly out of scope for v1
  (DESIGN.md §13). runcomposer expects to sit on a trusted network or behind a
  reverse proxy that authenticates. The ingest token exists so the results
  *write* path is not open by default — it is not a user authentication system,
  and every other endpoint (composing, dispatching, cancelling, deleting,
  attaching a quarantined bundle) is open to whoever can reach the API. (What
  a browser sends for another site's page is refused — see 6 above.)
- **Runners execute commands you configured.** `robot-pool` runs your suites and
  its `pre_run_hooks`, the `command` runner and the `command` observer run the
  command lines in your config, and `runcomposer-exec` runs the `--command` you
  give it. Someone who can already edit your config can already run code as
  you. That is the feature. (A run *spec* is not config — see 4 above.)
- **`verify_tls=False` and `--no-verify-tls` skip the certificate check**
  of an executor's push, as documented — for a lab server whose
  certificate nobody signed. Behind a private certificate authority give
  the CA bundle instead (`ca_bundle=`, `--ca-bundle`,
  `RUNCOMPOSER_CA_BUNDLE`).
- **`--simulate` fabricates verdicts.** It is a transport test. It is documented
  as such wherever it is used, and it never claims a test ran.
- **`runcomposer demo` writes a database under the working directory**
  (`./runcomposer-demo/`, or `--workspace DIR`). It is a demo; it says so.
- Findings from an automated scanner with no demonstrated impact on the above.

## Handling a report about your own corpus

If you hit something that requires showing us your test names, ids, or internal
hostnames to reproduce, say so in the private advisory and send a reduced case
instead. We do not need your corpus, and would rather not have it.
