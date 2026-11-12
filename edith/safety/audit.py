"""Append-only audit trail: every tool decision, confirmation, grant and outcome.

One JSON line per entry in <data_dir>/audit.jsonl. Inputs are redacted (secrets
masked) and long strings truncated. Answers "what did you do today?".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

import structlog

from edith.safety.redact import redact


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, kind: str, **fields: Any) -> None:
        ctx = structlog.contextvars.get_contextvars()
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "kind": kind,
            "trace_id": ctx.get("trace_id", ""),
            "session_id": ctx.get("session_id", ""),
            **redact(fields, max_str=300),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def decision(
        self,
        tool: str,
        tool_use_id: str,
        tool_input: dict[str, Any],
        tier: int,
        verdict: str,
        reason: str,
        via: str = "model",
    ) -> None:
        self.write(
            "decision",
            tool=tool,
            tool_use_id=tool_use_id,
            input=tool_input,
            tier=tier,
            verdict=verdict,
            reason=reason,
            via=via,
        )

    def confirmation(
        self, tool: str, tool_use_id: str, approved: bool, scope: str, timed_out: bool
    ) -> None:
        self.write(
            "confirmation",
            tool=tool,
            tool_use_id=tool_use_id,
            approved=approved,
            scope=scope,
            timed_out=timed_out,
        )

    def grant(self, scope: str, target: str) -> None:
        self.write("grant", scope=scope, target=target, duration="session")

    def outcome(self, tool: str, tool_use_id: str, ok: bool, summary: str) -> None:
        self.write("outcome", tool=tool, tool_use_id=tool_use_id, ok=ok, summary=summary)

    def entries(self, since: datetime | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    ts = datetime.fromisoformat(row["ts"])
                except (ValueError, KeyError, TypeError):
                    continue
                if since is None or ts >= since:
                    out.append(row)
        return out

    def today(self, tz: tzinfo) -> list[dict[str, Any]]:
        now = datetime.now(tz)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.entries(since=start)
