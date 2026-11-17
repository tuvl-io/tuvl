"""Cross-origin access for browser apps (``spec.api.cors_origins`` / ``TUVL_CORS_ORIGINS``)."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from tuvl.core.logger import logger
from tuvl.core.system_config import cors_origins

#: what ``@tuvl/client`` sends; credentials are bearer tokens in a header, never cookies
ALLOW_HEADERS = ["Authorization", "Content-Type", "Accept", "Last-Event-ID"]
ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
#: a browser hides every response header but the simple ones unless it is listed here;
#: the client reads the run id to tell a finished run from a refused request, and the wait
#: after a throttled sign-in (429) to say when to try again
EXPOSE_HEADERS = ["X-Tuvl-Run-Id", "Retry-After"]


def install_cors(app: FastAPI) -> tuple[str, ...]:
    """Answer preflights and add CORS headers for the configured origins; return them.

    Call it after every other ``add_middleware``: the last one added is outermost, so a
    preflight is answered before the IP allowlist or auth sees it, and a ``401`` or ``403``
    still carries the headers the browser needs to show it.
    """
    origins = cors_origins()
    if not origins:
        return ()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(origins),
        allow_methods=ALLOW_METHODS,
        allow_headers=ALLOW_HEADERS,
        expose_headers=EXPOSE_HEADERS,
        allow_credentials=False,
        max_age=600,
    )
    if "*" in origins:
        logger.warning(
            "cors.any_origin", detail="any website may call this API with a token it holds"
        )
    logger.info("CORS active", origins=list(origins))
    return origins
