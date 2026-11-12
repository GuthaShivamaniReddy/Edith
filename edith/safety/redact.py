"""Masking for anything written to logs or the audit trail."""

from __future__ import annotations

import re
from typing import Any

from edith.safety.secrets import known_secret_values

REDACTED = "***"

# Keys whose *last word* names a secret: api_key, access_token, client_secret, password...
# Anchored so counters like input_tokens / output_tokens are not masked.
SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|apikey|token|secret|password|passwd|authorization|auth|cookie)$",
    re.IGNORECASE,
)


def redact(value: Any, max_str: int | None = None) -> Any:
    """Mask secret-named keys and known secret values; optionally truncate long strings."""
    return _redact(value, known_secret_values(), max_str)


def _redact(value: Any, secrets: frozenset[str], max_str: int | None) -> Any:
    if isinstance(value, str):
        for s in secrets:
            if s and s in value:
                value = value.replace(s, REDACTED)
        if max_str is not None and len(value) > max_str:
            value = value[:max_str] + f"… [{len(value) - max_str} more chars]"
        return value
    if isinstance(value, dict):
        return {
            k: REDACTED if SECRET_KEY.search(str(k)) else _redact(v, secrets, max_str)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(_redact(v, secrets, max_str) for v in value)
    return value
