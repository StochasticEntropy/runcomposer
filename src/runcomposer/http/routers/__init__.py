"""Routers, one per area of DESIGN.md §9."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from runcomposer.app.service import Service

__all__ = ["service_of"]


def service_of(request: Request) -> Service:
    """The one Service of this app (built in ``create_app``)."""
    service: Service = request.app.state.service
    return service
