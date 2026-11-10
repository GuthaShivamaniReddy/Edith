# EDITH — rules for the coding agent

## Source of truth
- docs/PLAN.md is the plan (extracted from EDITH_Build_Plan_and_Roadmap.pdf, which is canonical).
  Build only the phase you are asked for.
- Architecture decisions live in docs/adr/. Add one for any new decision.

## Stack (do not change without an ADR)
Python 3.12, uv, Claude Agent SDK (pinned), FastAPI, SQLite + sqlite-vec,
openWakeWord, Silero VAD, faster-whisper, Kokoro, APScheduler, pytest.

## Hard rules
- Never modify edith/safety/ or config/permissions.yaml without the user's
  explicit approval in this session.
- Every tool declares a tier in config/permissions.yaml. No tier = tier 2.
- No secrets in code, config, logs, tests or commits. Use edith/safety/secrets.py.
- OS-specific code goes only in edith/tools/system/<os>.py behind SystemAdapter.
- Shell commands are fixed templates with validated arguments, never raw strings
  from the model.
- Never run destructive commands (rm -rf, format, registry edits) on this machine.
- ClaudeBrain runs with tools=[], setting_sources=[], strict_mcp_config=True until
  the Phase 1 safety gate is wired in. Do not give the brain tools before that.

## Code standards
- Type hints everywhere; ruff + mypy (strict) clean; async I/O.
- Every new tool ships with unit tests and at least 2 eval scenarios.
- Small commits with clear messages; tests must pass before each commit.
- New dependency? Say why in the commit message and pin it (== in pyproject.toml).
- Tests never touch the real keychain, data dir or network: use the fixtures in
  tests/conftest.py and the fake SDK client in tests/fakes.py.

## Commands
- uv run pytest            # tests
- uv run ruff format . && uv run ruff check . && uv run mypy   # what CI runs
- uv run edith chat        # talk to EDITH in the terminal
- uv run edith status      # config, credentials, CLI check (no model call)
- uv run edith costs       # today's and this month's spend
- uv run edith eval        # task evaluation suite (Phase 1)

## Lessons learned
(append here at the end of each phase)

### Phase 0
- The Agent SDK's ResultMessage.total_cost_usd is a running total for the session
  (reset on ConversationResetMessage). Per-reply cost = difference from the previous total.
- With include_partial_messages=True, one API message is reported several times
  (message_start, AssistantMessage snapshots, message_delta) with partial token counts.
  Merge per message id with a field-wise max; the first snapshot under-counts output.
- tools=[] is passed to the CLI as `--tools ""`, which disables every built-in tool.
- Windows needs the tzdata package for zoneinfo.
- uv writes a .gitignore inside .venv, so ripgrep-based search skips installed
  packages; read SDK sources with a direct path instead.
