# EDITH

A personal AI assistant that runs as a service on your own computer. Claude (through the
Claude Agent SDK) is the brain, every ability is a tool behind a permission gate, and your
data stays on your machine. The full plan is in [docs/PLAN.md](docs/PLAN.md).

**Status: Phase 0 (foundations).** EDITH can hold a multi-turn conversation in the terminal,
streams its replies, and records the cost of each one. It has no tools yet by design: the
safety gate (Phase 1) comes before anything that can act.

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
   uv run edith chat
   ```

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
