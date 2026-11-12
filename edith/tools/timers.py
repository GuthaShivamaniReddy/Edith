"""Timers: in-memory countdowns that notify every connected client when they fire.

They do not survive a restart; durable reminders arrive with the scheduler (Phase 6).
"""

from __future__ import annotations

import asyncio
import itertools
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from edith.core.events import NotificationEvent
from edith.tools.base import ToolContext, ToolError, ToolSpec, schema

MAX_SECONDS = 24 * 3600


@dataclass
class Timer:
    id: str
    label: str
    due: datetime
    task: asyncio.Task[None]


class TimerService:
    def __init__(self, notify: Callable[[NotificationEvent], None]) -> None:
        self._notify = notify
        self._timers: dict[str, Timer] = {}
        self._ids = itertools.count(1)

    def set(self, seconds: float, label: str = "") -> Timer:
        if not 1 <= seconds <= MAX_SECONDS:
            raise ToolError("Timers run from 1 second to 24 hours.")
        tid = str(next(self._ids))
        label = label.strip() or "Timer"
        due = datetime.now().astimezone() + timedelta(seconds=seconds)
        task = asyncio.get_running_loop().create_task(self._run(tid, seconds, label))
        timer = Timer(tid, label, due, task)
        self._timers[tid] = timer
        return timer

    async def _run(self, tid: str, seconds: float, label: str) -> None:
        try:
            await asyncio.sleep(seconds)
            self._notify(NotificationEvent(title="Timer done", text=f"{label} is done."))
        finally:
            self._timers.pop(tid, None)

    def list(self) -> list[Timer]:
        return sorted(self._timers.values(), key=lambda t: t.due)

    def cancel(self, tid: str) -> bool:
        timer = self._timers.pop(tid, None)
        if timer is None:
            return False
        timer.task.cancel()
        return True

    def close(self) -> None:
        for t in list(self._timers.values()):
            t.task.cancel()
        self._timers.clear()


def _seconds(args: dict[str, Any]) -> float:
    total = (
        float(args.get("hours") or 0) * 3600
        + float(args.get("minutes") or 0) * 60
        + float(args.get("seconds") or 0)
    )
    if total <= 0:
        raise ToolError("Give a duration in hours, minutes and/or seconds.")
    return total


def human_duration(seconds: float) -> str:
    s = round(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts = [f"{h} hour{'s' * (h != 1)}"] * bool(h) + [f"{m} minute{'s' * (m != 1)}"] * bool(m)
    parts += [f"{sec} second{'s' * (sec != 1)}"] * bool(sec)
    return " ".join(parts) or "0 seconds"


async def set_timer(args: dict[str, Any], ctx: ToolContext) -> str:
    secs = _seconds(args)
    t = ctx.timers.set(secs, str(args.get("label") or ""))
    return f"Timer {t.id} '{t.label}' set for {human_duration(secs)} (done at {t.due:%H:%M:%S})."


async def list_timers(args: dict[str, Any], ctx: ToolContext) -> str:
    timers = ctx.timers.list()
    if not timers:
        return "No timers running."
    now = datetime.now().astimezone()
    return "\n".join(
        f"{t.id}: {t.label}, {human_duration((t.due - now).total_seconds())} left" for t in timers
    )


async def cancel_timer(args: dict[str, Any], ctx: ToolContext) -> str:
    tid = re.sub(r"\D", "", str(args.get("id", "")))
    if not ctx.timers.cancel(tid):
        raise ToolError(f"No running timer with id {tid or '?'}.")
    return f"Timer {tid} cancelled."


def specs() -> list[ToolSpec]:
    dur = {"type": "number", "minimum": 0}
    return [
        ToolSpec(
            "timers",
            "set",
            "Start a countdown timer. EDITH notifies the user when it ends.",
            schema(
                {"hours": dur, "minutes": dur, "seconds": dur, "label": {"type": "string"}},
                required=[],
            ),
            set_timer,
            describe=lambda a: f"Set a {human_duration(_seconds(a))} timer",
        ),
        ToolSpec("timers", "list", "List running timers.", schema({}), list_timers),
        ToolSpec(
            "timers",
            "cancel",
            "Cancel a running timer by id.",
            schema({"id": {"type": "string"}}),
            cancel_timer,
            describe=lambda a: f"Cancel timer {a['id']}",
        ),
    ]
