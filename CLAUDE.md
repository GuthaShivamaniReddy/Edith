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
- Every tool call goes through the SafetyGate (PreToolUse hook + can_use_tool, ADR 0011).
  Built-ins stay limited to WebSearch, WebFetch, AskUserQuestion; allowed_tools=[],
  setting_sources=[], strict_mcp_config=True. Never pre-approve a tool.
- Privacy: no access to files, clipboard, screen or system info without a user grant;
  local data never goes outbound without a confirmation showing what is sent.

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
- uv run edith serve       # local WebSocket API + web page on http://127.0.0.1:8765
- uv run edith status      # config, credentials, CLI check (no model call)
- uv run edith costs       # today's and this month's spend
- uv run edith eval        # task eval suite: REAL model, costs money (--only, --max-cost, --list)

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

### Phase 1
- The SDK sends "ask" from a PreToolUse hook on to can_use_tool; that is where the
  confirmation waits. tests/fakes.py drives scripted ToolCalls through the same hooks.
- Anything a tool does to the real machine (even moving to the Recycle Bin) must go
  through SystemAdapter, so tests and evals can use FakeAdapter.
- Router keywords are cheap but blunt: "today" routed greetings to Sonnet at 20x cost.
  Add each misroute the evals find as a router test.
- Eval prompts use {sandbox} for the temp folder; never point evals at real folders.
