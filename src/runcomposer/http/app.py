"""The app factory (DESIGN.md §9).

``create_app`` builds the one ``Service`` of the process, validates the whole
deployment before the server accepts a request (§8: fail loudly, at startup),
reconciles what a previous process left executing, and wires the routers.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from runcomposer import __version__
from runcomposer.app.config import Config
from runcomposer.app.inbox import InboxWatcher
from runcomposer.app.service import Service

from .errors import install_error_handlers
from .routers import files, ingestion, meta, runs, selection

__all__ = ["create_app"]

log = logging.getLogger("runcomposer.http")


def create_app(config: Config, *, service: Service | None = None) -> FastAPI:
    """Raises ``ConfigError`` / ``TaxonomyError`` for a deployment that is
    not startable — a server that boots healthy and fails on first touch is
    exactly what startup validation exists to prevent."""
    service = service or Service(config)
    service.validate()
    for dispatch in service.recover():
        log.warning("run %s was interrupted — marked STALE", dispatch.run_id)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # File-drop inbox (§5): a polling loop inside serve — not a scheduler
        # (§13). Disabled with `core.ingestion.inbox: null`.
        watcher: InboxWatcher | None = None
        if config.inbox_dir is not None:
            watcher = InboxWatcher(
                service,
                config.inbox_dir,
                poll_interval_s=float(config.ingestion["poll_interval_s"]),
                settle_s=config.ingestion.get("settle_s"),
            )
            watcher.start()
        try:
            yield
        finally:
            if watcher is not None:
                watcher.stop()
            service.close()

    app = FastAPI(title="runcomposer", version=__version__, lifespan=lifespan)
    app.state.service = service
    cors: Any = config.api.get("cors") or []
    if cors:
        app.add_middleware(
            CORSMiddleware, allow_origins=cors, allow_methods=["*"], allow_headers=["*"]
        )
    install_error_handlers(app)
    for module in (meta, selection, runs, ingestion, files):
        app.include_router(module.router)
    files.mount_ui(app)
    return app
