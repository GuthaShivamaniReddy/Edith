# 4. Don't build what already exists

Status: accepted · 2026-11-10

## Decision
Use the Claude Agent SDK for the agent loop, Playwright MCP for the browser and Home Assistant for devices. Our code is the glue, the memory, the voice loop and the safety layer.

## Consequences
We depend on a v0.x SDK that releases weekly. Mitigation: exact version pins and the Brain interface (ADR 0010), so the SDK can be swapped for a plain Messages API loop.
