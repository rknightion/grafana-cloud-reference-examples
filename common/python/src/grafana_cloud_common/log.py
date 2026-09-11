"""Structured JSON logging for the function's own telemetry.

This is the log the *function* emits about itself, into CloudWatch. It is not the
data being shipped to Loki - keep the two straight, because a debug-level line
about a batch is not a customer log line.

JSON rather than text so a CloudWatch Logs Insights query or a Grafana Cloud
CloudWatch integration can filter on fields without regex. One line per event,
no multi-line tracebacks unless the level is ERROR.

Deliberately not using `print`, and the ruff config bans it, because print gives
no level and no correlation fields.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

_REDACTED = "***redacted***"

# Anything whose key looks like one of these is replaced before serialisation.
# A log line carrying a Cloud Access Policy token is a credential leak into
# CloudWatch, which is then readable by anyone with logs:FilterLogEvents.
_SENSITIVE_KEY_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "credential",
    "api_key",
    "apikey",
)


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def redact(fields: dict[str, Any]) -> dict[str, Any]:
    """Replace values whose key names a credential. Applied to every log line."""
    return {key: (_REDACTED if _is_sensitive(key) else value) for key, value in fields.items()}


class JsonFormatter(logging.Formatter):
    """Render a record as one JSON object.

    Lambda already prefixes each line with the request id, but that prefix is not
    part of the JSON, so ``aws_request_id`` is emitted as a field too - otherwise
    correlating in Loki or Insights needs a regex against the prefix.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "timestamp_ms": int(record.created * 1000),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


class FieldLogger:
    """Thin wrapper that makes structured fields the normal case.

    ``logger.info("pushed batch", lines=412, stream_count=3)`` rather than
    f-string interpolation, so the fields stay queryable.
    """

    __slots__ = ("_bound", "_logger")

    def __init__(self, logger: logging.Logger, bound: dict[str, Any] | None = None) -> None:
        self._logger = logger
        self._bound = bound or {}

    def bind(self, **fields: Any) -> FieldLogger:
        """Return a logger that adds these fields to every subsequent line."""
        return FieldLogger(self._logger, {**self._bound, **fields})

    def _emit(self, level: int, message: str, exc_info: bool, fields: dict[str, Any]) -> None:
        self._logger.log(
            level,
            message,
            exc_info=exc_info,
            extra={"fields": {**self._bound, **fields}},
        )

    def debug(self, message: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, message, False, fields)

    def info(self, message: str, **fields: Any) -> None:
        self._emit(logging.INFO, message, False, fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._emit(logging.WARNING, message, False, fields)

    def error(self, message: str, *, exc_info: bool = True, **fields: Any) -> None:
        self._emit(logging.ERROR, message, exc_info, fields)


def configure(level: str | None = None) -> None:
    """Install the JSON formatter on the root logger. Call once, at cold start.

    The Lambda Python runtime installs its own root handler before user code
    runs, so adding a handler produces duplicate lines. Reconfigure the existing
    one instead.
    """
    resolved = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    root = logging.getLogger()
    root.setLevel(resolved)

    if root.handlers:
        for handler in root.handlers:
            handler.setFormatter(JsonFormatter())
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)

    # botocore logs every request at DEBUG, which at LOG_LEVEL=DEBUG buries the
    # function's own lines and costs real money in CloudWatch ingest.
    logging.getLogger("botocore").setLevel(max(logging.INFO, root.level))
    logging.getLogger("urllib3").setLevel(max(logging.INFO, root.level))


def get_logger(name: str, **fields: Any) -> FieldLogger:
    """Get a field logger. Bind the fields every line in this module should carry."""
    return FieldLogger(logging.getLogger(name), fields or None)
