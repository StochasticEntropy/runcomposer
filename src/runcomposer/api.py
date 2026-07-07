"""The HTTP API (DESIGN.md §9) and the UI host (§10).

P1 ships the compose/preview/read surface; the ingestion push endpoint and
quarantine views arrive with P2. The pre-built UI (if bundled) is served at
``/``; without it, ``/`` answers with a JSON hint so headless installs stay
usable.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import asynccontextmanager
from dataclasses import asdict
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from fastapi import Body, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from runcomposer import __version__
from runcomposer.config import Config
from runcomposer.core.filter import FilterError
from runcomposer.core.selection import SelectionError
from runcomposer.core.model import Item, RunRecord
from runcomposer.quarantine import QuarantineError
from runcomposer.service import (
    IngestError,
    IngestReport,
    QuarantineReport,
    Service,
    ServiceError,
    bundle_content_hash,
)

__all__ = ["create_app"]


def _item_json(item: Item) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.display_name,
        "tags": list(item.tags),
        "hierarchy": list(item.hierarchy),
    }


def _run_json(run: RunRecord, *, summary: dict[str, int] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": run.id,
        "title": run.title,
        "created_at": run.created_at,
        "origin": run.origin,
        "labels": dict(run.labels),
        "state": run.state,
        "completion": run.completion,
        "completed_at": run.completed_at,
        "dispatches": [
            {
                "dispatch_id": d.dispatch_id,
                "mode": d.mode,
                "declared_shards": d.declared_shards,
                "created_at": d.created_at,
            }
            for d in run.dispatches
        ],
        "deliveries": [
            {
                "delivery_id": d.delivery_id,
                "dispatch_id": d.dispatch_id,
                "shard": d.shard,
                "content_hash": d.content_hash,
                "format": d.format,
                "created_at": d.created_at,
            }
            for d in run.deliveries
        ],
    }
    if summary is not None:
        data["verdict_summary"] = summary
    return data


def _ingest_json(report: IngestReport) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "dispatch_id": report.dispatch_id,
        "shard": report.shard,
        "outcome": report.outcome,
        "verdict_count": report.verdict_count,
        "run_state": report.run_state,
        "completion": report.completion,
        "warnings": report.warnings,
    }


def create_app(config: Config) -> FastAPI:
    service = Service(config)
    # §8: unknown active plugin ids fail startup loudly. Resolve every
    # configured plugin NOW — a server that boots healthy and 500s on first
    # touch is exactly what this rule forbids. Raises ConfigError.
    service.store
    service.source
    for runner_id in config.configured_runner_ids():
        config.resolve_runner_class(runner_id)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # File-drop inbox (§5): a polling loop inside serve — not a scheduler
        # (§13). Disabled with `core.ingestion.inbox: null`.
        watcher = None
        if config.inbox_dir is not None:
            from runcomposer.inbox import InboxWatcher

            watcher = InboxWatcher(
                service, config.inbox_dir, poll_interval_s=config.ingestion["poll_interval_s"]
            )
            watcher.start()
        yield
        if watcher is not None:
            watcher.stop()

    app = FastAPI(title="runcomposer", version=__version__, lifespan=lifespan)
    cors = config.api.get("cors") or []
    if cors:
        app.add_middleware(
            CORSMiddleware, allow_origins=cors, allow_methods=["*"], allow_headers=["*"]
        )

    @app.exception_handler(SelectionError)
    @app.exception_handler(FilterError)
    @app.exception_handler(ServiceError)
    async def _bad_request(_, exc: Exception):  # noqa: ANN001 - FastAPI signature
        status = 404 if isinstance(exc, ServiceError) and "unknown run" in str(exc) else 400
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    @app.exception_handler(QuarantineError)
    async def _quarantine_not_found(_, exc: QuarantineError):  # noqa: ANN001
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    # -- read surfaces ------------------------------------------------------

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/taxonomy")
    def taxonomy() -> dict[str, Any]:
        return service.taxonomy()

    @app.get("/api/v1/ui-config")
    def ui_config() -> dict[str, Any]:
        return {"locale_default": config.locale_default, **config.ui}

    @app.get("/api/v1/runners")
    def runners() -> list[dict[str, Any]]:
        return service.runner_infos()

    # -- selection ----------------------------------------------------------

    @app.post("/api/v1/selection/compile")
    def compile_selection(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        items, warnings = service.preview(body)
        return {"count": len(items), "items": [_item_json(i) for i in items], "warnings": warnings}

    @app.post("/api/v1/selection/spec-preview")
    def spec_preview(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        result = service.build_spec_document(
            body.get("selection") or {},
            title=body.get("title", "Untitled run"),
            labels=body.get("labels"),
        )
        return {"spec": result.spec, "warnings": result.warnings}

    # -- runs -----------------------------------------------------------------

    @app.post("/api/v1/runs", status_code=201)
    def create_run(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        result = service.compose_run(
            body.get("selection") or {},
            title=body.get("title", "Untitled run"),
            labels=body.get("labels"),
            origin="api",
        )
        assert result.run is not None
        dispatch_req = body.get("dispatch") or {}
        response: dict[str, Any] = {"warnings": result.warnings}
        if dispatch_req.get("mode") == "export":
            import json as _json

            spec_bytes = _json.dumps(result.spec, indent=2).encode("utf-8")
            dispatch = service.export_dispatch(result.run.id, spec_bytes=spec_bytes)
            response["dispatch"] = {"dispatch_id": dispatch.dispatch_id, "mode": "export"}
            response["spec_document"] = result.spec
        elif dispatch_req.get("runner"):
            dispatch = service.dispatch_runner(result.run.id, dispatch_req["runner"])
            response["dispatch"] = {"dispatch_id": dispatch.dispatch_id, "mode": dispatch.mode}
        refreshed = service.store.get_run(result.run.id)
        assert refreshed is not None
        response["run"] = _run_json(refreshed, summary=service.verdict_summary(result.run.id))
        return response

    @app.get("/api/v1/runs")
    def list_runs(
        state: str | None = None,
        label: list[str] = Query(default=[]),
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List runs, filtered by state, labels (`label=key=value`, repeatable,
        all must match), and created_at time window (§9)."""
        labels: dict[str, str] = {}
        for pair in label:
            key, sep, value = pair.partition("=")
            if not sep or not key:
                raise HTTPException(status_code=400, detail=f"label filter must be key=value, got {pair!r}")
            labels[key] = value
        runs = service.store.list_runs(
            state=state, labels=labels or None, since=since, until=until, limit=limit
        )
        return {"runs": [_run_json(run) for run in runs]}

    @app.get("/api/v1/runs/{run_id}")
    def run_detail(run_id: str) -> dict[str, Any]:
        run = service.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"unknown run id {run_id!r}")
        data = _run_json(run, summary=service.verdict_summary(run_id))
        # Live per-item progress (§6.2a): while a listener-equipped run is
        # RUNNING these are the streamed verdicts; afterwards the reconciled
        # terminal ones.
        latest = run.dispatches[-1].dispatch_id if run.dispatches else None
        data["verdicts"] = [
            {
                "item_id": v.item_id,
                "status": v.status,
                "duration_ms": v.duration_ms,
                "message": v.message,
            }
            for v in service.store.verdicts_for(run_id, latest)
        ]
        spec = service.store.get_spec_document(run_id)
        data["planned_count"] = (
            spec.get("selection", {}).get("materialized", {}).get("count", 0) if spec else 0
        )
        return data

    @app.get("/api/v1/runs/{run_id}/items")
    def run_items(run_id: str) -> dict[str, Any]:
        spec = service.store.get_spec_document(run_id)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"unknown run id {run_id!r}")
        materialized = spec.get("selection", {}).get("materialized", {})
        return {"item_ids": materialized.get("item_ids", []), "count": materialized.get("count", 0)}

    @app.get("/api/v1/runs/{run_id}/spec")
    def run_spec(run_id: str, format: str = "json"):
        spec = service.store.get_spec_document(run_id)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"unknown run id {run_id!r}")
        if format == "yaml":
            return Response(
                content=yaml.safe_dump(spec, sort_keys=False, allow_unicode=True),
                media_type="application/yaml",
            )
        return spec

    @app.post("/api/v1/runs/{run_id}/finalize")
    def finalize(run_id: str) -> dict[str, Any]:
        return _run_json(service.finalize(run_id), summary=service.verdict_summary(run_id))

    # -- ingestion push (§5 transport 1, §9) ---------------------------------

    @app.post("/api/v1/runs/{run_id}/results")
    def push_results(
        run_id: str,
        files: list[UploadFile] = File(...),
        shard: str | None = Form(None),
        dispatch: str | None = Form(None),
        format: str | None = Form(None),
        authorization: str | None = Header(None),
        x_runcomposer_token: str | None = Header(None),
    ):
        """Multipart results push. Auth: `Authorization: Bearer <results.token>`
        (or X-Runcomposer-Token). The §5 security floor runs before anything
        is parsed or persisted."""
        max_bytes = int(config.ingestion["max_upload_mb"]) * 1024 * 1024
        temp_root = Path(tempfile.mkdtemp(prefix="runcomposer-push-"))
        try:
            total = 0
            for upload in files:
                name = Path(upload.filename or "artifact").name  # no path traversal
                with open(temp_root / name, "wb") as out:
                    while chunk := upload.file.read(1024 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise HTTPException(
                                status_code=413,
                                detail=f"upload exceeds core.ingestion.max_upload_mb "
                                f"({config.ingestion['max_upload_mb']} MB)",
                            )
                        out.write(chunk)

            run = service.store.get_run(run_id)
            if run is None:
                # Unsolicited (§4): visible in quarantine, never a run record.
                entry = service.quarantine.add(
                    temp_root,
                    reason="unknown-run",
                    transport="push",
                    content_hash=bundle_content_hash(temp_root),
                    claimed_run_id=run_id,
                    format=format or "runcomposer-verdicts",
                )
                return JSONResponse(
                    status_code=404,
                    content={
                        "detail": f"unknown run id {run_id!r} — delivery quarantined",
                        "quarantine_entry": entry.entry_id if entry else None,
                    },
                )

            token = x_runcomposer_token
            if token is None and authorization and authorization.lower().startswith("bearer "):
                token = authorization[len("bearer "):]
            token_state = service.verify_ingest_token(run_id, token)
            if token_state == "missing":
                raise HTTPException(status_code=401, detail="ingest token required (results.token, §5)")
            if token_state == "wrong":
                raise HTTPException(status_code=403, detail="ingest token does not match this run")

            result = service.ingest_or_quarantine(
                temp_root, transport="push", run_id=run_id, dispatch_id=dispatch, shard=shard
            )
            if isinstance(result, QuarantineReport):
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": result.detail,
                        "reason": result.reason,
                        "quarantine_entry": result.entry_id,
                    },
                )
            return _ingest_json(result)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    # -- quarantine (§4, §5, §9) ------------------------------------------------

    @app.get("/api/v1/quarantine")
    def quarantine_list() -> dict[str, Any]:
        return {"entries": [asdict(entry) for entry in service.quarantine.entries()]}

    @app.post("/api/v1/quarantine/{entry_id}/attach")
    def quarantine_attach(entry_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        run_id = body.get("run_id")
        if not run_id:
            raise HTTPException(status_code=400, detail="attach requires a run_id")
        try:
            return _ingest_json(service.attach_quarantined(entry_id, run_id))
        except IngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.post("/api/v1/quarantine/{entry_id}/promote")
    def quarantine_promote(entry_id: str) -> dict[str, Any]:
        try:
            return _ingest_json(service.promote_quarantined(entry_id))
        except IngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.delete("/api/v1/quarantine/{entry_id}", status_code=204)
    def quarantine_discard(entry_id: str) -> None:
        service.quarantine.remove(entry_id)

    # -- UI (§10): pre-bundled static assets, if present ------------------------

    ui_root = resources.files("runcomposer") / "ui_dist"
    if ui_root.is_dir():
        app.mount("/assets", StaticFiles(directory=str(ui_root / "assets")), name="ui-assets")

        @app.get("/", include_in_schema=False)
        def ui_index() -> FileResponse:
            return FileResponse(str(ui_root / "index.html"))

        @app.get("/locales/{locale}.json", include_in_schema=False)
        def ui_locale(locale: str) -> FileResponse:
            target = ui_root / "locales" / f"{locale}.json"
            if not target.is_file():
                raise HTTPException(status_code=404, detail=f"unknown locale {locale!r}")
            return FileResponse(str(target))

    else:  # headless install: keep / informative instead of 404

        @app.get("/", include_in_schema=False)
        def no_ui() -> dict[str, str]:
            return {
                "runcomposer": __version__,
                "detail": "UI assets not bundled in this build; the API lives under /api/v1",
            }

    return app
