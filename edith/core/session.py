"""A chat session: one brain, plus the bookkeeping around each request.

For every request: start a trace, refuse if the daily budget is spent, stream
the brain's events, then record cost and tokens. Phase 1's orchestrator grows
out of this (memory injection, routing, safety gate, cancel tokens).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from edith.config import EdithConfig
from edith.core.brain import Brain
from edith.core.events import ErrorEvent, Event, FinalEvent, Request
from edith.core.prompts import user_tz
from edith.observability.costs import CostLedger, CostRecord
from edith.observability.logging import get_logger, new_trace

log = get_logger(__name__)


class ChatSession:
    def __init__(self, cfg: EdithConfig, brain: Brain, ledger: CostLedger) -> None:
        self.cfg = cfg
        self.brain = brain
        self.ledger = ledger

    def budget_left_usd(self) -> float:
        spent = self.ledger.totals(tz=user_tz(self.cfg)).today_usd
        return self.cfg.budgets.per_day_usd - spent

    async def ask(self, request: Request) -> AsyncIterator[Event]:
        trace_id = new_trace(source=request.source)
        started = time.perf_counter()
        log.info("request_received", chars=len(request.text))

        left = self.budget_left_usd()
        if left <= 0:
            log.warning("daily_budget_reached", per_day_usd=self.cfg.budgets.per_day_usd)
            yield ErrorEvent(
                f"Daily budget of ${self.cfg.budgets.per_day_usd:.2f} reached. "
                "Raise budgets.per_day_usd in config/edith.yaml or try again tomorrow."
            )
            return

        async for event in self.brain.run(request):
            if isinstance(event, FinalEvent | ErrorEvent):
                model = event.model if isinstance(event, FinalEvent) else ""
                session_id = event.session_id if isinstance(event, FinalEvent) else ""
                if event.cost_usd > 0 or isinstance(event, FinalEvent):
                    self.ledger.record(
                        CostRecord(
                            cost_usd=event.cost_usd,
                            model=model,
                            usage=event.usage,
                            trace_id=trace_id,
                            session_id=session_id,
                            source=request.source,
                        )
                    )
                log.info(
                    "request_finished",
                    ok=isinstance(event, FinalEvent),
                    model=model,
                    cost_usd=round(event.cost_usd, 6),
                    input_tokens=event.usage.total_input_tokens,
                    output_tokens=event.usage.output_tokens,
                    cache_read_tokens=event.usage.cache_read_input_tokens,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    error=event.message if isinstance(event, ErrorEvent) else None,
                )
            yield event
