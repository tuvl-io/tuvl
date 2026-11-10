"""The whole `/dev` router as it is in a running dev server: the engine's live-state routes plus Insight's."""

from __future__ import annotations

from fastapi import APIRouter

from tuvl.api.dev import router as live_router
from tuvl_insight.api.dev import router as tooling_router

router = APIRouter()
router.include_router(live_router)
router.include_router(tooling_router)
