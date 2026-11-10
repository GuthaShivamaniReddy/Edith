from __future__ import annotations

import json

import pytest

from edith.config import EdithConfig
from edith.core.brain.claude_brain import ClaudeBrain
from edith.core.events import ErrorEvent, FinalEvent, Request
from edith.core.session import ChatSession
from edith.observability.costs import CostLedger, CostRecord, Usage
from edith.observability.logging import configure_logging
from tests.fakes import FakeFactory, text_turn


async def test_records_cost_and_logs_each_reply(cfg: EdithConfig) -> None:
    log_path = configure_logging(cfg.log_dir)
    ledger = CostLedger(cfg.data_dir / "costs.jsonl")
    factory = FakeFactory(text_turn("hi", 0.01, input_tokens=500, output_tokens=30))
    async with ClaudeBrain(cfg, client_factory=factory) as brain:
        events = [e async for e in ChatSession(cfg, brain, ledger).ask(Request(text="hello"))]

    assert isinstance(events[-1], FinalEvent)
    rows = [json.loads(x) for x in ledger.path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["cost_usd"] == pytest.approx(0.01)
    assert rows[0]["usage"]["output_tokens"] == 30
    assert rows[0]["trace_id"]

    logs = [json.loads(x) for x in log_path.read_text(encoding="utf-8").splitlines()]
    finished = [r for r in logs if r["event"] == "request_finished"]
    assert finished[-1]["trace_id"] == rows[0]["trace_id"]
    assert finished[-1]["output_tokens"] == 30
    assert finished[-1]["cost_usd"] == pytest.approx(0.01)


async def test_refuses_when_daily_budget_spent(cfg: EdithConfig) -> None:
    ledger = CostLedger(cfg.data_dir / "costs.jsonl")
    ledger.record(CostRecord(cost_usd=1.50, model="m", usage=Usage()))  # cap is $1.00
    factory = FakeFactory()  # no turns scripted: the brain must never be called
    async with ClaudeBrain(cfg, client_factory=factory) as brain:
        events = [e async for e in ChatSession(cfg, brain, ledger).ask(Request(text="hi"))]
    assert len(events) == 1 and isinstance(events[0], ErrorEvent)
    assert "Daily budget" in events[0].message
    assert factory.client is not None and factory.client.queries == []
