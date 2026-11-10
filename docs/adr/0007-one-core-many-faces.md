# 7. One core, many faces

Status: accepted · 2026-11-10

## Decision
A single background service holds the brain, tools and memory. Voice, terminal, desktop and phone are thin clients over one local WebSocket API (127.0.0.1 only).

## Consequences
In Phase 0 the terminal talks to the brain in-process; it moves behind the WebSocket in Phase 1. Clients must never contain business logic.
