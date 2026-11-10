"""System prompt rendering."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from edith.config import EdithConfig
from edith.core.events import Source

PROMPTS_DIR = Path(__file__).parent

_SOURCE_LABEL: dict[Source, str] = {
    "cli": "terminal",
    "voice": "voice",
    "desktop": "desktop",
    "telegram": "phone",
    "scheduler": "scheduler",
}


def user_tz(cfg: EdithConfig) -> ZoneInfo:
    try:
        return ZoneInfo(cfg.user.timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def render_system_prompt(
    cfg: EdithConfig,
    source: Source = "cli",
    memories: str = "",
    now: datetime | None = None,
) -> str:
    template = (PROMPTS_DIR / "system.md").read_text(encoding="utf-8")
    tz = user_tz(cfg)
    now = (now or datetime.now(UTC)).astimezone(tz)
    return template.format(
        user_name=cfg.user.name,
        address=cfg.user.address,
        now=now.strftime("%A, %B %d, %Y, %I:%M %p"),
        timezone=cfg.user.timezone,
        city=cfg.user.city or "unknown",
        memories=memories or "nothing yet (memory arrives in Phase 2)",
        source=_SOURCE_LABEL[source],
    )
