"""The HTTP surface (DESIGN.md §9): a FastAPI app factory and one router per
area. ``create_app(config)`` is the whole public interface."""

from .app import create_app

__all__ = ["create_app"]
