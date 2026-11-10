# 10. Phase 0 runtime decisions

Status: accepted · 2026-11-10

## Decisions
1. **No tools before the safety gate.** ClaudeBrain runs with `tools=[]` (passed as `--tools ""`, disabling every built-in Claude Code tool), `setting_sources=[]` (ignores ~/.claude settings and CLAUDE.md files) and `strict_mcp_config=True` (ignores MCP servers configured outside EDITH). A test asserts this.
2. **Brain interface.** `Brain.start/run/interrupt/close` hides the SDK. `run(request)` yields Token, Final and Error events; Phase 1 adds the context and tool parameters and more event types.
3. **Credentials.** A Claude Console API key from the OS keychain if one is stored, otherwise the Claude Code login. Never a file or env file.
4. **Cost per reply.** The SDK reports a running session total; per-reply cost is the difference. Token usage merges partial reports per API message with a field-wise max.
5. **Budgets.** The daily cap is enforced by ChatSession before each request, in the user's timezone. The per-request cap moves to the orchestrator in Phase 1 (the SDK's max_budget_usd applies to a whole session, not one request).
6. **Pinned versions.** Direct dependencies are pinned with == in pyproject.toml; uv.lock pins the rest. Python 3.12 is pinned with .python-version even though the machine also has 3.14.
7. **CI** runs ruff format, ruff check, mypy --strict and pytest on Windows and Linux.
