"""Structured, redaction-aware logging for the underwriting copilot."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

LOGGER_NAME = "loan_ai_underwriting_copilot"

_SENSITIVE_KEYS = {
    "nvidia_api_key",
    "api_key",
    "apikey",
    "authorization",
    "token",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "messages",
    "prompt",
    "payload",
    "raw_text",
    "transactions",
    "bank_statement",
}

_ID_LIKE = re.compile(r"\b(?:\d[ -]?){9,}\b")


def mask_identifier(value: str | None, keep: int = 4) -> str | None:
    """Mask everything but the last ``keep`` characters of an identifier."""
    if value is None:
        return None
    text = str(value)
    stripped = re.sub(r"\s+", "", text)
    if len(stripped) <= keep:
        return "*" * len(stripped)
    return "*" * (len(stripped) - keep) + stripped[-keep:]


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively drop or mask sensitive content before it reaches a log."""
    if _depth > 6:
        return "<truncated>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                out[key] = "<redacted>"
            else:
                out[key] = redact(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value][:25]
    if isinstance(value, str):
        return _ID_LIKE.sub(lambda m: mask_identifier(m.group(0)) or "", value)[:500]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            entry.update(redact(extra))
        if record.exc_info:
            entry["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else "Unknown"
        return json.dumps(entry, ensure_ascii=False, default=str)


def get_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
        logger.propagate = False
    return logger


def log_event(message: str, level: int = logging.INFO, **context: Any) -> None:
    get_logger().log(level, message, extra={"context": context})


@contextmanager
def node_span(
    node: str,
    packet_id: str | None = None,
    cycle_id: str | None = None,
    model: str | None = None,
    **extra: Any,
) -> Iterator[dict[str, Any]]:
    """Log start/finish of a pipeline node with duration, status and error type."""
    started = time.time()
    context: dict[str, Any] = {
        "node": node,
        "packet_id": packet_id,
        "pipeline_cycle_id": cycle_id,
        "model": model,
        **extra,
    }
    log_event("node.start", **context)
    try:
        yield context
    except Exception as exc:  # noqa: BLE001 - logged then re-raised
        log_event(
            "node.error",
            level=logging.ERROR,
            status="ERROR",
            error_type=type(exc).__name__,
            duration_ms=int((time.time() - started) * 1000),
            **context,
        )
        raise
    else:
        log_event(
            "node.finish",
            status=context.get("status", "OK"),
            duration_ms=int((time.time() - started) * 1000),
            **{k: v for k, v in context.items() if k != "status"},
        )
