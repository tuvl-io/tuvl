"""Dev API: the routes that read and steer the running engine (schedules, memory, breakpoints, judges, storage).

Mounted only in dev mode, behind the dev key (:class:`tuvl.core.dev.middleware.DevMiddleware`). They stay in the
engine because they act on the live runtime and its registries. The file-based developer API (project files,
specs, jobs, validation, canvas, ...) is mounted by `tuvl.core.dev.extensions` when `tuvl-insight` is installed.

FastAPI matches in registration order, so the modules are included in the order the routes were first declared.
"""

from __future__ import annotations

from fastapi import APIRouter

from tuvl.api.dev import breakpoints, judges, memory, schedules, storage

router = APIRouter(prefix="/dev", tags=["dev"])

for _module in (schedules, memory, breakpoints, judges, storage):
    router.include_router(_module.router)

__all__ = ["router"]
