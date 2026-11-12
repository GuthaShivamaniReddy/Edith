"""End-to-end safety tests: real policy file, real gate, real tools (fake OS adapter),
scripted model. These are the Phase 1 exit checks for the safety layer."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from edith.config import BudgetsConfig, EdithConfig, SafetyConfig
from edith.core.events import (
    ConfirmRequestEvent,
    ErrorEvent,
    Event,
    FinalEvent,
    NotificationEvent,
    QuestionEvent,
    Request,
    ToolEndEvent,
    ToolStartEvent,
)
from edith.core.orchestrator import Orchestrator
from edith.safety.confirm import GrantScope
from edith.safety.policy import Permissions
from edith.tools.system.fake import FakeAdapter
from tests.fakes import FakeFactory, Hang, ToolCall, events_of, fake_orchestrator, text_turn

Answer = Callable[[ConfirmRequestEvent], tuple[bool, GrantScope]]


def yes(_: ConfirmRequestEvent) -> tuple[bool, GrantScope]:
    return True, "once"


def yes_session(_: ConfirmRequestEvent) -> tuple[bool, GrantScope]:
    return True, "session"


def no(_: ConfirmRequestEvent) -> tuple[bool, GrantScope]:
    return False, "once"


async def ask(
    orch: Orchestrator, text: str, answer: Answer | None = None, session: str = "s1"
) -> list[Event]:
    events: list[Event] = []
    async for ev in orch.handle(Request(text=text, source="eval", session_id=session)):
        events.append(ev)
        if isinstance(ev, ConfirmRequestEvent) and answer is not None:
            approved, scope = answer(ev)
            orch.confirm(ev.id, approved, scope)
    return events


def ran(factory: FakeFactory) -> list[str]:
    assert factory.client is not None
    return [t.sdk_name for t in factory.client.tool_log if t.verdict == "ran"]


def audit(orch: Orchestrator) -> list[dict[str, Any]]:
    return orch.audit.entries()


def read_call(path: Path, tid: str = "t1") -> ToolCall:
    return ToolCall("mcp__files__read", {"path": str(path)}, tid)


async def test_tier0_runs_without_asking(tool_cfg: EdithConfig, permissions: Permissions) -> None:
    adapter = FakeAdapter()
    orch, factory = fake_orchestrator(
        tool_cfg,
        permissions,
        [ToolCall("mcp__system__get_volume", {}), *text_turn("50%", 0.001)],
        adapter=adapter,
    )
    events = await ask(orch, "how loud is it")
    assert not events_of(events, ConfirmRequestEvent)
    assert ran(factory) == ["mcp__system__get_volume"]
    (end,) = events_of(events, ToolEndEvent)
    assert end.ok and "Volume is 50%" in end.summary
    assert isinstance(events[-1], FinalEvent)


async def test_every_tier2_action_asks(tool_cfg: EdithConfig, permissions: Permissions) -> None:
    """Exit criterion: no tier-2 tool in the policy can run without a confirmation."""
    tier2 = [n for n, t in permissions.tiers.items() if t == 2 and "*" not in n]
    orch, _ = fake_orchestrator(tool_cfg, permissions)
    gate = orch.session("s").gate
    from edith.safety.policy import RequestSafety

    for name in tier2:
        args = {"path": str(tool_cfg.files.resolved_allowed()[0] / "x.txt")}
        d = gate.decide(name, args, RequestSafety())
        assert d.verdict == "ask", name
    assert permissions.tier_for("brand.new_tool") == 2
    assert gate.decide("brand.new_tool", {}, RequestSafety()).verdict == "ask"


async def test_file_read_needs_access_grant_each_time(
    tool_cfg: EdithConfig, permissions: Permissions, sandbox: Path
) -> None:
    todo = sandbox / "docs" / "todo.txt"
    orch, factory = fake_orchestrator(
        tool_cfg,
        permissions,
        [read_call(todo), *text_turn("milk", 0.001, msg_id="a")],
        [read_call(todo, "t2"), *text_turn("milk", 0.002, msg_id="b")],
    )
    first = await ask(orch, "read my todo", yes)
    (confirm,) = events_of(first, ConfirmRequestEvent)
    assert confirm.kind == "access"
    assert str(todo) in confirm.action
    assert confirm.allow_session is True
    assert events_of(first, ToolEndEvent)[0].ok
    assert factory.client is not None
    assert "buy milk" in str(factory.client.tool_log[0].response)

    second = await ask(orch, "read it again", yes)  # 'once' does not carry over
    assert len(events_of(second, ConfirmRequestEvent)) == 1
    assert ran(factory) == ["mcp__files__read", "mcp__files__read"]


async def test_session_grant_covers_folder_until_revoked(
    tool_cfg: EdithConfig, permissions: Permissions, sandbox: Path
) -> None:
    todo = sandbox / "docs" / "todo.txt"
    turns = [
        [read_call(todo, f"t{i}"), *text_turn("ok", 0.001 * i, msg_id=f"m{i}")] for i in range(1, 5)
    ]
    orch, _ = fake_orchestrator(tool_cfg, permissions, *turns)

    assert events_of(await ask(orch, "read", yes_session), ConfirmRequestEvent)
    assert orch.grants("s1") == [f"files: {sandbox / 'docs'}"]
    assert not events_of(await ask(orch, "read"), ConfirmRequestEvent)  # granted
    # another session starts with nothing
    other = await ask(orch, "read", no, session="s2")
    assert events_of(other, ConfirmRequestEvent)
    orch.revoke_grants("s1")
    assert events_of(await ask(orch, "read", no), ConfirmRequestEvent)
    assert any(e["kind"] == "grant" for e in audit(orch))
    assert any(e["kind"] == "revoke" for e in audit(orch))


@pytest.mark.parametrize("where", ["blocked", "outside"])
async def test_blocked_and_out_of_scope_paths_are_refused(
    tool_cfg: EdithConfig, permissions: Permissions, sandbox: Path, tmp_path: Path, where: str
) -> None:
    target = sandbox / "secret" / "salary.txt" if where == "blocked" else tmp_path / "x.txt"
    orch, factory = fake_orchestrator(
        tool_cfg, permissions, [read_call(target), *text_turn("can't", 0.001)]
    )
    events = await ask(orch, "read it", yes)
    assert not events_of(events, ConfirmRequestEvent)  # never even offered
    assert ran(factory) == []
    (end,) = events_of(events, ToolEndEvent)
    assert not end.ok and "Refused" in end.summary


async def test_dotdot_cannot_escape_scope(
    tool_cfg: EdithConfig, permissions: Permissions, sandbox: Path
) -> None:
    sneaky = str(sandbox / "docs" / ".." / "secret" / "salary.txt")
    orch, factory = fake_orchestrator(
        tool_cfg,
        permissions,
        [ToolCall("mcp__files__read", {"path": sneaky}), *text_turn("no", 0.001)],
    )
    await ask(orch, "read", yes)
    assert ran(factory) == []


async def test_declined_action_is_not_done(
    tool_cfg: EdithConfig, permissions: Permissions, sandbox: Path
) -> None:
    todo = sandbox / "docs" / "todo.txt"
    orch, factory = fake_orchestrator(
        tool_cfg,
        permissions,
        [ToolCall("mcp__files__trash", {"path": str(todo)}), *text_turn("ok, kept it", 0.001)],
    )
    events = await ask(orch, "delete my todo", no)
    (confirm,) = events_of(events, ConfirmRequestEvent)
    assert confirm.allow_session is False  # trash is once-only
    assert todo.exists()
    assert ran(factory) == []
    assert factory.client is not None
    assert "declined" in str(factory.client.tool_log[0].response)
    conf = [e for e in audit(orch) if e["kind"] == "confirmation"]
    assert conf and conf[0]["approved"] is False and conf[0]["timed_out"] is False


async def test_approved_trash_moves_file(
    tool_cfg: EdithConfig, permissions: Permissions, sandbox: Path
) -> None:
    todo = sandbox / "docs" / "todo.txt"
    orch, factory = fake_orchestrator(
        tool_cfg,
        permissions,
        [ToolCall("mcp__files__trash", {"path": str(todo)}), *text_turn("done", 0.001)],
    )
    events = await ask(orch, "delete my todo", yes)
    assert events_of(events, ConfirmRequestEvent)[0].tier == 2
    assert not todo.exists()
    assert ran(factory) == ["mcp__files__trash"]


async def test_silence_is_a_no(tool_cfg: EdithConfig, permissions: Permissions) -> None:
    cfg = tool_cfg.model_copy(update={"safety": SafetyConfig(confirm_timeout_s=0.2)})
    orch, factory = fake_orchestrator(
        cfg, permissions, [ToolCall("mcp__clipboard__read", {}), *text_turn("no answer", 0.001)]
    )
    events = await ask(orch, "what's on my clipboard")  # nobody answers
    assert events_of(events, ConfirmRequestEvent)
    assert ran(factory) == []
    conf = [e for e in audit(orch) if e["kind"] == "confirmation"]
    assert conf[0]["timed_out"] is True


async def test_tier3_is_always_refused(tool_cfg: EdithConfig, permissions: Permissions) -> None:
    orch, factory = fake_orchestrator(
        tool_cfg,
        permissions,
        [ToolCall("mcp__payments__charge", {"amount": 5}), *text_turn("can't", 0.001)],
    )
    events = await ask(orch, "pay", yes)
    assert not events_of(events, ConfirmRequestEvent)
    assert ran(factory) == []
    assert "tier 3" in events_of(events, ToolEndEvent)[0].summary


async def test_outbound_after_local_read_asks(
    tool_cfg: EdithConfig, permissions: Permissions, sandbox: Path
) -> None:
    """Privacy: once local data is in the request, web tools must show what is sent."""
    todo = sandbox / "docs" / "todo.txt"
    orch, _factory = fake_orchestrator(
        tool_cfg,
        permissions,
        [
            read_call(todo),
            ToolCall("WebSearch", {"query": "buy milk call mom"}, "t2"),
            *text_turn("ok", 0.001),
        ],
    )
    events = await ask(orch, "read my todo and search for it", yes)
    kinds = [c.kind for c in events_of(events, ConfirmRequestEvent)]
    assert kinds == ["access", "outbound"]
    assert "buy milk call mom" in events_of(events, ConfirmRequestEvent)[1].action


async def test_web_search_alone_does_not_ask(
    tool_cfg: EdithConfig, permissions: Permissions
) -> None:
    orch, factory = fake_orchestrator(
        tool_cfg,
        permissions,
        [ToolCall("WebSearch", {"query": "weather"}), *text_turn("sunny", 0.001)],
    )
    events = await ask(orch, "weather?")
    assert not events_of(events, ConfirmRequestEvent)
    assert ran(factory) == ["WebSearch"]


async def test_untrusted_content_raises_tier1_to_ask(
    tool_cfg: EdithConfig, permissions: Permissions
) -> None:
    """Prompt-injection defence: after reading a web page, reversible actions ask too."""
    adapter = FakeAdapter()
    orch, _factory = fake_orchestrator(
        tool_cfg,
        permissions,
        [
            ToolCall("WebFetch", {"url": "https://example.com"}),
            ToolCall("mcp__web__open_url", {"url": "https://evil.example"}, "t2"),
            *text_turn("ok", 0.001),
        ],
        adapter=adapter,
    )
    events = await ask(orch, "summarize example.com", no)
    (confirm,) = events_of(events, ConfirmRequestEvent)
    assert confirm.kind in {"untrusted", "outbound"}
    assert adapter.opened_urls == []


async def test_clarifying_question_round_trip(
    tool_cfg: EdithConfig, permissions: Permissions
) -> None:
    questions = [{"question": "Which file?", "options": [{"label": "a.txt"}, {"label": "b.txt"}]}]
    orch, factory = fake_orchestrator(
        tool_cfg,
        permissions,
        [ToolCall("AskUserQuestion", {"questions": questions}), *text_turn("a it is", 0.001)],
    )
    events: list[Event] = []
    async for ev in orch.handle(Request(text="open the file", session_id="s1")):
        events.append(ev)
        if isinstance(ev, QuestionEvent):
            orch.answer(ev.id, {"Which file?": "a.txt"})
    assert factory.client is not None
    (call,) = factory.client.tool_log
    assert call.verdict == "ran"
    assert isinstance(events[-1], FinalEvent)


async def test_stop_cancels_within_one_second(
    tool_cfg: EdithConfig, permissions: Permissions
) -> None:
    orch, factory = fake_orchestrator(tool_cfg, permissions, [Hang()])
    events: list[Event] = []

    async def consume() -> None:
        async for ev in orch.handle(Request(text="write me an essay", session_id="s1")):
            events.append(ev)

    task = asyncio.create_task(consume())
    for _ in range(100):  # the brain starts lazily on the first request
        await asyncio.sleep(0)
        if factory.client is not None:
            break
    assert factory.client is not None
    await asyncio.wait_for(factory.client.queried.wait(), 1)
    started = time.perf_counter()
    stop_events = await ask(orch, "stop")  # the fast-path kill switch
    await asyncio.wait_for(task, 1.0)
    assert time.perf_counter() - started < 1.0
    assert isinstance(stop_events[-1], FinalEvent) and stop_events[-1].text == "Stopped."
    assert isinstance(events[-1], ErrorEvent) and events[-1].message == "Stopped."
    assert factory.client.interrupted
    await orch.close()


async def test_stop_releases_pending_confirmation(
    tool_cfg: EdithConfig, permissions: Permissions, sandbox: Path
) -> None:
    orch, factory = fake_orchestrator(
        tool_cfg, permissions, [read_call(sandbox / "docs" / "todo.txt"), *text_turn("x", 0.001)]
    )
    seen = asyncio.Event()

    async def consume() -> list[Event]:
        out: list[Event] = []
        async for ev in orch.handle(Request(text="read", session_id="s1")):
            out.append(ev)
            if isinstance(ev, ConfirmRequestEvent):
                seen.set()
        return out

    task = asyncio.create_task(consume())
    await asyncio.wait_for(seen.wait(), 1)
    assert await orch.stop("s1")
    events = await asyncio.wait_for(task, 1)
    assert ran(factory) == []
    assert any(isinstance(e, ErrorEvent) for e in events)


async def test_fast_path_runs_without_model(
    tool_cfg: EdithConfig, permissions: Permissions
) -> None:
    adapter = FakeAdapter()
    orch, factory = fake_orchestrator(tool_cfg, permissions, adapter=adapter)
    events = await ask(orch, "set volume to 30")
    assert adapter.volume == 30
    assert factory.client is None  # no brain, no cost
    final = events[-1]
    assert isinstance(final, FinalEvent) and final.route == "fast_path" and final.cost_usd == 0
    assert events_of(events, ToolStartEvent)
    assert [e["via"] for e in audit(orch) if e["kind"] == "decision"] == ["fast_path"]

    await ask(orch, "turn the volume down")
    assert adapter.volume == 20


async def test_fast_path_hands_asking_tools_to_model(
    tool_cfg: EdithConfig, permissions: Permissions
) -> None:
    """A fast-path command the policy would ask about goes to the model instead."""
    restricted = Permissions.from_dict(
        {
            "tools": {**permissions.tiers, "system.set_volume": 2},
            "access": {},
        }
    )
    adapter = FakeAdapter()
    orch, factory = fake_orchestrator(
        tool_cfg, restricted, [*text_turn("I'll ask first.", 0.001)], adapter=adapter
    )
    await ask(orch, "set volume to 30")
    assert adapter.volume == 50
    assert factory.client is not None


async def test_timer_notifies_subscribers(tool_cfg: EdithConfig, permissions: Permissions) -> None:
    orch, _ = fake_orchestrator(tool_cfg, permissions)
    q = orch.subscribe()
    events = await ask(orch, "set a timer for 1 second called tea")
    assert isinstance(events[-1], FinalEvent) and "tea" in events[-1].text
    note = await asyncio.wait_for(q.get(), 3)
    assert isinstance(note, NotificationEvent) and "tea" in note.text
    await orch.close()


async def test_daily_budget_blocks_model(tool_cfg: EdithConfig, permissions: Permissions) -> None:
    cfg = tool_cfg.model_copy(update={"budgets": BudgetsConfig(per_day_usd=0.001)})
    orch, _factory = fake_orchestrator(cfg, permissions, text_turn("hi", 0.002, msg_id="a"))
    await ask(orch, "explain quantum computing to me in detail please")
    events = await ask(orch, "and again")
    assert isinstance(events[-1], ErrorEvent) and "Daily budget" in events[-1].message


async def test_per_request_cap_stops_runaway_reply(
    tool_cfg: EdithConfig, permissions: Permissions
) -> None:
    cfg = tool_cfg.model_copy(update={"budgets": BudgetsConfig(per_request_usd=0.01)})
    orch, factory = fake_orchestrator(
        cfg, permissions, text_turn("long", 0.5, input_tokens=10, output_tokens=200_000)
    )
    events = await ask(orch, "write a novel")
    assert isinstance(events[-1], ErrorEvent)
    assert "per-request budget" in events[-1].message
    assert factory.client is not None and factory.client.interrupted
    assert orch.ledger.totals().today_usd == pytest.approx(0.5)  # still recorded


async def test_router_uses_fast_model_for_chat(
    tool_cfg: EdithConfig, permissions: Permissions
) -> None:
    orch, factory = fake_orchestrator(tool_cfg, permissions, text_turn("Hello.", 0.0001))
    events = await ask(orch, "hi there")
    assert factory.client is not None and factory.client.models == ["claude-haiku-5-5"]
    assert isinstance(events[-1], FinalEvent) and events[-1].route == "quick_answer"


async def test_audit_redacts_secrets(tool_cfg: EdithConfig, permissions: Permissions) -> None:
    orch, _ = fake_orchestrator(
        tool_cfg,
        permissions,
        [
            ToolCall("mcp__clipboard__write", {"text": "x", "api_key": "sk-ant-zzzz"}),
            *text_turn("ok", 0.001),
        ],
    )
    await ask(orch, "copy x", yes)
    raw = orch.audit.path.read_text(encoding="utf-8")
    assert "sk-ant-zzzz" not in raw
    assert all(json.loads(line)["trace_id"] for line in raw.splitlines())
