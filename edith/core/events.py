"""Requests in, events out (PLAN.md section 6, Orchestrator).

Every event is a frozen dataclass with a `type` tag and serializes to JSON for
the WebSocket API with event_to_dict().
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from edith.observability.costs import Usage

Source = Literal["cli", "voice", "desktop", "telegram", "scheduler", "eval"]
RouteKind = Literal["fast_path", "quick_answer", "task", "deep_research"]


@dataclass(frozen=True)
class Request:
    text: str
    source: Source = "cli"
    session_id: str = "default"
    private: bool = False


@dataclass(frozen=True)
class TokenEvent:
    """A chunk of streamed answer text."""

    text: str
    type: Literal["token"] = "token"


@dataclass(frozen=True)
class ToolStartEvent:
    tool: str  # policy name, e.g. "files.read"
    summary: str  # human description of the exact action
    tool_use_id: str = ""
    type: Literal["tool_start"] = "tool_start"


@dataclass(frozen=True)
class ToolEndEvent:
    tool: str
    ok: bool
    summary: str
    tool_use_id: str = ""
    type: Literal["tool_end"] = "tool_end"


@dataclass(frozen=True)
class ConfirmRequestEvent:
    """EDITH wants to do something that needs your yes. Answer with confirm(id, ...)."""

    id: str
    tool: str
    action: str  # the exact action, e.g. "Move C:\\...\\draft.txt to the Recycle Bin"
    reason: str  # why it is asking
    kind: str  # access | outbound | consequential | untrusted
    tier: int
    allow_session: bool = False  # may the user say "allow for this session"?
    grant_label: str = ""  # what a session grant would cover
    type: Literal["confirm_request"] = "confirm_request"


@dataclass(frozen=True)
class QuestionEvent:
    """A clarifying question. Answer with answer(id, {question: chosen_label})."""

    id: str
    questions: list[dict[str, Any]]
    type: Literal["question"] = "question"


@dataclass(frozen=True)
class NotificationEvent:
    """Out-of-band message (timer fired, etc.), delivered to every connected client."""

    title: str
    text: str
    type: Literal["notification"] = "notification"


@dataclass(frozen=True)
class FinalEvent:
    """The reply is complete. Cost and usage are for this reply only."""

    text: str
    model: str
    cost_usd: float
    usage: Usage = field(default_factory=Usage)
    num_turns: int = 0
    duration_ms: int = 0
    session_id: str = ""
    route: str = ""
    type: Literal["final"] = "final"


@dataclass(frozen=True)
class ErrorEvent:
    """The reply failed. A failed reply can still have cost money, so it carries cost too."""

    message: str
    retryable: bool = False
    cost_usd: float = 0.0
    usage: Usage = field(default_factory=Usage)
    type: Literal["error"] = "error"


Event = (
    TokenEvent
    | ToolStartEvent
    | ToolEndEvent
    | ConfirmRequestEvent
    | QuestionEvent
    | NotificationEvent
    | FinalEvent
    | ErrorEvent
)


def event_to_dict(event: Event) -> dict[str, Any]:
    return asdict(event)
