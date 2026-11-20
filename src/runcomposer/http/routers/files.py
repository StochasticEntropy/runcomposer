"""Files: the local artifact directory, and the pre-built UI."""

from __future__ import annotations

import mimetypes
from importlib import resources
from typing import Any

from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from runcomposer import __version__
from runcomposer.app.service import Service
from runcomposer.core.errors import NotFound

from . import service_of

router = APIRouter()

#: index.html and the locale files are re-validated on every load, so a new
#: release reaches the browser at once; the hashed assets never change under
#: their name and may be kept forever.
_REVALIDATE = {"Cache-Control": "no-cache"}
_IMMUTABLE = "public, max-age=31536000, immutable"

#: No script, no subresources, an origin of its own.
_STRICT_POLICY = "default-src 'none'; sandbox; base-uri 'none'"
#: An HTML report: its own inline script and style and embedded images, in a
#: sandbox without this origin (no `allow-same-origin`), unable to fetch,
#: post a form or load anything from anywhere (`default-src 'none'`).
_HTML_REPORT_POLICY = (
    "default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; sandbox allow-scripts; base-uri 'none'; form-action 'none'"
)


@router.get("/artifacts/{artifact_path:path}")
def artifact_file(artifact_path: str, service: Service = Depends(service_of)) -> FileResponse:
    """One file out of the local artifact directory (DESIGN.md §6.4) —
    ``/artifacts/{run_id}/{dispatch_id}/…``.

    1. **Containment.** The path is attacker-influenced. It is resolved fully
       and refused unless it ends up strictly inside the configured directory
       — `..`, absolute paths and symlinks out of the tree are one rule. A
       refusal is a 404, the same as a miss.
    2. **Sandboxed delivery.** Artifact bytes are untrusted content on the
       API's origin. Every file goes out with ``sandbox`` and ``nosniff``: a
       sandboxed document has an origin of its own, so nothing in it can read
       or call this origin as the user. A report that is an HTML *application*
       — a test framework's log is one — may run its own inline script and
       style inside that sandbox, and may load nothing and connect to
       nothing; every other type may not run script at all.
    """
    file = service.artifacts.resolve_file(artifact_path)
    media_type, _ = mimetypes.guess_type(file.name)
    return FileResponse(
        str(file),
        media_type=media_type or "application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": _HTML_REPORT_POLICY if media_type == "text/html" else _STRICT_POLICY,
            "Referrer-Policy": "no-referrer",
        },
    )


class _ImmutableStatic(StaticFiles):
    async def get_response(self, path: str, scope: Any) -> Any:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = _IMMUTABLE
        return response


def mount_ui(app: FastAPI) -> None:
    """The pre-bundled UI (§10), if this build carries it."""
    ui_root = resources.files("runcomposer") / "ui_dist"
    if not ui_root.is_dir():  # headless install: keep / informative instead of 404

        @app.get("/", include_in_schema=False)
        def no_ui() -> dict[str, str]:
            return {
                "runcomposer": __version__,
                "detail": "UI assets not bundled in this build; the API lives under /api/v1",
            }

        return

    app.mount("/assets", _ImmutableStatic(directory=str(ui_root / "assets")), name="ui-assets")

    @app.get("/", include_in_schema=False)
    def ui_index() -> FileResponse:
        return FileResponse(str(ui_root / "index.html"), headers=_REVALIDATE)

    @app.get("/locales/{locale}.json", include_in_schema=False)
    def ui_locale(locale: str) -> FileResponse:
        target = ui_root / "locales" / f"{locale}.json"
        if not locale.replace("-", "").replace("_", "").isalnum() or not target.is_file():
            raise NotFound(f"unknown locale {locale!r}")
        return FileResponse(str(target), headers=_REVALIDATE)
