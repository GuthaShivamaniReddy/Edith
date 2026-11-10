"""Cost counter: one JSONL record per model reply, plus day/month totals.

Stored at <data_dir>/costs.jsonl. Append-only, one file, easy to inspect.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @classmethod
    def from_api(cls, raw: dict[str, Any] | None) -> Usage:
        raw = raw or {}

        def n(key: str) -> int:
            v = raw.get(key)
            return int(v) if isinstance(v, int | float) else 0

        return cls(
            input_tokens=n("input_tokens"),
            output_tokens=n("output_tokens"),
            cache_read_input_tokens=n("cache_read_input_tokens"),
            cache_creation_input_tokens=n("cache_creation_input_tokens"),
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
        )

    def max(self, other: Usage) -> Usage:
        """Field-wise max: merges partial reports of the same API message."""
        return Usage(
            max(self.input_tokens, other.input_tokens),
            max(self.output_tokens, other.output_tokens),
            max(self.cache_read_input_tokens, other.cache_read_input_tokens),
            max(self.cache_creation_input_tokens, other.cache_creation_input_tokens),
        )

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens


@dataclass(frozen=True)
class CostRecord:
    cost_usd: float
    model: str
    usage: Usage
    trace_id: str = ""
    session_id: str = ""
    source: str = "cli"
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass(frozen=True)
class CostTotals:
    today_usd: float
    month_usd: float
    today_replies: int
    month_replies: int


class CostLedger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, rec: CostRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec)) + "\n")

    def totals(self, now: datetime | None = None, tz: tzinfo = UTC) -> CostTotals:
        """Totals for the current day and month, as seen in timezone `tz`."""
        now = (now or datetime.now(UTC)).astimezone(tz)
        today_usd = month_usd = 0.0
        today_n = month_n = 0
        if not self.path.exists():
            return CostTotals(0.0, 0.0, 0, 0)
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    ts = datetime.fromisoformat(row["ts"]).astimezone(tz)
                    cost = float(row["cost_usd"])
                except (ValueError, KeyError, TypeError):
                    continue  # a corrupt line must never break the budget check
                if (ts.year, ts.month) == (now.year, now.month):
                    month_usd += cost
                    month_n += 1
                    if ts.day == now.day:
                        today_usd += cost
                        today_n += 1
        return CostTotals(today_usd, month_usd, today_n, month_n)
