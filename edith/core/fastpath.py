"""Fast path: fixed commands answered by pattern matching, with no model call.

Free and instant. Fast-path tool calls still go through the safety policy and the
audit log; anything the policy would ask about is handed to the model instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from edith.tools.timers import human_duration

STOP_WORDS = re.compile(
    r"^\s*(?:hey\s+)?(?:edith[\s,]*)?(?:stop|cancel( that)?|halt|abort|never ?mind)\s*[.!]*\s*$",
    re.IGNORECASE,
)

_PREFIX = r"^\s*(?:hey\s+)?(?:edith[\s,]*)?(?:please\s+|can you\s+|could you\s+)?"
_SUFFIX = r"\s*(?:please)?\s*[.!?]*\s*$"
_UNITS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600}


@dataclass(frozen=True)
class FastCommand:
    kind: Literal["stop", "reply", "tool"]
    reply: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)


def _m(pattern: str, text: str) -> re.Match[str] | None:
    return re.match(_PREFIX + pattern + _SUFFIX, text, re.IGNORECASE)


def match(text: str, now: datetime | None = None) -> FastCommand | None:
    if STOP_WORDS.match(text):
        return FastCommand("stop")

    if _m(r"what(?:'s| is) the time|what time is it", text):
        now = now or datetime.now().astimezone()
        return FastCommand("reply", reply=f"It's {now:%I:%M %p}.".replace(" 0", " "))
    if _m(r"what(?:'s| is) (?:the )?(?:date|day)(?: today)?|what day is (?:it|today)", text):
        now = now or datetime.now().astimezone()
        return FastCommand("reply", reply=f"It's {now:%A, %B} {now.day}, {now:%Y}.")

    if m := _m(r"(?:set |turn )?(?:the )?volume (?:to |at )?(\d{1,3})\s*%?", text):
        return FastCommand("tool", tool="system.set_volume", args={"level": min(int(m[1]), 100)})
    if m := _m(r"(?:turn (?:the )?)?volume (up|down)|turn (?:it|the volume) (up|down)", text):
        delta = 10 if (m[1] or m[2]).lower() == "up" else -10
        return FastCommand("tool", tool="system.set_volume", args={"delta": delta})
    if _m(r"mute(?: the)?(?: sound| audio| volume)?", text):
        return FastCommand("tool", tool="system.mute", args={"muted": True})
    if _m(r"unmute(?: the)?(?: sound| audio| volume)?", text):
        return FastCommand("tool", tool="system.mute", args={"muted": False})

    timer = _m(
        r"(?:set |start )?(?:an? )?(?:timer for (\d+)\s*(second|sec|minute|min|hour|hr)s?"
        r"|(\d+)[\s-]*(second|sec|minute|min|hour|hr)s?\s+timer)"
        r"(?:\s+(?:for|called|named)\s+(?:the\s+)?([\w\s'-]{1,40}?))?",
        text,
    )
    if timer:
        amount = int(timer[1] or timer[3])
        unit = (timer[2] or timer[4]).lower()
        seconds = amount * _UNITS[unit]
        label = (timer[5] or "").strip()
        return FastCommand(
            "tool",
            tool="timers.set",
            args={"seconds": seconds, "label": label or f"{human_duration(seconds)} timer"},
        )
    return None
