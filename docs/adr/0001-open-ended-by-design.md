# 1. Open-ended by design

Status: accepted · 2026-11-10

## Context
"Do everything" cannot be designed up front; an assistant can only do what its tools allow.

## Decision
Every ability is a tool, and adding one must be cheap (one MCP server or one Python function). All requests run through one agent loop: understand → plan → call tools → check result → answer. EDITH may draft new tools itself, registered only after user approval (PLAN.md §6, tool builder).

## Consequences
Capability grows weekly instead of in big designs. The tool registry and safety gate are the load-bearing parts and must stay simple.
