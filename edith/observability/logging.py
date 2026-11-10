"""Structured JSON logging with a trace ID per request.

Logs go to <data_dir>/logs/edith.jsonl, not the terminal, so they never clutter
`edith chat`. Every line carries the current trace_id (bound with new_trace()).
A redaction processor masks secret-looking keys and any known secret value.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, TextIO

import structlog

from edith.safety.secrets import known_secret_values

# Keys whose *last word* names a secret: api_key, access_token, client_secret, password...
# Anchored so counters like input_tokens / output_tokens are not masked.
_SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|apikey|token|secret|password|passwd|authorization|auth|cookie)$",
    re.IGNORECASE,
)
REDACTED = "***"

_log_file: TextIO | None = None


def _redact_value(value: Any, secrets: frozenset[str]) -> Any:
    if isinstance(value, str):
        for s in secrets:
            if s and s in value:
                value = value.replace(s, REDACTED)
        return value
    if isinstance(value, dict):
        return {
            k: REDACTED if _SECRET_KEY.search(str(k)) else _redact_value(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(_redact_value(v, secrets) for v in value)
    return value


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    secrets = known_secret_values()
    for key in list(event_dict):
        if _SECRET_KEY.search(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact_value(event_dict[key], secrets)
    return event_dict


def configure_logging(log_dir: Path, level: str | None = None) -> Path:
    """Send structlog output as JSON lines to log_dir/edith.jsonl. Returns the file path."""
    global _log_file
    level_name = (level or os.environ.get("EDITH_LOG_LEVEL") or "INFO").upper()
    log_level = logging.getLevelNamesMapping().get(level_name, logging.INFO)

    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "edith.jsonl"
    if _log_file is not None:
        _log_file.close()
    _log_file = path.open("a", encoding="utf-8", buffering=1)  # kept open for the process

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.WriteLoggerFactory(file=_log_file),
        cache_logger_on_first_use=False,
    )
    return path


def new_trace(**fields: Any) -> str:
    """Start a new trace: binds a fresh trace_id (plus fields) to every log line after it."""
    trace_id = uuid.uuid4().hex[:16]
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(trace_id=trace_id, **fields)
    return trace_id


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
