"""activity.today: answers "what did you do today?" from the audit log."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from edith.tools.base import ToolContext, ToolSpec, schema


async def today(args: dict[str, Any], ctx: ToolContext) -> str:
    tz = ZoneInfo(ctx.cfg.user.timezone)
    entries = ctx.audit.today(tz)
    lines: list[str] = []
    for e in entries:
        t = datetime.fromisoformat(e["ts"]).astimezone(tz).strftime("%H:%M")
        if e["kind"] == "outcome":
            status = "done" if e.get("ok") else "failed"
            lines.append(f"{t} {e.get('tool')}: {status} - {e.get('summary', '')}")
        elif e["kind"] == "decision" and e.get("verdict") == "deny":
            lines.append(f"{t} {e.get('tool')}: refused - {e.get('reason', '')}")
        elif e["kind"] == "confirmation" and not e.get("approved"):
            why = "no answer" if e.get("timed_out") else "you declined"
            lines.append(f"{t} {e.get('tool')}: not done ({why})")
        elif e["kind"] == "grant":
            lines.append(f"{t} you granted {e.get('scope')} access: {e.get('target', '')}")
    if not lines:
        return "No actions taken today."
    return "Today's actions (from the audit log):\n" + "\n".join(lines[-100:])


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            "activity",
            "today",
            "List the actions EDITH took today (from its audit log).",
            schema({}),
            today,
        )
    ]
