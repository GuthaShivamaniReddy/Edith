"""Requests in, events out (PLAN.md section 6, Orchestrator).

Phase 0 uses Token, Final and Error. Phase 1 adds tool_start, tool_end,
confirm_request and question when tools and the safety gate arrive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from edith.observability.costs import Usage

Source = Literal["cli", "voice", "desktop", "telegram", "scheduler"]


@dataclass(frozen=True)
class Request:
    text: str
    source: Source = "cli"
    private: bool = False


@dataclass(frozen=True)
class TokenEvent:
    """A chunk of streamed answer text."""

    text: str
    type: Literal["token"] = "token"


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
    type: Literal["final"] = "final"


@dataclass(frozen=True)
class ErrorEvent:
    """The reply failed. A failed reply can still have cost money, so it carries cost too."""

    message: str
    retryable: bool = False
    cost_usd: float = 0.0
    usage: Usage = field(default_factory=Usage)
    type: Literal["error"] = "error"


Event = TokenEvent | FinalEvent | ErrorEvent
