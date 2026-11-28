"""Logging for the gateway, built on loguru.

One line per event, as JSON by default (`APP_LOG_FORMAT=text` for people).
Every line written while a request is handled carries its `request_id`, the
same value the client gets back in the `X-Request-ID` header.

What never goes in a log: the customer's message, a model's reply, a key, a
token, a password or a database URL. Log call sites pass only counts, codes,
names and durations. As a second guard, `scrub` drops any structured field
whose name says it holds one of those, so a careless `logger.bind(reply=...)`
cannot leak it.
"""

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

from loguru import logger

# Field names that are never written, whatever their value.
FORBIDDEN_FIELDS = frozenset(
    {
        "message",
        "reply",
        "text",
        "prompt",
        "content",
        "api_key",
        "authorization",
        "token",
        "password",
        "secret",
        "database_url",
    }
)

# Per-request details filled in while the request runs (model, tokens...) and
# written on the request's access line. Each request gets its own dict.
request_fields: ContextVar[dict[str, Any] | None] = ContextVar(
    "request_fields", default=None
)

_handler_id: int | None = None


def annotate(**fields: Any) -> None:
    """Add details to the access line of the request being handled.

    Does nothing outside a request. Fields whose name is forbidden are
    dropped when the line is written.
    """
    bag = request_fields.get()
    if bag is not None:
        bag.update({k: v for k, v in fields.items() if v is not None})


def add_tokens(prompt: int | None, completion: int | None) -> None:
    """Add one model call's token counts to the request's running totals."""
    bag = request_fields.get()
    if bag is None:
        return
    for key, value in (("prompt_tokens", prompt), ("completion_tokens", completion)):
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            bag[key] = bag.get(key, 0) + value


def scrub(extra: dict[str, Any]) -> dict[str, Any]:
    """Copy of `extra` without any field whose name is forbidden."""
    return {k: v for k, v in extra.items() if k.lower() not in FORBIDDEN_FIELDS}


def _json_sink(message: Any) -> None:
    """Write one log record as a JSON line on stderr."""
    record = message.record
    entry: dict[str, Any] = {
        "time": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": record["name"],
        "message": record["message"],
        **scrub(record["extra"]),
    }
    exc = record["exception"]
    if exc is not None and exc.type is not None:
        # The type only: a traceback or message can quote private data.
        entry["error_type"] = exc.type.__name__
    sys.stderr.write(json.dumps(entry, default=str) + "\n")


def _text_format(record: Any) -> str:
    """Readable one-line format, with the request id when there is one."""
    extra = scrub(record["extra"])
    rid = extra.pop("request_id", None)
    tail = " ".join(f"{k}={v}" for k, v in sorted(extra.items()))
    prefix = f"[{rid}] " if rid else ""
    record["extra"]["_tail"] = f" {tail}" if tail else ""
    return (
        "{time:YYYY-MM-DD HH:mm:ss} {level:<7} {name} "
        + prefix.replace("{", "{{").replace("}", "}}")
        + "{message}{extra[_tail]}\n"
    )


class _InterceptHandler(logging.Handler):
    """Send standard-library logging (uvicorn, psycopg) through loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        """Forward one stdlib record to loguru at the same level."""
        if getattr(record, "from_loguru", False):
            return  # a test fixture handed it to logging; do not loop back
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.opt(depth=6, exception=record.exc_info).bind(
            stdlib_logger=record.name
        ).log(level, record.getMessage())


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Send all logs, ours and the standard library's, to stderr through
    loguru. Safe to call again: it replaces only its own handler."""
    global _handler_id
    if _handler_id is not None:
        logger.remove(_handler_id)
    else:
        try:
            logger.remove(0)  # loguru's default handler
        except ValueError:
            pass
    if fmt == "json":
        _handler_id = logger.add(_json_sink, level=level)
    else:
        _handler_id = logger.add(sys.stderr, level=level, format=_text_format)
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not isinstance(h, _InterceptHandler)]
    root.addHandler(_InterceptHandler())
    root.setLevel(0)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    # The request middleware writes the access line; uvicorn's would repeat it.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("psycopg").setLevel(logging.ERROR)
