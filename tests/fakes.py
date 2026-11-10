"""Scripted stand-in for ClaudeSDKClient: no subprocess, no network, no cost."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    ResultMessage,
    StreamEvent,
    TextBlock,
)


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


class FakeClient:
    def __init__(self, options: ClaudeAgentOptions, turns: list[Sequence[Message]]) -> None:
        self.options = options
        self.turns = list(turns)
        self.queries: list[str] = []
        self.connected = False
        self.interrupted = False

    async def connect(self, prompt: str | None = None) -> None:
        self.connected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[Message]:
        for msg in self.turns.pop(0):
            yield msg

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.connected = False


class FakeFactory:
    def __init__(self, *turns: Sequence[Message], connect_error: Exception | None = None) -> None:
        self.turns = list(turns)
        self.connect_error = connect_error
        self.client: FakeClient | None = None

    def __call__(self, options: ClaudeAgentOptions) -> FakeClient:
        self.client = FakeClient(options, self.turns)
        if self.connect_error is not None:
            err = self.connect_error

            async def fail(prompt: str | None = None) -> None:
                raise err

            self.client.connect = fail  # type: ignore[method-assign]
        return self.client


def events_of(items: list[Any], kind: type) -> list[Any]:
    return [e for e in items if isinstance(e, kind)]
