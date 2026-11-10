# 5. Safety is architecture, not a feature

Status: accepted · 2026-11-10

## Decision
Permission tiers (0 read, 1 reversible, 2 ask, 3 forbidden), confirmations that show the exact action, and an append-only audit log ship in Phase 1, before EDITH can touch anything important. The gate is wired into the Agent SDK's PreToolUse hook and can_use_tool callback, so it is the only path from brain to tools.

## Consequences
Until the gate exists, the brain gets no tools at all (ADR 0010). edith/safety/ and config/permissions.yaml change only with the user's explicit approval.
