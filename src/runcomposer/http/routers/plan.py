"""Plan previews: what a dispatch would do, without doing it.

Read-only like the selection previews — nothing is stored, nothing is
executed, no dispatch, token or event comes into being — so a client may ask
whenever the selection, the runner or a parameter changes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from runcomposer.app.service import Service
from runcomposer.app.views import plan_json

from ..bodies import BodyLimitRoute
from ..models import PlanRequest, RunPlanRequest
from . import service_of

router = APIRouter(prefix="/api/v1", tags=["plan"], route_class=BodyLimitRoute)


@router.post("/plan")
def plan_selection(body: PlanRequest, service: Service = Depends(service_of)) -> dict[str, Any]:
    """The plan a runner would follow for a selection that is not stored —
    and is not stored by asking. ``parameters`` are validated exactly as a
    dispatch validates them (a bad one is a 400 naming it).

    The answer always says one of two things: ``plan`` — its text, the shards
    in declaration order with their group, items and estimate, the groups,
    notes — or ``refused: true`` with the ``refusal`` a dispatch would meet
    (the corpus drifted, the job has no way to carry the run out). A runner that does
    not declare the ``plan`` capability is a 400."""
    return plan_json(
        service.plan(
            body.runner,
            selection=body.selection,
            parameters=body.parameters,
            title=body.title,
            labels=body.labels or None,
        )
    )


@router.post("/runs/{run_id}/plan")
def plan_run(
    run_id: str, body: RunPlanRequest, service: Service = Depends(service_of)
) -> dict[str, Any]:
    """The plan a runner would follow for a stored run. Without
    ``parameters`` the ones the run's document carries for that runner are
    used — the preview of the dispatch ``POST /runs/{id}/dispatch`` with the
    same body would start. Allowed in any state of the run: a preview does
    not touch it."""
    return plan_json(service.plan(body.runner, run_id=run_id, parameters=body.parameters))
