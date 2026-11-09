"""The one place the engine reaches for the developer tooling's HTTP routes.

In dev mode the engine serves `/dev/ping`, the artifact routes and the dev-key gate itself. Everything else under
`/dev` (project files, specs, jobs, settings, the editor's probes) is developer tooling: this seam mounts it.
"""

from __future__ import annotations

from fastapi import FastAPI


def mount_dev_extensions(app: FastAPI, *, include_in_schema: bool) -> None:
    from tuvl.api.dev import router as dev_api_router
    from tuvl.core.dev.router import router as dev_files_router

    app.include_router(dev_api_router, include_in_schema=include_in_schema)
    app.include_router(dev_files_router, include_in_schema=include_in_schema)
