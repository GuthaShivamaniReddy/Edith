# 9. Measure everything

Status: accepted · 2026-11-10

## Decision
From week 1: a trace ID on every log line of a request, tokens and cost recorded per reply, and a daily budget cap. From Phase 1: an eval suite that gates every phase, and a change that lowers the pass rate doesn't merge.

## Consequences
Phase 0 writes logs/edith.jsonl and costs.jsonl under the data dir; `edith costs` reports them.
