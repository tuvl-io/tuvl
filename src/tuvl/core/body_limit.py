"""A cap on the size of a request body, enforced before the application has read it all."""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodyTooLargeError(Exception):
    """The bytes received so far are over the limit."""


class BodyLimitMiddleware:
    """Answers ``413`` to a request whose body is over *limit* bytes: at once by its ``Content-Length``,
    and otherwise by the bytes actually received (a chunked body declares no length).

    A multipart body is bounded by the upload settings of the trigger that takes it, and the dev API
    (``/dev``, which writes project files) is not capped."""

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.limit <= 0 or self._exempt(scope):
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.limit:
            await self._refuse(scope, receive, send)
            return

        received = 0
        started = False

        async def counted() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise BodyTooLargeError
            return message

        async def watched(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted, watched)
        except BodyTooLargeError:
            if started:
                raise
            await self._refuse(scope, receive, send)

    @staticmethod
    def _exempt(scope: Scope) -> bool:
        content_type = dict(scope["headers"]).get(b"content-type", b"").lower()
        path: str = scope["path"]
        return content_type.startswith(b"multipart/form-data") or path.startswith("/dev")

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"detail": f"The request body is larger than {self.limit} bytes."}, status_code=413
        )
        await response(scope, receive, send)
