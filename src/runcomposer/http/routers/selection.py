"""Selection preview: compile and spec-preview. Read-only — nothing is
stored, so both are safe to call on every keystroke and concurrently."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from runcomposer.app.service import Service
from runcomposer.app.views import item_json

from ..models import CompileRequest, SpecPreviewRequest
from . import service_of

router = APIRouter(prefix="/api/v1/selection", tags=["selection"])


@router.post("/compile")
def compile_selection(body: CompileRequest, service: Service = Depends(service_of)) -> dict[str, Any]:
    """Matched items + warnings, the size of the whole catalog (``total``),
    and — for a history selection — which run it resolved to
    (``derived_from``)."""
    preview = service.preview(body.selection())
    shown = preview.items if body.limit is None else preview.items[: body.limit]
    return {
        "count": len(preview.items),
        "total": preview.catalog_size,
        "items": [item_json(item) for item in shown],
        "warnings": preview.warnings,
        "derived_from": preview.derived_from,
        "snapshot": preview.snapshot,
    }


@router.post("/spec-preview")
def spec_preview(body: SpecPreviewRequest, service: Service = Depends(service_of)) -> dict[str, Any]:
    """The would-be run spec — composed, validated, not stored."""
    result = service.build_spec_document(
        body.selection,
        title=body.title,
        labels=body.labels or None,
        runner_section=body.runner,
        expect_format=body.expect_format,
    )
    return {
        "spec": result.spec,
        "warnings": result.warnings,
        "derived_from": result.spec["selection"].get("derived_from", []),
    }
