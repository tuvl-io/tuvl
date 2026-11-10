"""The one place the engine reaches for the developer tooling's HTTP routes.

In dev mode the engine serves the live-state routes (`tuvl.api.dev`), `/dev/ping`, the artifact routes and the
dev-key gate itself. Everything else under `/dev` (project files, specs, jobs, settings, the editor's probes)
is developer tooling: this seam mounts it when `tuvl-insight` is installed.
"""

from __future__ import annotations

import importlib.util

from fastapi import FastAPI


def mount_dev_extensions(app: FastAPI, *, include_in_schema: bool) -> None:
    if importlib.util.find_spec("tuvl_insight") is None:
        return
    from tuvl_insight.api import mount_dev

    mount_dev(app, include_in_schema=include_in_schema)
