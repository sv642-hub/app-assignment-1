"""Server-Sent Events for a slow answer.

`POST /v1/chat/stream` runs the same flow as `/v1/chat`. Instead of one
reply after the wait, the client gets events as the request moves on:

- `progress`: a stage (`routing`, `generating`, `waiting`) with a short
  message fit to show a customer. `waiting` repeats every
  `APP_STREAM_HEARTBEAT_SECONDS` while a model is slow, so the page and any
  proxy in between see traffic and the customer sees that work continues.
- `result`: the same JSON as `/v1/chat`. The stream then ends.
- `error`: the status the plain route would have sent, a stable `code`, and a
  message fit to show a customer. The stream then ends.

Every stream ends with exactly one `result` or `error`.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from time import monotonic
from typing import Any

from fastapi.encoders import jsonable_encoder
from loguru import logger

from app_assignment_1.logs import annotate
from app_assignment_1.service import BackendUnavailable, DeadlineExceeded, Gateway

PROGRESS_TEXT = {
    "routing": "Reading your message and choosing the right model.",
    "generating": "A model is writing your answer.",
    "waiting": "Still working. The model is taking longer than usual.",
}
ERROR_TEXT = {
    "deadline_exceeded": (
        "Your answer took too long, so it was stopped. Please try again in a moment."
    ),
    "no_model_available": (
        "No model can answer right now. Please try again in a minute."
    ),
    "internal_error": (
        "Something went wrong on our side. Please try again; if it keeps "
        "happening, give the X-Request-ID header of this response to support."
    ),
}


def sse(event: str, data: dict[str, Any]) -> str:
    """One SSE frame: the event name and its JSON data on a single line."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def error_event(exc: BaseException) -> tuple[int, str]:
    """The status and stable code for a failed request."""
    if isinstance(exc, DeadlineExceeded):
        return 503, "deadline_exceeded"
    if isinstance(exc, BackendUnavailable):
        return 503, "no_model_available"
    return 500, "internal_error"


async def stream_chat(
    gateway: Gateway, message: str, heartbeat_seconds: float
) -> AsyncIterator[str]:
    """Yield the SSE frames for one chat request.

    If the client goes away the generator is closed, and the request is
    cancelled; the gateway still finishes saving what it already counted.
    """
    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    def progress(stage: str, details: dict[str, Any]) -> None:
        """Queue a stage change for the stream."""
        queue.put_nowait(
            ("progress", {"stage": stage, "message": PROGRESS_TEXT[stage], **details})
        )

    task = asyncio.create_task(gateway.chat(message, progress))
    task.add_done_callback(lambda _: queue.put_nowait(("done", {})))
    started = monotonic()
    try:
        while True:
            try:
                name, data = await asyncio.wait_for(queue.get(), heartbeat_seconds)
            except TimeoutError:
                yield sse(
                    "progress",
                    {
                        "stage": "waiting",
                        "message": PROGRESS_TEXT["waiting"],
                        "elapsed_seconds": round(monotonic() - started, 1),
                    },
                )
                continue
            if name == "done":
                break
            yield sse(name, data)
        try:
            response = task.result()
        except Exception as exc:
            status, code = error_event(exc)
            if status >= 500 and code == "internal_error":
                logger.error("Chat stream failed ({})", type(exc).__name__)
            annotate(outcome=code)
            yield sse(
                "error", {"status": status, "code": code, "message": ERROR_TEXT[code]}
            )
        else:
            annotate(
                model=response.trace.model,
                route=response.trace.route,
                outcome="result",
            )
            yield sse("result", jsonable_encoder(response))
    finally:
        if not task.done():
            task.cancel()
