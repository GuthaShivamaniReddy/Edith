"""`edith` command line: chat, costs, secrets, status."""

from __future__ import annotations

import asyncio
import shutil
import sys
from typing import Annotated

import typer
from rich.console import Console

from edith import __version__
from edith.config import ConfigError, EdithConfig, config_dir, load_config
from edith.core.brain import BrainError
from edith.core.brain.claude_brain import ClaudeBrain, credential_env
from edith.core.events import ErrorEvent, FinalEvent, Request, TokenEvent
from edith.core.prompts import user_tz
from edith.core.session import ChatSession
from edith.observability.costs import CostLedger
from edith.observability.logging import configure_logging, get_logger
from edith.safety import secrets

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
    model: Annotated[str | None, typer.Option(help="Override the model for this session.")] = None,
) -> None:
    """Talk to EDITH in the terminal. Type /exit to leave, /costs for spend."""
    cfg = _setup()
    try:
        asyncio.run(_chat_loop(cfg, model))
    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye.[/dim]")


async def _chat_loop(cfg: EdithConfig, model: str | None) -> None:
    ledger = _ledger(cfg)
    brain = ClaudeBrain(cfg, source="cli", model=model)
    try:
        await brain.start()
    except BrainError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    session = ChatSession(cfg, brain, ledger)
    console.print(
        f"[bold cyan]EDITH[/bold cyan] online · {brain.model} · {brain.credential_source}. "
        "[dim]/exit to leave, /costs for spend.[/dim]"
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
                _print_costs(cfg, ledger)
                continue
            await _reply(session, text)
    finally:
        await brain.close()
        console.print("[dim]Goodbye.[/dim]")


async def _reply(session: ChatSession, text: str) -> None:
    console.print("[bold cyan]edith >[/bold cyan] ", end="")
    streamed = False
    async for event in session.ask(Request(text=text, source="cli")):
        if isinstance(event, TokenEvent):
            streamed = True
            console.print(event.text, end="", markup=False, highlight=False, soft_wrap=True)
        elif isinstance(event, FinalEvent):
            if not streamed:
                console.print(event.text, markup=False, highlight=False, end="")
            u = event.usage
            console.print(
                f"\n[dim]{event.model} · {u.total_input_tokens:,} in / {u.output_tokens:,} out"
                f" · ${event.cost_usd:.4f}[/dim]\n"
            )
        elif isinstance(event, ErrorEvent):
            console.print(f"\n[red]{event.message}[/red]\n")


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
