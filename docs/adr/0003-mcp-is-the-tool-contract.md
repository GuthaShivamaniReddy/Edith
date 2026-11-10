# 3. MCP is the tool contract

Status: accepted · 2026-11-10

## Decision
Every tool is exposed through the Model Context Protocol: in-process SDK MCP servers for our own tools, external MCP servers (config/mcp_servers.yaml) for everything else.

## Consequences
Any brain (Claude or a local model) can use any tool, and existing servers (Playwright, Home Assistant) plug in without new code. Plain Python functions are the fallback.
