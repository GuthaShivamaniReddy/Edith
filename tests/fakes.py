"""Scripted stand-in for ClaudeSDKClient: no subprocess, no network, no cost."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolPermissionContext,
)

from edith.config import EdithConfig
from edith.core.brain import Brain
from edith.core.brain.claude_brain import ClaudeBrain, SDKClient
from edith.core.events import Source
from edith.core.orchestrator import Orchestrator
from edith.safety.gate import SafetyGate
from edith.safety.policy import Permissions, policy_name
from edith.tools.system.fake import FakeAdapter


def text_turn(
    text: str,
    session_total_usd: float,
    *,
    msg_id: str = "msg_1",
    input_tokens: int = 100,
    output_tokens: int = 20,
) -> list[Message]:
    """The messages the SDK emits for one plain-text reply with partial messages on."""
    half = len(text) // 2
    return [
        StreamEvent(
            uuid="u1",
            session_id="s1",
            event={
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "usage": {"input_tokens": input_tokens, "output_tokens": 1},
                },
            },
        ),
        StreamEvent(
            uuid="u2",
            session_id="s1",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": text[:half]},
            },
        ),
        StreamEvent(
            uuid="u3",
            session_id="s1",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": text[half:]},
            },
        ),
        # Snapshot arrives mid-stream with a partial output count.
        AssistantMessage(
            content=[TextBlock(text=text)],
            model="claude-sonnet-5-5",
            usage={"input_tokens": input_tokens, "output_tokens": 1},
            message_id=msg_id,
        ),
        StreamEvent(
            uuid="u4",
            session_id="s1",
            event={"type": "message_delta", "usage": {"output_tokens": output_tokens}},
        ),
        ResultMessage(
            subtype="success",
            duration_ms=900,
            duration_api_ms=800,
            is_error=False,
            num_turns=1,
            session_id="s1",
            total_cost_usd=session_total_usd,
            result=text,
        ),
    ]


@dataclass(frozen=True)
class ToolCall:
    """A scripted tool call. The fake drives it through the real hooks and can_use_tool,
    the way Claude Code does: PreToolUse -> (ask: can_use_tool) -> run -> PostToolUse."""

    sdk_name: str  # e.g. "mcp__system__set_volume" or "WebSearch"
    tool_input: dict[str, Any]
    tool_use_id: str = "toolu_1"


@dataclass(frozen=True)
class Hang:
    """The model 'thinks' until interrupted (for kill-switch tests)."""


@dataclass
class ToolLog:
    sdk_name: str
    verdict: str  # allow | deny | declined | ran
    response: Any = None


ToolRunner = Callable[[str, dict[str, Any]], Awaitable[Any]]
Step = Message | ToolCall | Hang


async def _no_runner(sdk_name: str, tool_input: dict[str, Any]) -> Any:
    return {"content": [{"type": "text", "text": f"ran {sdk_name}"}], "is_error": False}


class FakeClient:
    def __init__(
        self,
        options: ClaudeAgentOptions,
        turns: list[Sequence[Step]],
        tool_runner: ToolRunner = _no_runner,
    ) -> None:
        self.options = options
        self.turns = turns
        self.tool_runner = tool_runner
        self.queries: list[str] = []
        self.models: list[str | None] = []
        self.tool_log: list[ToolLog] = []
        self.connected = False
        self.interrupted = False
        self._interrupt = asyncio.Event()
        self.queried = asyncio.Event()  # set when a request reaches the "model"

    async def connect(self, prompt: str | None = None) -> None:
        self.connected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.queries.append(prompt)
        self._interrupt.clear()
        self.queried.set()

    async def receive_response(self) -> AsyncIterator[Message]:
        for step in self.turns.pop(0):
            if isinstance(step, ToolCall):
                await self._tool(step)
            elif isinstance(step, Hang):
                await self._interrupt.wait()
                yield ResultMessage(
                    subtype="error_during_execution",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=True,
                    num_turns=1,
                    session_id="s1",
                    total_cost_usd=0.0,
                )
                return
            else:
                yield step

    async def _hook(self, event: str, data: dict[str, Any], tool_use_id: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for matcher in (self.options.hooks or {}).get(event, []):  # type: ignore[call-overload]
            for hook in matcher.hooks:
                out = dict(await hook(data, tool_use_id, {"signal": None}))
        return out

    async def _tool(self, call: ToolCall) -> None:
        base = {"tool_name": call.sdk_name, "tool_input": call.tool_input}
        pre = await self._hook("PreToolUse", {**base, "tool_use_id": call.tool_use_id}, "")
        verdict = (pre.get("hookSpecificOutput") or {}).get("permissionDecision", "ask")
        tool_input = call.tool_input
        if verdict == "deny":
            self.tool_log.append(ToolLog(call.sdk_name, "deny"))
            return
        if verdict == "ask":
            assert self.options.can_use_tool is not None
            result = await self.options.can_use_tool(
                call.sdk_name,
                call.tool_input,
                ToolPermissionContext(tool_use_id=call.tool_use_id),
            )
            if isinstance(result, PermissionResultDeny):
                self.tool_log.append(ToolLog(call.sdk_name, "declined", result.message))
                return
            tool_input = result.updated_input or call.tool_input
        response = await self.tool_runner(call.sdk_name, tool_input)
        self.tool_log.append(ToolLog(call.sdk_name, "ran", response))
        await self._hook(
            "PostToolUse",
            {**base, "tool_input": tool_input, "tool_response": response},
            call.tool_use_id,
        )

    async def interrupt(self) -> None:
        self.interrupted = True
        self._interrupt.set()

    async def set_model(self, model: str | None = None) -> None:
        self.models.append(model)

    async def disconnect(self) -> None:
        self.connected = False


class FakeFactory:
    def __init__(
        self,
        *turns: Sequence[Step],
        connect_error: Exception | None = None,
        tool_runner: ToolRunner = _no_runner,
    ) -> None:
        self.turns = list(turns)
        self.connect_error = connect_error
        self.tool_runner = tool_runner
        self.client: FakeClient | None = None

    def __call__(self, options: ClaudeAgentOptions) -> SDKClient:
        self.client = FakeClient(options, self.turns, self.tool_runner)
        if self.connect_error is not None:
            err = self.connect_error

            async def fail(prompt: str | None = None) -> None:
                raise err

            self.client.connect = fail  # type: ignore[method-assign]
        return self.client


def events_of(items: list[Any], kind: type) -> list[Any]:
    return [e for e in items if isinstance(e, kind)]


def tool_result(text: str, ok: bool = True) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": not ok}


def fake_orchestrator(
    cfg: EdithConfig,
    permissions: Permissions,
    *turns: Sequence[Step],
    adapter: FakeAdapter | None = None,
) -> tuple[Orchestrator, FakeFactory]:
    """An Orchestrator whose brains talk to the scripted fake client.

    Scripted tool calls run the real tools (on the fake system adapter) through the
    real safety gate, so tests exercise the same path as production.
    """
    orch: Orchestrator | None = None

    async def runner(sdk_name: str, tool_input: dict[str, Any]) -> Any:
        assert orch is not None
        ok, text = await orch.registry.call(policy_name(sdk_name), tool_input)
        return tool_result(text, ok)

    factory = FakeFactory(*turns, tool_runner=runner)

    def brain_factory(session_id: str, source: Source, gate: SafetyGate) -> Brain:
        assert orch is not None
        return ClaudeBrain(
            cfg, gate, source=source, client_factory=factory, cost_sink=orch._record_cost
        )

    orch = Orchestrator(
        cfg,
        adapter=adapter or FakeAdapter(),
        permissions=permissions,
        brain_factory=brain_factory,
    )
    return orch, factory
