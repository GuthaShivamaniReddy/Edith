"""`edith eval`: run scenarios, print a table, save a JSON report."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from edith.config import EdithConfig
from edith.evals.runner import EvalRunner, Outcome, Scenario, Summary, load_scenarios


def select(scenarios: list[Scenario], only: str | None) -> list[Scenario]:
    if not only:
        return scenarios
    wanted = {w.strip() for w in only.split(",") if w.strip()}
    return [s for s in scenarios if s.id in wanted or wanted & set(s.tags)]


async def run_suite(
    cfg: EdithConfig,
    console: Console,
    only: str | None,
    threshold: float,
    max_cost: float,
    list_only: bool,
) -> int:
    """Returns the process exit code: 0 if the pass rate meets the threshold."""
    scenarios = select(load_scenarios(), only)
    if list_only:
        for s in scenarios:
            console.print(f"{s.id:38} [dim]{', '.join(s.tags)}[/dim]")
        console.print(f"{len(scenarios)} scenarios")
        return 0
    if not scenarios:
        console.print("[red]No scenarios match.[/red]")
        return 2

    runner = EvalRunner(cfg)
    outcomes: list[Outcome] = []
    console.print(
        f"Running {len(scenarios)} scenarios on the real model (stops past ${max_cost:.2f})."
    )
    for sc in scenarios:
        spent = sum(o.cost_usd for o in outcomes)
        if spent >= max_cost:
            console.print(f"[yellow]Stopped: spent ${spent:.2f}, the --max-cost limit.[/yellow]")
            break
        o = await runner.run_one(sc)
        outcomes.append(o)
        mark = "[green]PASS[/green]" if o.passed else "[red]FAIL[/red]"
        console.print(f"{mark} {sc.id:38} ${o.cost_usd:.4f} {o.latency_s:5.1f}s")
        for failure in o.failures:
            console.print(f"     [dim]- {escape(failure)}[/dim]")

    summary = Summary(outcomes)
    _print_summary(console, summary, threshold)
    path = cfg.data_dir / "evals" / f"{datetime.now():%Y-%m-%d_%H-%M-%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "pass_rate": summary.pass_rate,
        "passed": summary.passed,
        "total": len(outcomes),
        "total_cost_usd": summary.total_cost,
        "avg_cost_usd": summary.avg_cost,
        "avg_latency_s": summary.avg_latency,
        "outcomes": [dataclasses.asdict(o) for o in outcomes],
    }
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    console.print(f"[dim]Report: {path}[/dim]")
    complete = len(outcomes) == len(scenarios)
    return 0 if complete and summary.pass_rate >= threshold else 1


def _print_summary(console: Console, s: Summary, threshold: float) -> None:
    table = Table(show_header=False, box=None)
    ok = s.pass_rate >= threshold
    color = "green" if ok else "red"
    rate = f"[{color}]{s.pass_rate:.0%}[/{color}]"
    table.add_row("Pass rate", f"{rate} ({s.passed}/{len(s.outcomes)}, need {threshold:.0%})")
    table.add_row("Total cost", f"${s.total_cost:.4f}")
    table.add_row("Avg cost / task", f"${s.avg_cost:.4f}")
    table.add_row("Avg latency", f"{s.avg_latency:.1f}s")
    console.print(table)
