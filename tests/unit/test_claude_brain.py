from __future__ import annotations

from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    CLINotFoundError,
    Message,
    ResultMessage,
    TextBlock,
)

from edith.config import EdithConfig
from edith.core.brain import BrainError
from edith.core.brain.claude_brain import ClaudeBrain
from edith.core.events import ErrorEvent, FinalEvent, Request, TokenEvent
from edith.observability.costs import Usage
from edith.safety import secrets
from tests.fakes import FakeFactory, events_of, text_turn


async def _collect(brain: ClaudeBrain, text: str) -> list[Any]:
    return [e async for e in brain.run(Request(text=text))]


def test_options_disable_all_tools_and_outside_settings(cfg: EdithConfig) -> None:
    """Phase 0 has no safety gate, so the brain must not be able to act at all."""
    opts = ClaudeBrain(cfg).build_options()
    assert opts.tools == []
    assert opts.allowed_tools == []
    assert opts.setting_sources == []
    assert opts.strict_mcp_config is True
    assert opts.mcp_servers == {}
    assert opts.model == "claude-sonnet-5-5"
    assert opts.max_turns == 25
    assert opts.effort == "medium"
    assert isinstance(opts.system_prompt, str) and "You are EDITH" in opts.system_prompt


def test_credentials_default_to_claude_login(cfg: EdithConfig) -> None:
    brain = ClaudeBrain(cfg)
    opts = brain.build_options()
    assert "ANTHROPIC_API_KEY" not in opts.env
    assert brain.credential_source == "Claude Code login"


def test_credentials_from_keychain(cfg: EdithConfig) -> None:
    secrets.set_secret("anthropic_api_key", "sk-ant-from-keychain-1234")
    brain = ClaudeBrain(cfg)
    opts = brain.build_options()
    assert opts.env["ANTHROPIC_API_KEY"] == "sk-ant-from-keychain-1234"
    assert brain.credential_source == "API key from OS keychain"


async def test_streams_tokens_then_final(cfg: EdithConfig) -> None:
    factory = FakeFactory(text_turn("Hello, boss.", 0.004, input_tokens=1000, output_tokens=44))
    async with ClaudeBrain(cfg, client_factory=factory) as brain:
        events = await _collect(brain, "hi")
    tokens = events_of(events, TokenEvent)
    assert "".join(t.text for t in tokens) == "Hello, boss."
    final = events[-1]
    assert isinstance(final, FinalEvent)
    assert final.text == "Hello, boss."
    assert final.cost_usd == pytest.approx(0.004)
    # usage merges message_start, snapshot and message_delta -> the real totals
    assert final.usage == Usage(input_tokens=1000, output_tokens=44)
    assert factory.client is not None and factory.client.queries == ["hi"]
    assert factory.client.connected is False  # closed by the context manager


async def test_multi_turn_cost_is_per_reply(cfg: EdithConfig) -> None:
    """The SDK reports a running session total; each reply must get only its own share."""
    factory = FakeFactory(
        text_turn("one", 0.004, msg_id="a"),
        text_turn("two", 0.006, msg_id="b"),
    )
    async with ClaudeBrain(cfg, client_factory=factory) as brain:
        first = (await _collect(brain, "1"))[-1]
        second = (await _collect(brain, "2"))[-1]
    assert isinstance(first, FinalEvent) and isinstance(second, FinalEvent)
    assert first.cost_usd == pytest.approx(0.004)
    assert second.cost_usd == pytest.approx(0.002)


async def test_auth_error_is_explained(cfg: EdithConfig) -> None:
    turn: list[Message] = [
        AssistantMessage(
            content=[TextBlock(text="")], model="claude-sonnet-5-5", error="authentication_failed"
        ),
        ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=0,
            is_error=True,
            num_turns=1,
            session_id="s1",
            total_cost_usd=0.0,
        ),
    ]
    async with ClaudeBrain(cfg, client_factory=FakeFactory(turn)) as brain:
        events = await _collect(brain, "hi")
    assert len(events) == 1 and isinstance(events[0], ErrorEvent)
    assert "Not signed in" in events[0].message
    assert events[0].retryable is False


async def test_turn_limit_error(cfg: EdithConfig) -> None:
    turn = [
        ResultMessage(
            subtype="error_max_turns",
            duration_ms=10,
            duration_api_ms=5,
            is_error=True,
            num_turns=25,
            session_id="s1",
            total_cost_usd=0.03,
        )
    ]
    async with ClaudeBrain(cfg, client_factory=FakeFactory(turn)) as brain:
        (event,) = await _collect(brain, "loop forever")
    assert isinstance(event, ErrorEvent)
    assert "turn limit" in event.message
    assert event.cost_usd == pytest.approx(0.03)  # failed replies still cost money


async def test_missing_cli_gives_actionable_error(cfg: EdithConfig) -> None:
    brain = ClaudeBrain(cfg, client_factory=FakeFactory(connect_error=CLINotFoundError("nope")))
    with pytest.raises(BrainError, match="Claude Code CLI not found"):
        await brain.start()


async def test_run_before_start_fails(cfg: EdithConfig) -> None:
    with pytest.raises(BrainError, match="before start"):
        await _collect(ClaudeBrain(cfg, client_factory=FakeFactory()), "hi")


async def test_interrupt_reaches_client(cfg: EdithConfig) -> None:
    factory = FakeFactory()
    async with ClaudeBrain(cfg, client_factory=factory) as brain:
        await brain.interrupt()
    assert factory.client is not None and factory.client.interrupted
