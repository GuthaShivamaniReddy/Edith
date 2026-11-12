"""Orchestrator: runs every request, whichever face it came from (PLAN.md section 6).

For each request: start a trace -> kill switch / fast path -> daily budget ->
route to a model -> run the session's brain with a per-request cost cap ->
record cost. Sessions are independent conversations (one brain and one set of
access grants each). Timers and other out-of-band events go to subscribers.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import structlog

from edith.config import EdithConfig, config_dir
from edith.core import fastpath
from edith.core.brain import Brain
from edith.core.brain.claude_brain import ClaudeBrain
from edith.core.events import (
    ErrorEvent,
    Event,
    FinalEvent,
    NotificationEvent,
    Request,
    Source,
)
from edith.core.prompts import user_tz
from edith.core.router import route
from edith.observability.costs import CostLedger, CostRecord, Usage
from edith.observability.logging import get_logger, new_trace
from edith.safety.audit import AuditLog
from edith.safety.confirm import ConfirmationBroker, GrantScope
from edith.safety.gate import SafetyGate
from edith.safety.policy import Permissions, RequestSafety
from edith.safety.scope import FileScope
from edith.tools.base import ToolContext
from edith.tools.registry import ToolRegistry, load_external_servers
from edith.tools.system import SystemAdapter, get_adapter
from edith.tools.timers import TimerService

log = get_logger(__name__)

MAX_SESSIONS = 8
FAST_PATH = "fast-path"

BrainFactory = Callable[[str, Source, SafetyGate], Brain]


@dataclass
class Session:
    id: str
    source: Source
    gate: SafetyGate
    brain: Brain | None = None
    busy: bool = False
    last_used: float = 0.0


class Orchestrator:
    def __init__(
        self,
        cfg: EdithConfig,
        *,
        adapter: SystemAdapter | None = None,
        permissions: Permissions | None = None,
        brain_factory: BrainFactory | None = None,
    ) -> None:
        self.cfg = cfg
        self.permissions = permissions or Permissions.load(config_dir() / "permissions.yaml")
        self.scope = FileScope.from_config(cfg)
        self.audit = AuditLog(cfg.data_dir / "audit.jsonl")
        self.broker = ConfirmationBroker(cfg.safety.confirm_timeout_s)
        self.ledger = CostLedger(cfg.data_dir / "costs.jsonl")
        self.timers = TimerService(self.publish)
        self.adapter = adapter or get_adapter()
        self.ctx = ToolContext(cfg, self.adapter, self.scope, self.timers, self.audit)
        self.registry = ToolRegistry(
            self.ctx,
            self.permissions,
            external=load_external_servers(config_dir() / "mcp_servers.yaml"),
        )
        self._brain_factory = brain_factory or self._claude_brain
        self._sessions: dict[str, Session] = {}
        self._subscribers: set[asyncio.Queue[Event]] = set()

    # ------------------------------------------------------------------ sessions

    def _claude_brain(self, session_id: str, source: Source, gate: SafetyGate) -> Brain:
        return ClaudeBrain(
            self.cfg,
            gate,
            mcp_servers=self.registry.mcp_servers(),  # fresh server instances per session
            source=source,
            cost_sink=self._record_cost,
        )

    def session(self, session_id: str, source: Source = "cli") -> Session:
        s = self._sessions.get(session_id)
        if s is None:
            gate = SafetyGate(
                session_id, self.permissions, self.scope, self.audit, self.broker, self.registry
            )
            s = Session(session_id, source, gate)
            self._sessions[session_id] = s
        s.last_used = time.monotonic()
        return s

    async def _brain_for(self, s: Session) -> Brain:
        if s.brain is None:
            await self._evict_idle()
            brain = self._brain_factory(s.id, s.source, s.gate)
            await brain.start()
            s.brain = brain
        return s.brain

    async def _evict_idle(self) -> None:
        live = [s for s in self._sessions.values() if s.brain is not None]
        if len(live) < MAX_SESSIONS:
            return
        idle = sorted((s for s in live if not s.busy), key=lambda s: s.last_used)
        if idle:
            await self.end_session(idle[0].id)

    async def end_session(self, session_id: str) -> None:
        """Forget a conversation and every access grant it held."""
        s = self._sessions.pop(session_id, None)
        if s is None:
            return
        self.broker.cancel_session(session_id)
        s.gate.grants.revoke_all()
        if s.brain is not None:
            await s.brain.close()

    def revoke_grants(self, session_id: str) -> list[str]:
        s = self._sessions.get(session_id)
        if s is None:
            return []
        revoked = s.gate.grants.describe()
        s.gate.grants.revoke_all()
        self.audit.write("revoke", session_id=session_id, grants=revoked)
        return revoked

    def grants(self, session_id: str) -> list[str]:
        s = self._sessions.get(session_id)
        return s.gate.grants.describe() if s else []

    # ------------------------------------------------------------------ requests

    async def handle(self, req: Request) -> AsyncIterator[Event]:
        new_trace(source=req.source, session_id=req.session_id)
        started = time.perf_counter()
        log.info("request_received", chars=len(req.text))
        s = self.session(req.session_id, req.source)

        cmd = fastpath.match(req.text)
        if cmd is not None and cmd.kind == "stop":
            stopped = await self.stop(req.session_id)
            yield FinalEvent(
                "Stopped." if stopped else "Nothing to stop.", FAST_PATH, 0.0, route="fast_path"
            )
            return
        if cmd is not None and cmd.kind == "reply":
            yield FinalEvent(cmd.reply, FAST_PATH, 0.0, route="fast_path")
            return
        if cmd is not None and cmd.kind == "tool":
            events = await self._fast_tool(s, cmd.tool, dict(cmd.args))
            if events is not None:
                for ev in events:
                    yield ev
                self._log_finished(events[-1], "fast_path", started)
                return

        left = self.cfg.budgets.per_day_usd - self.ledger.totals(tz=user_tz(self.cfg)).today_usd
        if left <= 0:
            log.warning("daily_budget_reached")
            yield ErrorEvent(
                f"Daily budget of ${self.cfg.budgets.per_day_usd:.2f} reached. Raise "
                "budgets.per_day_usd in config/edith.yaml or try again tomorrow."
            )
            return

        r = route(req, self.cfg)
        log.info("routed", route=r.kind, model=r.model, reason=r.reason)
        s.busy = True
        try:
            brain = await self._brain_for(s)
            cap = min(self.cfg.budgets.per_request_usd, left)
            async for ev in brain.run(req, model=r.model, max_cost_usd=cap):
                if isinstance(ev, FinalEvent):
                    ev = dataclasses.replace(ev, route=r.kind)
                if isinstance(ev, FinalEvent | ErrorEvent):
                    self._log_finished(ev, r.kind, started)
                yield ev
        finally:
            s.busy = False

    async def _fast_tool(
        self, s: Session, name: str, args: dict[str, object]
    ) -> list[Event] | None:
        """Run a fixed command without the model, through the same gate. None = needs the model."""
        if name == "system.set_volume" and "delta" in args:
            current = await asyncio.to_thread(self.adapter.get_volume)
            args = {"level": max(0, min(100, current + int(str(args["delta"]))))}
        safety = RequestSafety()
        if s.gate.decide(name, args, safety).verdict != "allow":
            return None  # anything that needs a confirmation goes through the model
        events: list[Event] = []

        async def collect(ev: Event) -> None:
            events.append(ev)

        decision = await s.gate.pre_tool(name, args, "", safety, collect, via="fast_path")
        if decision.verdict != "allow":
            return None
        ok, text = await self.registry.call(name, args)
        await s.gate.post_tool(name, "", ok, text, collect)
        events.append(
            FinalEvent(
                text if ok else f"I couldn't do that: {text}", FAST_PATH, 0.0, route="fast_path"
            )
        )
        return events

    def _record_cost(self, cost: float, usage: Usage, model: str) -> None:
        ctx = structlog.contextvars.get_contextvars()
        self.ledger.record(
            CostRecord(
                cost_usd=cost,
                model=model,
                usage=usage,
                trace_id=str(ctx.get("trace_id", "")),
                session_id=str(ctx.get("session_id", "")),
                source=str(ctx.get("source", "")),
            )
        )

    @staticmethod
    def _log_finished(ev: Event, route_kind: str, started: float) -> None:
        if not isinstance(ev, FinalEvent | ErrorEvent):
            return
        log.info(
            "request_finished",
            ok=isinstance(ev, FinalEvent),
            route=route_kind,
            model=ev.model if isinstance(ev, FinalEvent) else "",
            cost_usd=round(ev.cost_usd, 6),
            input_tokens=ev.usage.total_input_tokens,
            output_tokens=ev.usage.output_tokens,
            cache_read_tokens=ev.usage.cache_read_input_tokens,
            latency_ms=round((time.perf_counter() - started) * 1000),
            error=ev.message if isinstance(ev, ErrorEvent) else None,
        )

    # ------------------------------------------------------------------ control

    async def stop(self, session_id: str | None = None) -> bool:
        """Kill switch: stop the reply in progress (one session, or all). True if any ran."""
        targets = [s for s in self._sessions.values() if session_id is None or s.id == session_id]
        stopped = False
        for s in targets:
            self.broker.cancel_session(s.id)
            if s.busy and s.brain is not None:
                await s.brain.interrupt()
                stopped = True
        log.info("stop", session_id=session_id, stopped=stopped)
        return stopped

    def confirm(self, confirm_id: str, approved: bool, scope: GrantScope = "once") -> bool:
        return self.broker.resolve(confirm_id, approved, scope)

    def answer(self, question_id: str, answers: dict[str, str] | None) -> bool:
        return self.broker.answer(question_id, answers)

    # ------------------------------------------------------------------ notifications

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)

    def publish(self, event: NotificationEvent) -> None:
        log.info("notification", title=event.title)
        for q in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

    async def close(self) -> None:
        self.timers.close()
        self.broker.cancel_session(None)
        for sid in list(self._sessions):
            await self.end_session(sid)
