from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from edith.observability.costs import CostLedger, CostRecord, Usage

NY = ZoneInfo("America/New_York")


def _rec(ts: str, cost: float) -> CostRecord:
    return CostRecord(cost_usd=cost, model="m", usage=Usage(), ts=ts)


def test_usage_from_api_and_add() -> None:
    a = Usage.from_api({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100})
    b = Usage.from_api({"input_tokens": 1, "output_tokens": 2, "junk": "x"})
    total = a + b
    assert total == Usage(11, 7, 100, 0)
    assert total.total_input_tokens == 111
    assert Usage.from_api(None) == Usage()


def test_usage_max_merges_partial_reports() -> None:
    start = Usage(input_tokens=1000, output_tokens=1)
    end = Usage(input_tokens=0, output_tokens=44)
    assert start.max(end) == Usage(input_tokens=1000, output_tokens=44)


def test_totals_day_and_month(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "costs.jsonl")
    ledger.record(_rec("2026-11-10T15:00:00+00:00", 0.10))
    ledger.record(_rec("2026-11-10T16:00:00+00:00", 0.05))
    ledger.record(_rec("2026-11-02T12:00:00+00:00", 1.00))
    ledger.record(_rec("2026-10-31T12:00:00+00:00", 9.00))  # last month
    t = ledger.totals(now=datetime(2026, 11, 10, 18, tzinfo=UTC))
    assert round(t.today_usd, 4) == 0.15
    assert t.today_replies == 2
    assert round(t.month_usd, 4) == 1.15
    assert t.month_replies == 3


def test_today_follows_user_timezone(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "costs.jsonl")
    # 02:00 UTC on Nov 11 is still Nov 10 (21:00) in New York.
    ledger.record(_rec("2026-11-11T02:00:00+00:00", 0.20))
    now = datetime(2026, 11, 11, 3, tzinfo=UTC)  # 22:00 Nov 10 in New York
    assert ledger.totals(now=now, tz=NY).today_usd == 0.20
    assert ledger.totals(now=datetime(2026, 11, 11, 12, tzinfo=UTC), tz=NY).today_usd == 0


def test_missing_file_and_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "costs.jsonl"
    assert CostLedger(path).totals().month_usd == 0
    path.write_text('not json\n{"ts": "bad"}\n', encoding="utf-8")
    ledger = CostLedger(path)
    ledger.record(_rec(datetime.now(UTC).isoformat(), 0.01))
    assert ledger.totals().today_usd == 0.01
