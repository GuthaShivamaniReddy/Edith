# EDITH

A personal AI assistant that runs as a service on your own computer. Claude (through the
Claude Agent SDK) is the brain, every ability is a tool behind a permission gate, and your
data stays on your machine. The full plan is in [docs/PLAN.md](docs/PLAN.md).

**Status: Phase 1 (text agent and core tools).** EDITH chats in the terminal, a web page
or its own Windows window; uses web search, scoped files, system control, notes and timers
behind a safety gate that asks before anything risky; and records the cost of every reply.

## Setup (Windows, macOS or Linux)

1. Install [uv](https://docs.astral.sh/uv/) and the [Claude Code CLI](https://code.claude.com),
   then run `claude` once and sign in.
2. Install EDITH:

   ```sh
   uv sync
   uv run edith status
   ```

3. Talk to it:

   ```sh
   uv run edith chat        # terminal
   uv run edith app         # Windows window with a tray icon
   ```

   On Windows, `uv run edith app --shortcut` adds EDITH to the Start menu (pin it to the
   taskbar from there); `--remove-shortcut` takes it out again.

### Credentials

By default EDITH uses your Claude Code login. For an always-on assistant, an API key from the
Claude Console is better (predictable, capped costs). Store it in the OS keychain, never a file:

```sh
uv run edith secrets set anthropic_api_key   # value is read hidden
uv run edith secrets list                    # shows which are set, never values
```

## Commands

| Command | What it does |
|---|---|
| `edith chat` | Terminal conversation. `/costs` shows spend, `/exit` leaves. |
| `edith app` | Windows window with a tray icon. Closing hides it; Quit is in the tray menu. |
| `edith serve` | Local WebSocket API and web page on http://127.0.0.1:8765. |
| `edith eval` | Task eval suite on the real model (costs money; `--only`, `--max-cost`). |
| `edith costs` | Today's and this month's spend, against the daily cap. |
| `edith status` | Config, data dir, model, credential source. No model call. |
| `edith secrets set/list/delete` | Manage secrets in the OS keychain. |

## Configuration

- `config/edith.yaml`: your name, models, budgets, paths. No secrets.
- `config/permissions.yaml`: tool → permission tier (used from Phase 1).
- `config/mcp_servers.yaml`: external MCP servers (used from Phase 1).

Runtime data goes to `~/.edith` (override with `EDITH_DATA_DIR`):
`logs/edith.jsonl` (one JSON line per event, with a trace ID per request) and
`costs.jsonl` (one line per reply, with tokens and cost).

## Development

```sh
uv run pytest
uv run ruff format . && uv run ruff check . && uv run mypy
```

CI runs the same checks on Windows and Linux. Rules for coding agents are in
[CLAUDE.md](CLAUDE.md); architecture decisions are in [docs/adr](docs/adr/).

## Roadmap

| Phase | Builds |
|---|---|
| **0** ✅ | Repo, config, keychain secrets, logging, CI, `edith chat`, cost counter |
| 1 | Orchestrator, WebSocket API, router, safety layer, first tools, eval harness |
| 2 | Memory: facts, preferences, your documents |
| 3 | Voice: wake word, speech in and out, barge-in |
| 4 | Email, calendar, tasks, music, smart home, meeting notes |
| 5 | Browser and screen control |
| 6 | Proactive monitors, subagents, deep research, tool builder, offline mode |
| 7 | Desktop HUD, Telegram, autostart, hardening → v1.0 |
