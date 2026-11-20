"""Refusing state-changing requests a browser sends for another site's page.

runcomposer has no login (DESIGN.md §13): whoever reaches the API may compose,
dispatch, cancel and delete. A browser that can reach it — the browser of
anyone who has the UI open — can be made to send such a request by any web
page it visits, by a form post or a ``fetch`` with ``mode: no-cors``. The
answer is unreadable to that page; the side effect is not. Browsers say where
a request comes from, and that is enough to refuse it:

- ``Origin`` — sent by browsers with every ``POST`` and every request
  ``fetch`` makes across origins. A request whose ``Origin`` is neither this
  server's own origin nor listed in ``core.api.cors`` is refused.
- ``Sec-Fetch-Site: cross-site`` without an ``Origin`` — refused too.

Safe methods (``GET``, ``HEAD``, ``OPTIONS``) pass: they change nothing.
Clients that are not browsers (curl, the CLI, an executor pushing results
with its token) send neither header and pass unchanged. A token-authorized
push that does carry a foreign ``Origin`` is refused like any other request:
pushing results from a web page is not a use case.

"This server's own origin" is the ``Host`` the request was sent to: an
``Origin`` passes when its host and port are the ``Host`` header's, the
scheme's default port being implied when either leaves it out. The scheme is
not compared, so a TLS-terminating reverse proxy that passes the ``Host``
through needs no configuration; one that rewrites ``Host`` must have the
public origin listed in ``core.api.cors`` (SECURITY.md).
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .errors import CrossSiteRequest, error_body, status_of

__all__ = ["SAFE_METHODS", "CrossSiteGuard"]

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(value: str) -> tuple[str, str, int] | None:
    """``(scheme, host, port)`` of an ``Origin`` value; ``None`` for anything
    that is not an http(s) origin (``null``, a sandboxed document's origin)."""
    try:
        parts = urlsplit(value.strip().lower())
        if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
            return None
        return parts.scheme, parts.hostname, parts.port or _DEFAULT_PORTS[parts.scheme]
    except ValueError:
        return None


def _host(value: str, default_port: int) -> tuple[str, int] | None:
    """``(host, port)`` of a ``Host`` header value."""
    try:
        parts = urlsplit("//" + value.strip())
        if not parts.hostname:
            return None
        return parts.hostname.lower(), parts.port or default_port
    except ValueError:
        return None


class CrossSiteGuard:
    """ASGI middleware: one rule for every state-changing route."""

    def __init__(self, app: ASGIApp, *, allowed_origins: Iterable[str] = ()):
        self.app = app
        allowed = [str(origin) for origin in allowed_origins]
        self.any_origin = "*" in allowed
        self.allowed = {parsed for parsed in map(_origin, allowed) if parsed is not None}

    def refusal(self, headers: Headers) -> str | None:
        """Why this state-changing request is refused, or ``None``."""
        origin = headers.get("origin")
        if origin is not None:
            parsed = _origin(origin)
            if parsed is not None:
                if self.any_origin or parsed in self.allowed:
                    return None
                scheme, *host_and_port = parsed
                if _host(headers.get("host", ""), _DEFAULT_PORTS[scheme]) == tuple(host_and_port):
                    return None
            return (
                f"refused a state-changing request from a page on {origin!r}: it is neither "
                "this server's own origin nor listed in core.api.cors"
            )
        if headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
            return (
                "refused a state-changing request the browser marked as cross-site "
                "(Sec-Fetch-Site: cross-site)"
            )
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            reason = self.refusal(Headers(scope=scope))
            if reason is not None:
                exc = CrossSiteRequest(reason)
                response = JSONResponse(
                    status_code=status_of(exc), content=error_body(reason, exc.code)
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
