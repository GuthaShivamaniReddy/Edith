"""ClaudeBrain: the Claude Agent SDK behind the Brain interface.

Tools: EDITH's in-process MCP tools, plus only three Claude Code built-ins:
WebSearch, WebFetch and AskUserQuestion. File, shell and edit built-ins stay off.
User/project settings and outside MCP config are never loaded.

Every tool call passes the SafetyGate through the PreToolUse hook and the
can_use_tool callback. SDK messages and gate events (confirmations, tool
start/end) are merged into one event stream per request.

Credentials: a Claude Console API key from the OS keychain if one is stored,
otherwise the Claude Code login on this machine. Never a file.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLINotFoundError,
    ConversationResetMessage,
    HookJSONOutput,
    HookMatcher,
    Message,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolPermissionContext,
)

from edith.config import EdithConfig
from edith.core.brain.base import Brain, BrainError
from edith.core.events import (
    ErrorEvent,
    Event,
    FinalEvent,
    QuestionEvent,
    Request,
    Source,
    TokenEvent,
)
from edith.core.prompts import render_system_prompt
from edith.observability.costs import Usage, estimate_cost
from edith.observability.logging import get_logger
from edith.safety.gate import SafetyGate
from edith.safety.policy import RequestSafety, policy_name
from edith.safety.secrets import SecretError, get_secret

log = get_logger(__name__)

BUILTIN_TOOLS = ["WebSearch", "WebFetch", "AskUserQuestion"]
TELEMETRY_OFF = {
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}
DRAIN_TIMEOUT_S = 15.0

_ERROR_TEXT: dict[str, str] = {
    "authentication_failed": (
        "Not signed in. Run `claude` once and log in, or store an API key with "
        "`uv run edith secrets set anthropic_api_key`."
    ),
    "billing_error": "Billing problem with the Claude account (credits or plan limits).",
    "rate_limit": "Rate limited by the Claude API. Try again in a moment.",
    "invalid_request": "The request was rejected as invalid.",
    "server_error": "The Claude API had a server error. Try again in a moment.",
    "unknown": "Unknown error from the Claude API.",
}
_RETRYABLE = {"rate_limit", "server_error"}


class SDKClient(Protocol):
    """The subset of ClaudeSDKClient that ClaudeBrain uses (lets tests inject a fake)."""

    async def connect(self, prompt: str | None = None) -> None: ...
    async def query(self, prompt: str, session_id: str = "default") -> None: ...
    def receive_response(self) -> AsyncIterator[Message]: ...
    async def interrupt(self) -> None: ...
    async def set_model(self, model: str | None = None) -> None: ...
    async def disconnect(self) -> None: ...


ClientFactory = Callable[[ClaudeAgentOptions], SDKClient]
CostSink = Callable[[float, Usage, str], None]


def _default_factory(options: ClaudeAgentOptions) -> SDKClient:
    return ClaudeSDKClient(options=options)


def credential_env() -> tuple[dict[str, str], str]:
    """Env vars for the Claude Code process, plus a label saying where credentials come from."""
    try:
        key = get_secret("anthropic_api_key")
    except SecretError as e:
        log.warning("keychain_unavailable", error=str(e))
        key = None
    if key:
        return {"ANTHROPIC_API_KEY": key}, "API key from OS keychain"
    return {}, "Claude Code login"


@dataclass
class RunState:
    request: Request
    model: str
    max_cost_usd: float | None
    queue: asyncio.Queue[Event | None] = field(default_factory=asyncio.Queue)
    safety: RequestSafety = field(default_factory=RequestSafety)
    usage_by_msg: dict[str, Usage] = field(default_factory=dict)
    stream_msg_id: str = ""
    over_budget: bool = False
    stopped: bool = False
    closed: bool = False  # the consumer has been told the request is over

    async def emit(self, event: Event) -> None:
        if not self.closed:
            await self.queue.put(event)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.queue.put_nowait(None)

    def note_usage(self, msg_id: str, raw: dict[str, Any] | None) -> None:
        if msg_id and raw:
            prev = self.usage_by_msg.get(msg_id, Usage())
            self.usage_by_msg[msg_id] = prev.max(Usage.from_api(raw))

    @property
    def usage(self) -> Usage:
        return sum(self.usage_by_msg.values(), Usage())


class ClaudeBrain(Brain):
    def __init__(
        self,
        cfg: EdithConfig,
        gate: SafetyGate,
        mcp_servers: dict[str, Any] | None = None,
        source: Source = "cli",
        model: str | None = None,
        client_factory: ClientFactory = _default_factory,
        cost_sink: CostSink | None = None,
    ) -> None:
        self.cfg = cfg
        self.gate = gate
        self.mcp_servers = mcp_servers or {}
        self.source: Source = source
        self.model = model or cfg.models.main
        self._current_model = self.model
        self._factory = client_factory
        self._cost_sink = cost_sink
        self._client: SDKClient | None = None
        self._session_total_usd = 0.0  # SDK reports a running session total
        self._state: RunState | None = None
        self._lock = asyncio.Lock()
        self._reader: asyncio.Task[None] | None = None
        self.credential_source = ""

    # ------------------------------------------------------------------ setup

    def build_options(self) -> ClaudeAgentOptions:
        env, self.credential_source = credential_env()
        if self.cfg.privacy.disable_cli_telemetry:
            env = {**env, **TELEMETRY_OFF}
        workspace = self.cfg.data_dir / "workspace"  # empty dir; no file tools exist anyway
        workspace.mkdir(parents=True, exist_ok=True)
        hook_timeout = self.cfg.safety.confirm_timeout_s + 30
        return ClaudeAgentOptions(
            model=self.model,
            system_prompt=render_system_prompt(self.cfg, source=self.source),
            tools=BUILTIN_TOOLS,  # every other Claude Code built-in stays off
            allowed_tools=[],  # nothing pre-approved: the gate decides everything
            mcp_servers=self.mcp_servers,
            strict_mcp_config=True,  # ignore MCP servers configured outside EDITH
            setting_sources=[],  # ignore ~/.claude and project settings, CLAUDE.md
            permission_mode="default",
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher=None, hooks=[self._pre_tool_use], timeout=hook_timeout)
                ],
                "PostToolUse": [HookMatcher(matcher=None, hooks=[self._post_tool_use])],
                "PostToolUseFailure": [HookMatcher(matcher=None, hooks=[self._post_tool_fail])],
            },
            can_use_tool=self._can_use_tool,
            max_turns=self.cfg.agent.max_turns,
            include_partial_messages=True,
            effort=self.cfg.models.effort,
            cwd=str(workspace),
            env=env,
            stderr=lambda line: log.debug("claude_cli_stderr", line=line),
        )

    async def start(self) -> None:
        options = self.build_options()
        self._client = self._factory(options)
        try:
            await self._client.connect()
        except CLINotFoundError as e:
            raise BrainError(
                "Claude Code CLI not found. Install it (https://code.claude.com) and run "
                "`claude` once to sign in."
            ) from e
        except ClaudeSDKError as e:
            raise BrainError(f"Could not start the Claude session: {e}") from e
        log.info("brain_started", model=self.model, credentials=self.credential_source)

    async def close(self) -> None:
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        if self._client is not None:
            try:
                await self._client.disconnect()
            except ClaudeSDKError as e:
                log.warning("brain_disconnect_failed", error=str(e))
            self._client = None

    # ------------------------------------------------------------------ control

    async def interrupt(self) -> None:
        """Stop the reply in progress. The caller's event stream ends at once."""
        state = self._state
        if state is not None and not state.stopped:
            state.stopped = True
            self.gate.broker.cancel_session(self.gate.session_id)
            await state.emit(ErrorEvent("Stopped."))
            state.close()
        if self._client is not None:
            with contextlib.suppress(ClaudeSDKError):
                await self._client.interrupt()

    async def run(
        self,
        request: Request,
        model: str | None = None,
        max_cost_usd: float | None = None,
    ) -> AsyncIterator[Event]:
        if self._client is None:
            raise BrainError("ClaudeBrain.run() called before start()")
        async with self._lock:
            await self._wait_for_drain()
            model = model or self.model
            if model != self._current_model:
                await self._client.set_model(model)
                self._current_model = model
            state = RunState(request=request, model=model, max_cost_usd=max_cost_usd)
            self._state = state
            self._reader = asyncio.create_task(self._read(state, self._client))
            while True:
                event = await state.queue.get()
                if event is None:
                    break
                yield event

    async def _wait_for_drain(self) -> None:
        """A stopped request's reader keeps draining SDK messages; let it finish first."""
        if self._reader is not None and not self._reader.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._reader), DRAIN_TIMEOUT_S)
            except TimeoutError:
                log.warning("previous_request_did_not_drain")

    # ------------------------------------------------------------------ reading

    async def _read(self, state: RunState, client: SDKClient) -> None:
        text_parts: list[str] = []
        model = state.model
        assistant_error: str | None = None
        try:
            await client.query(state.request.text)
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    ev = msg.event
                    etype = ev.get("type")
                    if etype == "message_start":
                        message = ev.get("message") or {}
                        state.stream_msg_id = str(message.get("id") or "")
                        state.note_usage(state.stream_msg_id, message.get("usage"))
                    elif etype == "message_delta":
                        state.note_usage(state.stream_msg_id, ev.get("usage"))
                        await self._check_budget(state)
                    token = _text_delta(ev)
                    if token and msg.parent_tool_use_id is None:
                        await state.emit(TokenEvent(token))

                elif isinstance(msg, AssistantMessage):
                    model = msg.model or model
                    if msg.error:
                        assistant_error = msg.error
                    text_parts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
                    state.note_usage(msg.message_id or msg.uuid or "", msg.usage)
                    await self._check_budget(state)

                elif isinstance(msg, ConversationResetMessage):
                    self._session_total_usd = 0.0

                elif isinstance(msg, ResultMessage):
                    cost = self._reply_cost(msg.total_cost_usd)
                    usage = state.usage if state.usage_by_msg else Usage.from_api(msg.usage)
                    if self._cost_sink is not None:
                        self._cost_sink(cost, usage, model)
                    await state.emit(
                        self._result_event(
                            msg, state, model, cost, usage, text_parts, assistant_error
                        )
                    )
                    return
            await state.emit(ErrorEvent("The reply ended without a result.", retryable=True))
        except ClaudeSDKError as e:
            log.exception("brain_run_failed")
            await state.emit(ErrorEvent(f"Claude session error: {e}", retryable=True))
        finally:
            state.close()
            if self._state is state:
                self._state = None

    def _result_event(
        self,
        msg: ResultMessage,
        state: RunState,
        model: str,
        cost: float,
        usage: Usage,
        text_parts: list[str],
        assistant_error: str | None,
    ) -> Event:
        if state.over_budget:
            return ErrorEvent(
                f"Stopped: this request reached the per-request budget "
                f"(${state.max_cost_usd:.2f}).",
                cost_usd=cost,
                usage=usage,
            )
        if msg.is_error:
            return _error_event(msg, assistant_error, cost, usage)
        return FinalEvent(
            text=msg.result if msg.result is not None else "".join(text_parts),
            model=model,
            cost_usd=cost,
            usage=usage,
            num_turns=msg.num_turns,
            duration_ms=msg.duration_ms,
            session_id=msg.session_id,
        )

    async def _check_budget(self, state: RunState) -> None:
        if state.max_cost_usd is None or state.over_budget or state.stopped:
            return
        if estimate_cost(state.model, state.usage) > state.max_cost_usd:
            state.over_budget = True
            log.warning("request_budget_exceeded", cap=state.max_cost_usd)
            self.gate.broker.cancel_session(self.gate.session_id)
            if self._client is not None:
                with contextlib.suppress(ClaudeSDKError):
                    await self._client.interrupt()

    def _reply_cost(self, session_total: float | None) -> float:
        total = session_total or 0.0
        cost = total - self._session_total_usd if total >= self._session_total_usd else total
        self._session_total_usd = total
        return max(cost, 0.0)

    # ------------------------------------------------------------------ safety hooks

    async def _pre_tool_use(
        self, input_data: Any, tool_use_id: str | None, context: Any
    ) -> HookJSONOutput:
        name = policy_name(str(input_data.get("tool_name", "")))
        args = input_data.get("tool_input") or {}
        tid = tool_use_id or str(input_data.get("tool_use_id") or "")
        state = self._state
        if state is None or state.stopped:
            return _hook_decision("deny", "No active request.")
        if name == "question.ask":
            return _hook_decision("ask", "Clarifying question for the user.")
        decision = await self.gate.pre_tool(name, args, tid, state.safety, state.emit)
        return _hook_decision(decision.verdict, decision.reason)

    async def _can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        state = self._state
        if state is None or state.stopped:
            return PermissionResultDeny(message="The request was stopped.", interrupt=True)
        name = policy_name(tool_name)
        if name == "question.ask":
            return await self._ask_question(state, tool_input)
        result = await self.gate.confirm(
            name, tool_input, context.tool_use_id or "", state.safety, state.emit
        )
        if result.allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message=result.message)

    async def _ask_question(
        self, state: RunState, tool_input: dict[str, Any]
    ) -> PermissionResultAllow | PermissionResultDeny:
        questions = tool_input.get("questions") or []
        qid = self.gate.broker.new_id()
        await state.emit(QuestionEvent(id=qid, questions=questions))
        answers = await self.gate.broker.wait_answer(qid, self.gate.session_id)
        if not answers:
            return PermissionResultDeny(
                message="The user did not answer. Do not guess on anything costly; say what "
                "you need to know."
            )
        return PermissionResultAllow(updated_input={**tool_input, "answers": answers})

    async def _post_tool_use(
        self, input_data: Any, tool_use_id: str | None, context: Any
    ) -> HookJSONOutput:
        name = policy_name(str(input_data.get("tool_name", "")))
        if name == "question.ask":
            return cast(HookJSONOutput, {})
        ok, summary = _summarize_response(input_data.get("tool_response"))
        state = self._state
        tid = tool_use_id or str(input_data.get("tool_use_id") or "")
        emit = state.emit if state is not None else _drop
        await self.gate.post_tool(name, tid, ok, summary, emit)
        return cast(HookJSONOutput, {})

    async def _post_tool_fail(
        self, input_data: Any, tool_use_id: str | None, context: Any
    ) -> HookJSONOutput:
        name = policy_name(str(input_data.get("tool_name", "")))
        state = self._state
        tid = tool_use_id or str(input_data.get("tool_use_id") or "")
        emit = state.emit if state is not None else _drop
        await self.gate.post_tool(name, tid, False, str(input_data.get("error", ""))[:200], emit)
        return cast(HookJSONOutput, {})


async def _drop(_event: Event) -> None:
    return None


def _hook_decision(verdict: str, reason: str) -> HookJSONOutput:
    return cast(
        HookJSONOutput,
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": verdict,
                "permissionDecisionReason": reason,
            }
        },
    )


def _summarize_response(resp: Any) -> tuple[bool, str]:
    """(ok, one-line summary) from a tool response of any shape."""
    ok = True
    text = ""
    if isinstance(resp, dict):
        ok = not (resp.get("is_error") or resp.get("isError"))
        content = resp.get("content")
        if isinstance(content, list):
            text = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        else:
            text = str(resp.get("result") or content or "")
    elif isinstance(resp, list):
        text = " ".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in resp)
    else:
        text = str(resp or "")
    text = " ".join(text.split())
    return ok, (text[:160] + "…") if len(text) > 160 else text


def _text_delta(event: dict[str, Any]) -> str:
    if event.get("type") != "content_block_delta":
        return ""
    delta = event.get("delta") or {}
    if delta.get("type") != "text_delta":
        return ""
    text = delta.get("text")
    return text if isinstance(text, str) else ""


def _error_event(
    msg: ResultMessage, assistant_error: str | None, cost: float, usage: Usage
) -> ErrorEvent:
    if assistant_error:
        text = _ERROR_TEXT.get(assistant_error, _ERROR_TEXT["unknown"])
        retryable = assistant_error in _RETRYABLE
    elif msg.subtype == "error_max_turns":
        text, retryable = f"Stopped after {msg.num_turns} turns (turn limit).", False
    elif msg.subtype == "error_max_budget_usd":
        text, retryable = "Stopped: budget reached.", False
    else:
        detail = "; ".join(msg.errors or []) or msg.result or msg.subtype
        text = f"The request failed: {detail}"
        retryable = msg.api_error_status in {429, 500, 502, 503, 529}
    return ErrorEvent(text, retryable=retryable, cost_usd=cost, usage=usage)
