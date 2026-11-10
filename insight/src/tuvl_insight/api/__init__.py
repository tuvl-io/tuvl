"""Insight's HTTP surface: the developer API under `/dev` and the code-assist routes."""

from __future__ import annotations

from fastapi import FastAPI


def mount_dev(app: FastAPI, *, include_in_schema: bool) -> None:
    """Register the file-based developer routes on *app* (dev mode only; the caller gates it)."""
    from tuvl_insight.api.dev import router as dev_router
    from tuvl_insight.api.dev_core_router import router as project_router

    app.include_router(dev_router, include_in_schema=include_in_schema)
    app.include_router(project_router, include_in_schema=include_in_schema)
