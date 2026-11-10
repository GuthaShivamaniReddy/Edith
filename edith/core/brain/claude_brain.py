"""ClaudeBrain: the Claude Agent SDK behind the Brain interface.

Phase 0 runs with NO tools: built-in Claude Code tools are switched off
(tools=[]), user/project settings are not loaded (setting_sources=[]) and no
external MCP config is picked up (strict_mcp_config=True). Nothing can act
until the Phase 1 safety gate exists.

Credentials: a Claude Console API key from the OS keychain if one is stored,
otherwise the Claude Code login on this machine. Never a file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLINotFoundError,
    ConversationResetMessage,
    Message,
    ResultMessage,
    StreamEvent,
    TextBlock,
)

from edith.config import EdithConfig
from edith.core.brain.base import Brain, BrainError
from edith.core.events import ErrorEvent, Event, FinalEvent, Request, Source, TokenEvent
from edith.core.prompts import render_system_prompt
from edith.observability.costs import Usage
from edith.observability.logging import get_logger
from edith.safety.secrets import SecretError, get_secret

log = get_logger(__name__)

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
    async def disconnect(self) -> None: ...


ClientFactory = Callable[[ClaudeAgentOptions], SDKClient]


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


class ClaudeBrain(Brain):
    def __init__(
        self,
        cfg: EdithConfig,
        source: Source = "cli",
        model: str | None = None,
        client_factory: ClientFactory = _default_factory,
    ) -> None:
        self.cfg = cfg
        self.source: Source = source
        self.model = model or cfg.models.main
        self._factory = client_factory
        self._client: SDKClient | None = None
        self._session_total_usd = 0.0  # SDK reports a running session total
        self.credential_source = ""

    def build_options(self) -> ClaudeAgentOptions:
        env, self.credential_source = credential_env()
        workspace = self.cfg.data_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        return ClaudeAgentOptions(
            model=self.model,
            system_prompt=render_system_prompt(self.cfg, source=self.source),
            tools=[],  # no built-in tools until the Phase 1 safety gate
            allowed_tools=[],
            setting_sources=[],  # ignore ~/.claude and project settings, CLAUDE.md
            strict_mcp_config=True,  # ignore MCP servers configured outside EDITH
            max_turns=self.cfg.agent.max_turns,
            include_partial_messages=True,  # stream tokens
            effort=self.cfg.models.effort,
            cwd=str(Path(workspace)),
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
        if self._client is not None:
            try:
                await self._client.disconnect()
            except ClaudeSDKError as e:
                log.warning("brain_disconnect_failed", error=str(e))
            self._client = None

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()

    async def run(self, request: Request) -> AsyncIterator[Event]:
        if self._client is None:
            raise BrainError("ClaudeBrain.run() called before start()")

        text_parts: list[str] = []
        # Usage per API message id. With partial messages on, the same API message is
        # reported several times (message_start, message_delta, AssistantMessage
        # snapshots), each with a partial count; keep the max of each field per id.
        usage_by_msg: dict[str, Usage] = {}
        stream_msg_id = ""
        model = self.model
        assistant_error: str | None = None

        def note_usage(msg_id: str, raw: dict[str, Any] | None) -> None:
            if msg_id and raw:
                usage_by_msg[msg_id] = usage_by_msg.get(msg_id, Usage()).max(Usage.from_api(raw))

        try:
            await self._client.query(request.text)
            async for msg in self._client.receive_response():
                if isinstance(msg, StreamEvent):
                    ev = msg.event
                    if ev.get("type") == "message_start":
                        message = ev.get("message") or {}
                        stream_msg_id = str(message.get("id") or "")
                        note_usage(stream_msg_id, message.get("usage"))
                    elif ev.get("type") == "message_delta":
                        note_usage(stream_msg_id, ev.get("usage"))
                    token = _text_delta(ev)
                    if token and msg.parent_tool_use_id is None:
                        yield TokenEvent(token)

                elif isinstance(msg, AssistantMessage):
                    model = msg.model or model
                    if msg.error:
                        assistant_error = msg.error
                    text_parts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
                    note_usage(msg.message_id or msg.uuid or "", msg.usage)

                elif isinstance(msg, ConversationResetMessage):
                    self._session_total_usd = 0.0

                elif isinstance(msg, ResultMessage):
                    cost = self._reply_cost(msg.total_cost_usd)
                    usage = (
                        sum(usage_by_msg.values(), Usage())
                        if usage_by_msg
                        else Usage.from_api(msg.usage)
                    )
                    if msg.is_error:
                        yield _error_event(msg, assistant_error, cost, usage)
                    else:
                        yield FinalEvent(
                            text=msg.result if msg.result is not None else "".join(text_parts),
                            model=model,
                            cost_usd=cost,
                            usage=usage,
                            num_turns=msg.num_turns,
                            duration_ms=msg.duration_ms,
                            session_id=msg.session_id,
                        )
                    return
        except ClaudeSDKError as e:
            log.exception("brain_run_failed")
            yield ErrorEvent(f"Claude session error: {e}", retryable=True)
            return

        yield ErrorEvent("The reply ended without a result.", retryable=True)

    def _reply_cost(self, session_total: float | None) -> float:
        total = session_total or 0.0
        cost = total - self._session_total_usd if total >= self._session_total_usd else total
        self._session_total_usd = total
        return max(cost, 0.0)


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
        text, retryable = "Stopped: per-request budget reached.", False
    else:
        detail = "; ".join(msg.errors or []) or msg.result or msg.subtype
        text = f"The request failed: {detail}"
        retryable = msg.api_error_status in {429, 500, 502, 503, 529}
    return ErrorEvent(text, retryable=retryable, cost_usd=cost, usage=usage)
