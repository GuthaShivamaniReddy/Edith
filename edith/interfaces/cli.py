"""`edith` command line: chat, costs, secrets, status."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from edith import __version__
from edith.config import ConfigError, EdithConfig, config_dir, load_config
from edith.core.brain import BrainError
from edith.core.brain.claude_brain import credential_env
from edith.core.events import (
    ConfirmRequestEvent,
    ErrorEvent,
    FinalEvent,
    NotificationEvent,
    QuestionEvent,
    Request,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
)
from edith.core.orchestrator import Orchestrator
from edith.core.prompts import user_tz
from edith.observability.costs import CostLedger
from edith.observability.logging import configure_logging, get_logger
from edith.safety import secrets
from edith.safety.confirm import GrantScope
from edith.safety.policy import PolicyError
from edith.tools.registry import RegistryError

app = typer.Typer(help="EDITH - your personal AI assistant.", no_args_is_help=True)
secrets_app = typer.Typer(help="Manage secrets in the OS keychain.", no_args_is_help=True)
app.add_typer(secrets_app, name="secrets")

console = Console()
err = Console(stderr=True)
log = get_logger(__name__)

EXIT_WORDS = {"/exit", "/quit", "exit", "quit", "bye"}


def _setup() -> EdithConfig:
    try:
        cfg = load_config()
    except ConfigError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from e
    configure_logging(cfg.log_dir)
    return cfg


def _ledger(cfg: EdithConfig) -> CostLedger:
    return CostLedger(cfg.data_dir / "costs.jsonl")


@app.command()
def version() -> None:
    """Print the EDITH version."""
    console.print(f"EDITH {__version__}")


@app.command()
def chat(
    session: Annotated[str, typer.Option(help="Conversation id (grants are per session).")] = "cli",
) -> None:
    """Talk to EDITH in the terminal. Ctrl+C stops a reply; /exit leaves."""
    cfg = _setup()
    try:
        asyncio.run(_chat_loop(cfg, session))
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye.[/dim]")


def _orchestrator(cfg: EdithConfig) -> Orchestrator:
    try:
        return Orchestrator(cfg)
    except (PolicyError, RegistryError) as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from e


async def _chat_loop(cfg: EdithConfig, session_id: str) -> None:
    orch = _orchestrator(cfg)
    notes = asyncio.create_task(_print_notifications(orch))
    console.print(
        f"[bold cyan]EDITH[/bold cyan] online · {cfg.models.main} · {credential_env()[1]}. "
        "[dim]/exit to leave, /costs for spend, /grants to see or /revoke access. "
        "Ctrl+C stops a reply.[/dim]"
    )
    try:
        while True:
            try:
                text = await asyncio.to_thread(console.input, "[bold green]you >[/bold green] ")
            except EOFError:
                break
            text = text.strip()
            if not text:
                continue
            if text.lower() in EXIT_WORDS:
                break
            if text == "/costs":
                _print_costs(cfg, orch.ledger)
            elif text == "/grants":
                grants = orch.grants(session_id)
                console.print("\n".join(grants) if grants else "[dim]No access granted.[/dim]")
            elif text == "/revoke":
                revoked = orch.revoke_grants(session_id)
                console.print(f"Revoked {len(revoked)} grant(s).")
            else:
                await _reply(orch, Request(text=text, source="cli", session_id=session_id))
    except BrainError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    finally:
        notes.cancel()
        await orch.close()
        console.print("[dim]Goodbye.[/dim]")


async def _print_notifications(orch: Orchestrator) -> None:
    q = orch.subscribe()
    try:
        while True:
            ev = await q.get()
            if isinstance(ev, NotificationEvent):
                console.print(f"\n[bold yellow]{escape(ev.title)}:[/bold yellow] {escape(ev.text)}")
    finally:
        orch.unsubscribe(q)


async def _reply(orch: Orchestrator, req: Request) -> None:
    """Stream one reply. Ctrl+C during it is the kill switch, not an exit."""
    task = asyncio.create_task(_consume(orch, req))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
        await orch.stop(req.session_id)
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, 5)
        console.print("[dim](stopped)[/dim]")


async def _consume(orch: Orchestrator, req: Request) -> None:
    console.print("[bold cyan]edith >[/bold cyan] ", end="")
    streamed = False
    async for event in orch.handle(req):
        if isinstance(event, TokenEvent):
            streamed = True
            console.print(event.text, end="", markup=False, highlight=False, soft_wrap=True)
        elif isinstance(event, ToolStartEvent):
            console.print(f"\n[dim]-> {escape(event.summary)}[/dim]")
        elif isinstance(event, ToolEndEvent) and not event.ok:
            console.print(f"[dim]x {escape(event.summary)}[/dim]")
        elif isinstance(event, ConfirmRequestEvent):
            approved, scope = await _ask_confirm(event)
            orch.confirm(event.id, approved, scope)
        elif isinstance(event, QuestionEvent):
            orch.answer(event.id, await _ask_questions(event))
        elif isinstance(event, FinalEvent):
            if not streamed:
                console.print(event.text, markup=False, highlight=False, end="")
            u = event.usage
            console.print(
                f"\n[dim]{event.model} · {u.total_input_tokens:,} in / {u.output_tokens:,} out"
                f" · ${event.cost_usd:.4f}[/dim]\n"
            )
        elif isinstance(event, ErrorEvent):
            console.print(f"\n[red]{escape(event.message)}[/red]\n")


async def _ask_confirm(ev: ConfirmRequestEvent) -> tuple[bool, GrantScope]:
    console.print(
        f"\n[bold yellow]EDITH wants to:[/bold yellow] {escape(ev.action)}\n"
        f"[dim]{escape(ev.reason)}[/dim]"
    )
    options = "[y]es once / [n]o"
    if ev.allow_session:
        options += f" / [s] allow {ev.grant_label} for this session"
    answer = await asyncio.to_thread(console.input, escape(options) + " > ")
    choice = answer.strip().lower()[:1]
    if choice == "s" and ev.allow_session:
        return True, "session"
    return choice == "y", "once"


async def _ask_questions(ev: QuestionEvent) -> dict[str, str] | None:
    answers: dict[str, str] = {}
    for q in ev.questions:
        question = str(q.get("question", ""))
        labels = [str(o.get("label", "")) for o in q.get("options") or []]
        console.print(f"\n[bold yellow]?[/bold yellow] {escape(question)}")
        for i, label in enumerate(labels, 1):
            console.print(f"  {i}. {escape(label)}")
        raw = (await asyncio.to_thread(console.input, "number or your own answer > ")).strip()
        if not raw:
            return None
        picked = raw.isdigit() and 0 < int(raw) <= len(labels)
        answers[question] = labels[int(raw) - 1] if picked else raw
    return answers


@app.command()
def serve() -> None:
    """Run the local API and web page (http://127.0.0.1:8765 by default)."""
    import uvicorn

    from edith.interfaces.server import create_app

    cfg = _setup()
    console.print(
        f"EDITH listening on [bold]http://{cfg.server.host}:{cfg.server.port}[/bold] "
        "(this computer only). Ctrl+C to quit."
    )
    uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port, log_level="warning")


@app.command("eval")
def eval_(
    only: Annotated[
        str | None, typer.Option(help="Comma-separated scenario ids or tags to run.")
    ] = None,
    threshold: Annotated[float, typer.Option(help="Pass rate needed (0-1).")] = 0.85,
    max_cost: Annotated[float, typer.Option(help="Stop the run past this spend (USD).")] = 3.0,
    list_only: Annotated[bool, typer.Option("--list", help="List scenarios, run nothing.")] = False,
) -> None:
    """Run the task evaluation suite. Uses the real model: it costs money."""
    from edith.evals.report import run_suite

    cfg = _setup()
    raise typer.Exit(asyncio.run(run_suite(cfg, console, only, threshold, max_cost, list_only)))


def _print_costs(cfg: EdithConfig, ledger: CostLedger) -> None:
    t = ledger.totals(tz=user_tz(cfg))
    cap = cfg.budgets.per_day_usd
    console.print(
        f"Today: [bold]${t.today_usd:.4f}[/bold] over {t.today_replies} replies "
        f"(daily cap ${cap:.2f}, ${max(cap - t.today_usd, 0):.4f} left)\n"
        f"This month: [bold]${t.month_usd:.4f}[/bold] over {t.month_replies} replies"
    )


@app.command()
def costs() -> None:
    """Show today's and this month's spend."""
    cfg = _setup()
    _print_costs(cfg, _ledger(cfg))


@app.command()
def status() -> None:
    """Check config, credentials and the Claude Code CLI without calling the model."""
    cfg = _setup()
    _, cred = credential_env()
    claude = shutil.which("claude")
    console.print(f"EDITH {__version__} · Python {sys.version.split()[0]}")
    console.print(f"Config:      {config_dir() / 'edith.yaml'}")
    console.print(f"Data dir:    {cfg.data_dir}")
    console.print(f"Model:       {cfg.models.main} (effort: {cfg.models.effort or 'default'})")
    console.print(f"Credentials: {cred}")
    console.print(
        f"Claude CLI:  {claude or '[yellow]not on PATH (the SDK uses its bundled copy)[/yellow]'}"
    )


@secrets_app.command("set")
def secrets_set(
    name: Annotated[str, typer.Argument(help="Secret name, e.g. anthropic_api_key")],
) -> None:
    """Store a secret in the OS keychain. The value is read hidden, never from arguments."""
    _setup()
    value = typer.prompt(f"Value for {name}", hide_input=True)
    try:
        secrets.set_secret(name, value)
    except secrets.SecretError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    console.print(f"Stored {name} ({secrets.mask(value.strip())}) in the OS keychain.")


@secrets_app.command("delete")
def secrets_delete(name: str) -> None:
    """Remove a secret from the OS keychain."""
    _setup()
    try:
        removed = secrets.delete_secret(name)
    except secrets.SecretError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    console.print(f"Removed {name}." if removed else f"{name} was not set.")


@secrets_app.command("list")
def secrets_list() -> None:
    """Show which known secrets are set (never their values)."""
    _setup()
    for name, desc in secrets.KNOWN_SECRETS.items():
        try:
            state = "[green]set[/green]" if secrets.get_secret(name) else "[dim]not set[/dim]"
        except secrets.SecretError as e:
            state = f"[red]keychain error: {e}[/red]"
        console.print(f"{name}: {state}  [dim]{desc}[/dim]")


if __name__ == "__main__":
    app()
