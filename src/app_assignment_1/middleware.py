"""Request-level middleware: request id, size limit and access log.

Written as plain ASGI so a streamed (SSE) response is never buffered.
"""

import re
from time import perf_counter
from typing import Any
from uuid import uuid4

from loguru import logger
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app_assignment_1.logs import request_fields, scrub

REQUEST_ID_HEADER = "X-Request-ID"
# A client-supplied id is kept only if it is short and plain, so a header can
# never carry log-forging text (newlines, JSON, escape codes) into the logs.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _too_large(limit: int) -> tuple[int, bytes]:
    """The 413 status and JSON body."""
    return 413, (
        b'{"detail":"Request body is too large; the limit is '
        + str(limit).encode()
        + b' bytes."}'
    )


class RequestContextMiddleware:
    """Give each request an id, cap its body, and log one line when it ends.

    The id comes from a safe `X-Request-ID` the client sent, or is new. It is
    returned in the same header and added to every log line of the request.
    A body over `max_body_bytes` gets 413, judged by Content-Length first and
    then by the bytes actually read, so a chunked upload cannot slip past.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        """Wrap `app`; bodies over `max_body_bytes` are refused."""
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI call."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v for k, v in scope["headers"]}
        supplied = headers.get("x-request-id", b"").decode("latin-1")
        request_id = supplied if _SAFE_ID.match(supplied) else uuid4().hex
        fields: dict[str, Any] = {}
        token = request_fields.set(fields)
        started = perf_counter()
        status = 500
        sent_start = False
        exceeded = False

        async def send_wrapped(message: Message) -> None:
            nonlocal status, sent_start
            if exceeded:
                # The body was cut short, so whatever the app made of it is
                # not the answer: replace it with the 413.
                if message["type"] == "http.response.start":
                    message = {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [(b"content-type", b"application/json")],
                    }
                else:
                    message = {
                        "type": "http.response.body",
                        "body": _too_large(self.max_body_bytes)[1],
                    }
            if message["type"] == "http.response.start":
                status = message["status"]
                sent_start = True
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        (REQUEST_ID_HEADER.lower().encode(), request_id.encode()),
                    ],
                }
            await send(message)

        seen = 0

        async def receive_limited() -> Message:
            nonlocal seen, exceeded
            if exceeded:
                return {"type": "http.request", "body": b"", "more_body": False}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_body_bytes:
                    # End the body here; the answer is swapped for a 413 in
                    # send_wrapped. Raising would be caught by FastAPI's body
                    # reader and turned into a 400.
                    exceeded = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        with logger.contextualize(request_id=request_id):
            try:
                declared = headers.get("content-length", b"").decode("latin-1")
                if declared.isdigit() and int(declared) > self.max_body_bytes:
                    await self._refuse(send_wrapped)
                else:
                    await self.app(scope, receive_limited, send_wrapped)
            except Exception as exc:
                logger.error("Unhandled error ({})", type(exc).__name__)
                if not sent_start:
                    await send_wrapped(
                        {
                            "type": "http.response.start",
                            "status": 500,
                            "headers": [(b"content-type", b"application/json")],
                        }
                    )
                    await send_wrapped(
                        {
                            "type": "http.response.body",
                            "body": b'{"detail":"Internal server error"}',
                        }
                    )
            finally:
                request_fields.reset(token)
            route = scope.get("route")
            logger.bind(
                **scrub(
                    {
                        "method": scope["method"],
                        "endpoint": getattr(route, "path", scope["path"]),
                        "status": status,
                        "duration_ms": round((perf_counter() - started) * 1000, 1),
                        **fields,
                    }
                )
            ).log("INFO" if status < 500 else "ERROR", "request completed")

    async def _refuse(self, send: Send) -> None:
        """Answer 413."""
        status, body = _too_large(self.max_body_bytes)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})
