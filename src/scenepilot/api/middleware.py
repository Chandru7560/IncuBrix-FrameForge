"""Request correlation and request-size guards."""

from __future__ import annotations

import re
import uuid

from starlette.responses import JSONResponse

from scenepilot.errors import error_body
from scenepilot.observability import correlation_id_context


class CorrelationAndSizeMiddleware:
    def __init__(self, app, *, max_request_bytes: int) -> None:
        self.app = app
        self.max_request_bytes = max_request_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", ())
        }
        supplied = headers.get("x-correlation-id", "")
        correlation_id = (
            supplied
            if len(supplied) <= 128 and re.fullmatch(r"[A-Za-z0-9._:-]+", supplied)
            else str(uuid.uuid4())
        )
        token = correlation_id_context.set(correlation_id)
        try:
            content_length = headers.get("content-length")
            try:
                too_large = content_length is not None and int(content_length) > self.max_request_bytes
            except ValueError:
                too_large = True

            if too_large:
                await self._send_too_large(scope, receive, send, correlation_id)
                return

            bytes_received = 0
            response_replaced = False

            async def limited_receive():
                nonlocal bytes_received, too_large
                message = await receive()
                if message["type"] == "http.request":
                    bytes_received += len(message.get("body", b""))
                    if bytes_received > self.max_request_bytes:
                        too_large = True
                        return {"type": "http.request", "body": b"", "more_body": False}
                return message

            async def correlated_send(message):
                nonlocal response_replaced
                if message["type"] == "http.response.start":
                    if too_large:
                        response_replaced = True
                        await self._send_too_large(scope, receive, send, correlation_id)
                        return
                    message["headers"] = list(message.get("headers", [])) + [
                        (b"x-correlation-id", correlation_id.encode("ascii"))
                    ]
                if not response_replaced:
                    await send(message)

            await self.app(scope, limited_receive, correlated_send)
        finally:
            correlation_id_context.reset(token)

    async def _send_too_large(self, scope, receive, send, correlation_id: str) -> None:
        response = JSONResponse(
            status_code=413,
            content=error_body(
                "REQUEST_TOO_LARGE",
                "The request exceeds the configured request size limit.",
                details={"max_request_bytes": self.max_request_bytes},
            ),
            headers={"X-Correlation-Id": correlation_id},
        )
        await response(scope, receive, send)
