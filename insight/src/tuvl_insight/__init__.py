"""Tuvl Insight — developer portal companion package for tuvl.

Discovered and mounted at runtime via ``tuvl.main``. Install with ``pip install tuvl[standard]``.
"""

from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
#: index.html names the build's hashed bundles: never reuse a stale copy after a new build
#: (and in dev mode it carries the session's key).
_NO_CACHE = {"Cache-Control": "no-cache"}


def mount(app: FastAPI) -> None:
    """Mount the Insight router and pre-compiled static UI onto *app*."""
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    assets_dir = _STATIC_DIR / "assets"
    index_file = _STATIC_DIR / "index.html"

    if index_file.exists():
        if assets_dir.exists():
            app.mount(
                "/insight/assets",
                StaticFiles(directory=str(assets_dir)),
                name="insight-assets",
            )

        from fastapi.responses import HTMLResponse

        from tuvl.core.config import settings

        def _get_index_html() -> HTMLResponse:
            import os

            try:
                content = index_file.read_text(encoding="utf-8")

                # Inject the dev key only when auto-login is requested via the CLI.
                if os.environ.get("TUVL_DEV_AUTO_LOGIN") == "1":
                    key = html.escape(settings.tuvl_dev_api_key or "", quote=True)
                    meta_tag = f'<meta name="tuvl-dev-key" content="{key}">'
                    if "<head>" in content:
                        content = content.replace("<head>", f"<head>{meta_tag}", 1)
                    else:
                        content = meta_tag + content

                return HTMLResponse(content=content, headers=_NO_CACHE)
            except Exception as exc:
                logger.error("insight.dev_key_inject.failed: %s", exc)
                return HTMLResponse(
                    content=index_file.read_text(encoding="utf-8"), headers=_NO_CACHE
                )

        @app.get("/insight", tags=["insight"], include_in_schema=False)
        @app.get("/insight/", tags=["insight"], include_in_schema=False)
        async def serve_insight_ui() -> HTMLResponse:
            return _get_index_html()

        @app.get("/insight/{path:path}", tags=["insight"], include_in_schema=False)
        async def serve_insight_static(path: str) -> FileResponse | HTMLResponse:
            # Serve root-level static files; fall back to index.html for unknown
            # paths so client-side React Router routes survive a hard refresh.
            # Containment check: candidate must stay inside the static root so
            # encoded traversal (``..%2f``) can't escape to arbitrary files.
            static_root = _STATIC_DIR.resolve()
            candidate = (static_root / path).resolve()
            if candidate.is_relative_to(static_root) and candidate.is_file():
                return FileResponse(str(candidate))
            return _get_index_html()

        logger.info("Tuvl Insight UI mounted at /insight")
    else:

        @app.get("/insight", tags=["insight"], include_in_schema=False)
        @app.get("/insight/", tags=["insight"], include_in_schema=False)
        async def insight_ui_missing() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "Tuvl Insight UI not compiled.",
                    "fix": "Run `make build-ui` to compile the frontend assets.",
                },
            )

        logger.warning(
            "Tuvl Insight UI static files not found at %s. Run `make build-ui` to compile.",
            _STATIC_DIR,
        )
