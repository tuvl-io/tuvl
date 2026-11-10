"""Dev API v2 (implementation plan P4.0): what Insight needs to author, check
and plan a 2.0 project. Mounted only in dev mode, behind the dev key
(:class:`tuvl.core.dev.middleware.DevMiddleware`).

Responses reuse the CLI's documented ``--json`` shapes
(:mod:`tuvl.cli.json_output`) so Insight, ``tuvl serve mcp`` and coding agents see
the same data. Files are read and written as text, so YAML comments survive a
round trip through the canvas.

One module per area; each owns a router that is included here in the order the
routes were first declared, since FastAPI matches in registration order. The routes
that act on the live engine are in `tuvl.api.dev`.
"""

from __future__ import annotations

from fastapi import APIRouter

from tuvl_insight.api.dev import agents, canvas, files, jobs, probe, specs, telemetry, validation

router = APIRouter(prefix="/dev", tags=["dev"])

for _module in (files, validation, specs, telemetry, jobs, agents, canvas, probe):
    router.include_router(_module.router)

__all__ = ["router"]
