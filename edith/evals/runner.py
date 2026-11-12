"""Task evaluation suite (PLAN.md section 9): `uv run edith eval`.

Each scenario runs against a fresh Orchestrator with:
- a FakeAdapter (volume, apps, clipboard, screenshots, trash are simulated),
- a temporary data folder and a temporary file sandbox (never your real files),
- the REAL model and REAL web tools, so a run costs money. Spend is recorded in your
  real cost ledger and counts against the daily budget.

Confirmations and clarifying questions are answered by the scenario's script.
Scoring uses exact checks only (tools that ran, events, final state, answer text);
an LLM judge for answer quality comes later.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from edith.config import EdithConfig, FilesConfig, PathsConfig, config_dir
from edith.core.events import (
    ConfirmRequestEvent,
    ErrorEvent,
    Event,
    FinalEvent,
    QuestionEvent,
    Request,
    ToolStartEvent,
)
from edith.core.orchestrator import Orchestrator
from edith.observability.costs import CostLedger
from edith.safety.confirm import GrantScope
from edith.safety.policy import Permissions
from edith.tools.system.fake import FakeAdapter

SCENARIOS_FILE = Path(__file__).resolve().parents[2] / "tests" / "evals" / "scenarios.yaml"
EVAL_APPS = {"notepad": "notepad.exe", "calculator": "calc.exe", "paint": "mspaint.exe"}

ConfirmPolicy = Literal["approve", "approve_session", "deny", "ignore"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Setup(_Strict):
    files: dict[str, str] = Field(default_factory=dict)  # sandbox-relative path -> text
    clipboard: str = ""
    volume: int = 50
    muted: bool = False
    notes: dict[str, str] = Field(default_factory=dict)  # title -> body
    timers: dict[str, float] = Field(default_factory=dict)  # label -> seconds
    audit: list[dict[str, Any]] = Field(default_factory=list)  # outcome entries


class ExpectState(_Strict):
    volume: int | None = None
    muted: bool | None = None
    opened_apps: list[str] | None = None
    opened_url_contains: str | None = None
    no_urls_opened: bool = False
    clipboard_contains: str | None = None
    files_exist: list[str] = Field(default_factory=list)
    files_missing: list[str] = Field(default_factory=list)
    note_contains: str | None = None
    timers_running: int | None = None
    screenshots: int | None = None


class Scenario(_Strict):
    id: str
    prompt: str
    tags: list[str] = Field(default_factory=list)
    setup: Setup = Setup()
    confirm: ConfirmPolicy = "approve"
    expect_tools: list[str] = Field(default_factory=list)  # all must run
    expect_any_tool: list[str] = Field(default_factory=list)  # at least one must run
    forbid_tools: list[str] = Field(default_factory=list)  # none may run
    expect_events: list[str] = Field(default_factory=list)
    expect_confirm_kinds: list[str] = Field(default_factory=list)
    expect_state: ExpectState = ExpectState()
    answer_contains: list[str] = Field(default_factory=list)  # all, case-insensitive
    answer_any: list[str] = Field(default_factory=list)  # at least one
    answer_not_contains: list[str] = Field(default_factory=list)
    expect_error: bool = False
    expect_route: str | None = None
    no_tools: bool = False
    check: str = ""  # human description (future LLM judge)
    timeout_s: float = 180


def load_scenarios(path: Path = SCENARIOS_FILE) -> list[Scenario]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    scenarios = [Scenario.model_validate(r) for r in raw]
    ids = [s.id for s in scenarios]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"Duplicate scenario ids: {sorted(dupes)}")
    return scenarios


@dataclass
class Outcome:
    id: str
    passed: bool
    failures: list[str]
    cost_usd: float
    latency_s: float
    tools_ran: list[str]
    route: str = ""
    answer: str = ""


@dataclass
class Run:
    """Everything observed while one scenario ran."""

    events: list[Event] = field(default_factory=list)
    tools_ran: list[str] = field(default_factory=list)
    confirm_kinds: list[str] = field(default_factory=list)
    answer: str = ""
    error: str = ""
    route: str = ""
    cost_usd: float = 0.0
    timed_out: bool = False
    timers_running: int = 0


OrchestratorFactory = Callable[[EdithConfig, FakeAdapter, Permissions], Orchestrator]


def _default_orchestrator(
    cfg: EdithConfig, adapter: FakeAdapter, perms: Permissions
) -> Orchestrator:
    return Orchestrator(cfg, adapter=adapter, permissions=perms)


class EvalRunner:
    def __init__(
        self,
        base_cfg: EdithConfig,
        permissions: Permissions | None = None,
        orchestrator_factory: OrchestratorFactory = _default_orchestrator,
        ledger: CostLedger | None = None,
    ) -> None:
        self.base_cfg = base_cfg
        self.permissions = permissions or Permissions.load(config_dir() / "permissions.yaml")
        self.factory = orchestrator_factory
        # Real spend goes to the real ledger, so evals count against the daily budget.
        self.ledger = ledger or CostLedger(base_cfg.data_dir / "costs.jsonl")

    async def run_one(self, sc: Scenario) -> Outcome:
        with tempfile.TemporaryDirectory(prefix="edith-eval-") as tmp:
            root = Path(tmp)
            sandbox = root / "sandbox"
            cfg = self._cfg(root, sandbox)
            adapter = FakeAdapter()
            orch = self.factory(cfg, adapter, self.permissions)
            orch.ledger = self.ledger
            started = time.perf_counter()
            try:
                await self._setup(sc.setup, cfg, sandbox, adapter, orch)
                run = await self._drive(orch, sc, sandbox)
                run.timers_running = len(orch.timers.list())
            finally:
                await orch.close()
            latency = time.perf_counter() - started
            failures = check(sc, run, adapter, cfg, sandbox)
        return Outcome(
            id=sc.id,
            passed=not failures,
            failures=failures,
            cost_usd=run.cost_usd,
            latency_s=latency,
            tools_ran=run.tools_ran,
            route=run.route,
            answer=run.answer or run.error,
        )

    def _cfg(self, root: Path, sandbox: Path) -> EdithConfig:
        sandbox.mkdir(parents=True)
        (sandbox / "private").mkdir()
        return self.base_cfg.model_copy(
            update={
                "paths": PathsConfig(data_dir=root / "data"),
                "files": FilesConfig(
                    allowed_roots=[str(sandbox)], blocked_roots=[str(sandbox / "private")]
                ),
                "apps": dict(EVAL_APPS),
            }
        )

    @staticmethod
    async def _setup(
        setup: Setup, cfg: EdithConfig, sandbox: Path, adapter: FakeAdapter, orch: Orchestrator
    ) -> None:
        for rel, text in setup.files.items():
            path = sandbox / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        adapter.clipboard = setup.clipboard
        adapter.volume = setup.volume
        adapter.muted = setup.muted
        for title, body in setup.notes.items():
            cfg.notes_dir.mkdir(parents=True, exist_ok=True)
            (cfg.notes_dir / f"2026-11-01 {title}.md").write_text(
                f"# {title}\n\n{body}\n", encoding="utf-8"
            )
        for label, seconds in setup.timers.items():
            orch.timers.set(seconds, label)
        for entry in setup.audit:
            orch.audit.outcome(
                str(entry.get("tool", "")), "setup", bool(entry.get("ok", True)),
                str(entry.get("summary", "")),
            )  # fmt: skip

    @staticmethod
    async def _drive(orch: Orchestrator, sc: Scenario, sandbox: Path) -> Run:
        run = Run()
        text = sc.prompt.replace("{sandbox}", str(sandbox))
        req = Request(text=text, source="eval", session_id=f"eval-{sc.id}")

        async def consume() -> None:
            async for ev in orch.handle(req):
                run.events.append(ev)
                if isinstance(ev, ToolStartEvent):
                    run.tools_ran.append(ev.tool)
                elif isinstance(ev, ConfirmRequestEvent):
                    run.confirm_kinds.append(ev.kind)
                    if sc.confirm != "ignore":
                        approved = sc.confirm.startswith("approve")
                        scope: GrantScope = "session" if sc.confirm == "approve_session" else "once"
                        orch.confirm(ev.id, approved, scope)
                elif isinstance(ev, QuestionEvent):
                    orch.answer(ev.id, _first_options(ev))
                elif isinstance(ev, FinalEvent):
                    run.answer, run.route, run.cost_usd = ev.text, ev.route, ev.cost_usd
                elif isinstance(ev, ErrorEvent):
                    run.error, run.cost_usd = ev.message, ev.cost_usd

        try:
            await asyncio.wait_for(consume(), sc.timeout_s)
        except TimeoutError:
            run.timed_out = True
            await orch.stop(req.session_id)
        return run


def _first_options(ev: QuestionEvent) -> dict[str, str]:
    out: dict[str, str] = {}
    for q in ev.questions:
        options = q.get("options") or [{}]
        out[str(q.get("question", ""))] = str(options[0].get("label", ""))
    return out


def check(
    sc: Scenario, run: Run, adapter: FakeAdapter, cfg: EdithConfig, sandbox: Path
) -> list[str]:
    """Every failed expectation, as a readable line. Empty = pass."""
    f: list[str] = []
    if run.timed_out:
        f.append(f"timed out after {sc.timeout_s:.0f}s")
    if sc.expect_error and not run.error:
        f.append("expected an error, got a normal answer")
    if not sc.expect_error and run.error:
        f.append(f"error: {run.error}")

    ran = set(run.tools_ran)
    f += [f"tool {t} did not run" for t in sc.expect_tools if t not in ran]
    if sc.expect_any_tool and not ran & set(sc.expect_any_tool):
        f.append(f"none of {sc.expect_any_tool} ran")
    f += [f"forbidden tool {t} ran" for t in sc.forbid_tools if t in ran]
    if sc.no_tools and ran:
        f.append(f"expected no tools, but ran {sorted(ran)}")
    if sc.expect_route and run.route != sc.expect_route:
        f.append(f"route was {run.route!r}, expected {sc.expect_route!r}")

    types = {ev.type for ev in run.events}
    f += [f"no {e} event" for e in sc.expect_events if e not in types]
    f += [
        f"no '{k}' confirmation (saw {run.confirm_kinds})"
        for k in sc.expect_confirm_kinds
        if k not in run.confirm_kinds
    ]
    answer = run.answer.lower()
    f += [f"answer lacks {s!r}" for s in sc.answer_contains if s.lower() not in answer]
    if sc.answer_any and not any(s.lower() in answer for s in sc.answer_any):
        f.append(f"answer has none of {sc.answer_any}")
    f += [f"answer contains {s!r}" for s in sc.answer_not_contains if s.lower() in answer]

    st = sc.expect_state
    if st.timers_running is not None and run.timers_running != st.timers_running:
        f.append(f"{run.timers_running} timers running, expected {st.timers_running}")
    f += _check_state(st, adapter, cfg, sandbox)
    return f


def _check_state(st: ExpectState, a: FakeAdapter, cfg: EdithConfig, sandbox: Path) -> list[str]:
    f: list[str] = []
    if st.volume is not None and a.volume != st.volume:
        f.append(f"volume is {a.volume}, expected {st.volume}")
    if st.muted is not None and a.muted != st.muted:
        f.append(f"muted is {a.muted}, expected {st.muted}")
    if st.opened_apps is not None and a.opened_apps != st.opened_apps:
        f.append(f"opened apps {a.opened_apps}, expected {st.opened_apps}")
    if st.opened_url_contains and not any(st.opened_url_contains in u for u in a.opened_urls):
        f.append(f"no opened URL contains {st.opened_url_contains!r} ({a.opened_urls})")
    if st.no_urls_opened and a.opened_urls:
        f.append(f"URLs were opened: {a.opened_urls}")
    if (
        st.clipboard_contains is not None
        and st.clipboard_contains.lower() not in a.clipboard.lower()
    ):
        f.append(f"clipboard is {a.clipboard[:80]!r}")
    f += [f"{p} is missing" for p in st.files_exist if not (sandbox / p).exists()]
    f += [f"{p} still exists" for p in st.files_missing if (sandbox / p).exists()]
    if st.note_contains is not None:
        notes = cfg.notes_dir.glob("*.md") if cfg.notes_dir.exists() else []
        pattern = re.compile(re.escape(st.note_contains), re.IGNORECASE)
        if not any(pattern.search(n.read_text(encoding="utf-8")) for n in notes):
            f.append(f"no note contains {st.note_contains!r}")
    if st.screenshots is not None and len(a.screenshots) != st.screenshots:
        f.append(f"{len(a.screenshots)} screenshots taken, expected {st.screenshots}")
    return f


@dataclass
class Summary:
    outcomes: list[Outcome]

    @property
    def passed(self) -> int:
        return sum(o.passed for o in self.outcomes)

    @property
    def pass_rate(self) -> float:
        return self.passed / len(self.outcomes) if self.outcomes else 0.0

    @property
    def total_cost(self) -> float:
        return sum(o.cost_usd for o in self.outcomes)

    @property
    def avg_cost(self) -> float:
        return self.total_cost / len(self.outcomes) if self.outcomes else 0.0

    @property
    def avg_latency(self) -> float:
        return (
            sum(o.latency_s for o in self.outcomes) / len(self.outcomes) if self.outcomes else 0.0
        )
