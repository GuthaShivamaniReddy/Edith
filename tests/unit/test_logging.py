from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from edith.observability.logging import configure_logging, get_logger, new_trace
from edith.safety import secrets


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_lines_carry_trace_id(tmp_path: Path) -> None:
    path = configure_logging(tmp_path)
    trace_id = new_trace(source="cli")
    get_logger("t").info("hello", n=1)
    rows = _lines(path)
    assert rows[-1]["event"] == "hello"
    assert rows[-1]["trace_id"] == trace_id
    assert rows[-1]["source"] == "cli"


def test_new_trace_replaces_previous(tmp_path: Path) -> None:
    path = configure_logging(tmp_path)
    first = new_trace()
    second = new_trace()
    get_logger("t").info("x")
    assert first != second
    assert _lines(path)[-1]["trace_id"] == second


def test_secret_keys_are_masked_but_token_counts_are_not(tmp_path: Path) -> None:
    path = configure_logging(tmp_path)
    get_logger("t").info(
        "call",
        api_key="sk-ant-zzz",
        access_token="abc",
        password="hunter2",
        input_tokens=120,
        output_tokens=30,
        headers={"Authorization": "Bearer abc", "accept": "json"},
    )
    row = _lines(path)[-1]
    assert row["api_key"] == "***"
    assert row["access_token"] == "***"
    assert row["password"] == "***"
    assert row["input_tokens"] == 120
    assert row["output_tokens"] == 30
    assert row["headers"] == {"Authorization": "***", "accept": "json"}


def test_known_secret_values_never_reach_the_log(tmp_path: Path) -> None:
    path = configure_logging(tmp_path)
    secrets.set_secret("anthropic_api_key", "sk-ant-supersecret-9999")
    get_logger("t").info("oops", detail="failed with key sk-ant-supersecret-9999 in url")
    text = path.read_text(encoding="utf-8")
    assert "sk-ant-supersecret-9999" not in text
    assert "failed with key *** in url" in text
