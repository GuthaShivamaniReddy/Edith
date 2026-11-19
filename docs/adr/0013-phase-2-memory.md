# 13. Phase 2: memory and the document index

Status: accepted · 2026-11-19

## Decisions
1. **One SQLite file, two indexes per table.** `<data_dir>/memory.db` holds facts, episodes
   (conversation summaries), indexed folders, documents and chunks. Each searchable table
   has an FTS5 keyword index and a sqlite-vec `vec0` index (cosine). Search fuses both with
   reciprocal rank fusion. A vector-only match counts only if it clears 0.57 and is within
   0.06 of the best match (bge-small scores unrelated text at about 0.50-0.56). Document
   search uses a looser 0.50 floor and no margin, because the user is searching on purpose.
   PLAN.md's separate people and preferences tables are a `kind` column on facts
   (fact | preference | person | project).
2. **Local embeddings: fastembed + BAAI/bge-small-en-v1.5 (384-dim, ONNX).** No torch. The
   model (~65 MB) is downloaded once into `~/.edith/models` (`EDITH_MODELS_DIR`) and then
   loaded with `local_files_only=True`, so later runs never touch the network. Hugging
   Face telemetry is off. Tests use a hashing embedder (`EDITH_FAKE_EMBED=1`): no downloads.
3. **Forget means gone.** `PRAGMA secure_delete=ON`, FTS5 `secure-delete`, and a rollback
   journal (no WAL), so deleted text is overwritten in the file. A test checks that the raw
   bytes no longer contain the forgotten text, and the vector is checked too.
4. **Tools and tiers (approved by the user, 2026-11-19).** `memory.remember` tier 1 (raised
   to "ask" after untrusted content in the same request, so a web page cannot plant a
   memory); `memory.recall` and `memory.list` tier 0; `memory.forget` tier 2, and its
   confirmation lists the exact memories (via `ToolSpec.describe_ctx`); `knowledge.search`
   tier 0 but needs a `documents` access grant each session, and its results count as
   local data and untrusted output.
5. **Injection before each request.** The orchestrator attaches a `<memory>` block to the
   user's message (not the system prompt, which stays cacheable): every fact while there
   are at most 25, otherwise the top 8 by relevance, plus the 3 latest conversation
   summaries. Facts come from the user's own conversation, so they are not "local data"
   for the outbound rule. Document passages are.
6. **After-session extraction (approved: on).** When a session ends (terminal exit,
   WebSocket disconnect, app quit), the fast model gets only the user's own messages from
   that session. It never sees tool results, files, web pages or EDITH's replies, and has
   no tools (`complete_once`: `--tools ""`, deny-all hooks). It returns JSON facts and a
   summary. Likely secrets (passwords, keys, long digit runs) are dropped. The call is
   skipped for evals and the scheduler, and when the daily budget is spent. It costs
   about $0.0005.
7. **Indexing is explicit.** `edith index add|refresh|list|remove`. Only folders inside
   `files.allowed_roots` (minus blocked roots) can be added. Files are re-read only when
   their mtime or size changes, and only when the user runs the command. There is no
   background watching. Formats: PDF (pypdf, page numbers kept for citations), DOCX
   (python-docx), Markdown, text. Chunks are about 900 characters with a 150-character
   overlap.
8. **User control without the model.** `edith memory list|forget|clear` work offline.

## Consequences
- New pinned dependencies: sqlite-vec, fastembed (pulls onnxruntime), pypdf, python-docx.
- Changing `memory.embed_model` to one with a different dimension is refused until
  memory.db is moved away (no silent re-embedding yet).
- A local model for private mode (Phase 6) could also do extraction without the cloud.
