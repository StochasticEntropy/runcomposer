"""Health, meta, runners, catalog, taxonomy — what a client asks before it
does anything."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from runcomposer import __version__
from runcomposer.app.service import Service
from runcomposer.core import lifecycle
from runcomposer.core.registry import installed_plugins

from . import service_of

router = APIRouter(prefix="/api/v1", tags=["meta"])


@router.get("/health")
def health() -> dict[str, Any]:
    """Core health only — is this process serving."""
    return {"status": "ok", "version": __version__}


@router.get("/meta")
def meta(service: Service = Depends(service_of)) -> dict[str, Any]:
    """Everything a client needs at boot, in one call: version, default
    locale, the dispatchable runners with their per-run parameters, the size
    of the catalog, whether a taxonomy is configured, whether the API
    documentation viewers are served (``api.docs``), what the deployment
    loads (``configured``) and what is installed (``plugins``)."""
    config = service.config
    stats = service.catalog.stats()
    return {
        "version": __version__,
        "locale_default": config.locale_default,
        "runners": service.runner_infos(health=False),
        "catalog": stats,
        "taxonomy": {"configured": config.taxonomy_configured},
        "ingestion": {"tokens": config.ingestion["tokens"], "inbox": config.inbox_dir is not None},
        # Whether the API documentation viewers (/docs, /redoc) are served —
        # `core.api.docs`; /openapi.json always is.
        "api": {"docs": config.api.get("docs") is True},
        "states": list(lifecycle.RUN_STATES),
        "completions": list(lifecycle.COMPLETIONS),
        "ui": dict(config.ui),
        # What this deployment loads (entry-point and `module:` plugins alike)…
        "configured": service.configured_plugins(),
        # …and every entry-point plugin installed in the environment.
        "plugins": [
            {
                "group": plugin.group,
                "name": plugin.name,
                "distribution": plugin.distribution,
                "version": plugin.version,
            }
            for plugin in installed_plugins()
        ],
    }


@router.get("/ui-config")
def ui_config(service: Service = Depends(service_of)) -> dict[str, Any]:
    """The deployment's ``ui:`` section. Every key is served as configured,
    except the selections, which are validated at startup and served
    normalized: each of ``quick_filters`` and ``presets``, and
    ``default_filter``, carries ``tag_filter`` (the expression, in the
    grammar ``selection.tag_filter`` takes), ``text`` (one line to show) and
    ``kind`` (``pattern`` for a single pattern, ``expression`` otherwise)."""
    return {"locale_default": service.config.locale_default, **service.config.ui}


@router.get("/presets")
def presets(service: Service = Depends(service_of)) -> dict[str, Any]:
    """The named starting points this deployment configured under
    ``ui.presets``: a filter, and optionally a title, labels, and a runner
    with its per-run parameters — everything ``POST /runs`` needs."""
    ui = service.config.ui
    return {
        "presets": list(ui.get("presets") or []),
        "default_filter": ui.get("default_filter"),
        "quick_filters": list(ui.get("quick_filters") or []),
    }


@router.get("/runners")
def runners(
    refresh: bool = Query(False, description="probe every runner's health now"),
    service: Service = Depends(service_of),
) -> list[dict[str, Any]]:
    """The dispatchable runners — each under the id it is configured (and
    dispatched) by — with capabilities, per-run parameters and health. Health
    is cached for 30 s; ``refresh=true`` probes now."""
    return service.runner_infos(refresh=refresh)


@router.get("/catalog/tags")
def catalog_tags(service: Service = Depends(service_of)) -> dict[str, Any]:
    """Every tag of the catalog with the number of items carrying it."""
    counts = service.catalog.tag_counts()
    stats = service.catalog.stats()
    return {
        "tags": [{"tag": tag, "count": counts[tag]} for tag in sorted(counts)],
        "items": stats["items"],
        "snapshot": stats["snapshot"],
    }


@router.post("/catalog/reload")
def catalog_reload(service: Service = Depends(service_of)) -> dict[str, Any]:
    """Re-read the corpus now. Sources that can tell cheaply whether they
    changed are reloaded on their own; this forces the rest."""
    reloaded = service.catalog.refresh(force=True)
    return {"reloaded": reloaded, **service.catalog.stats()}


@router.get("/taxonomy")
def taxonomy(
    resolve: bool = Query(False), service: Service = Depends(service_of)
) -> dict[str, Any]:
    # `resolve` is opt-in: the unresolved body is the file, verbatim. With it
    # the same node grammar comes back resolved against the live catalog.
    return service.resolved_taxonomy() if resolve else service.taxonomy()
