# 2. The LLM is the brain; tools are the hands

Status: accepted · 2026-11-10

## Decision
EDITH can act only through tools. Every tool has a name, a typed input schema and a permission tier. The model never runs free-form commands.

## Consequences
A capability that has no tool must be refused or turned into a new tool; the model is told plainly when it lacks one (see the Phase 0 system prompt).
