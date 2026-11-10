# 6. Local-first data, cloud-smart brain

Status: accepted · 2026-11-10

## Decision
Memory, logs, audio, costs and files stay on this machine (default ~/.edith). Reasoning goes to Claude by default; a local Ollama model is the offline and privacy fallback (Phase 6). Secrets live in the OS keychain via keyring.

## Consequences
Backups are the user's job (nightly backup lands in Phase 7). Data never lives in the repo; .gitignore guards against it.
