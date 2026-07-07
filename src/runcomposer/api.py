"""The HTTP API (DESIGN.md §9) and the UI host (§10).

P1 ships the compose/preview/read surface; the ingestion push endpoint and
quarantine views arrive with P2. The pre-built UI (if bundled) is served at
``/``; without it, ``/`` answers with a JSON hint so headless installs stay
usable.
"""

from __future__ import annotations

from importlib import resources
from typing import Any

import yaml
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from runcomposer import __version__
from runcomposer.config import Config
from runcomposer.core.filter import FilterError
from runcomposer.core.selection import SelectionError
from runcomposer.core.model import Item, RunRecord
from runcomposer.service import Service, ServiceError

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


def create_app(config: Config) -> FastAPI:
    service = Service(config)
    # §8: unknown active plugin ids fail startup loudly. Resolve every
    # configured plugin NOW — a server that boots healthy and 500s on first
    # touch is exactly what this rule forbids. Raises ConfigError.
    service.store
    service.source
    for runner_id in config.configured_runner_ids():
        config.resolve_runner_class(runner_id)

    app = FastAPI(title="runcomposer", version=__version__)
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
        return _run_json(run, summary=service.verdict_summary(run_id))

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
