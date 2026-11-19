"""MemoryStore: facts, conversation summaries and your documents, in one SQLite file.

<data_dir>/memory.db holds:
  facts      what EDITH knows about you (text, kind, subject, source, dates)
  episodes   one-paragraph summaries of past conversations
  folders / documents / chunks   the document index (`edith index add`)
Each searchable table has an FTS5 keyword index and a sqlite-vec vector index
(local embeddings); search fuses both with reciprocal rank fusion.

Forgetting is real: rows are deleted from the table and both indexes, with
SQLite's secure_delete (and FTS5's secure-delete) on, so the text is overwritten
in the file, not just unlinked. The journal is a rollback journal (no WAL), so no
copy lingers in a side file after the commit.

All methods are synchronous and thread-safe; async callers use asyncio.to_thread.
"""

from __future__ import annotations

import re
import sqlite3
import struct
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from edith.memory.embed import Embedder
from edith.memory.knowledge import ExtractError, chunk, extract, scan
from edith.observability.logging import get_logger
from edith.safety.scope import FileScope

log = get_logger(__name__)

SCHEMA_VERSION = "1"
FactKind = Literal["fact", "preference", "person", "project"]
FACT_KINDS: tuple[FactKind, ...] = ("fact", "preference", "person", "project")
MAX_FACT_CHARS = 1000
DUPLICATE_SIM = 0.93  # a new fact this close to an old one replaces it
# A vector-only match counts as relevant if it clears MIN_SIM and is within
# REL_MARGIN of the best match (bge-small: unrelated pairs score ~0.50-0.56).
MIN_SIM = 0.57
REL_MARGIN = 0.06
DOC_MIN_SIM = 0.50  # document search is deliberate: be more generous
RRF_K = 60
# Table names are interpolated into SQL only from this fixed set (never from input).
TABLES = frozenset({"facts", "episodes", "chunks"})
_TOKEN = re.compile(r"\w+", re.UNICODE)
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "do",
        "does",
        "did",
        "for",
        "from",
        "has",
        "have",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "so",
        "that",
        "the",
        "their",
        "them",
        "they",
        "this",
        "to",
        "us",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
        "about",
        "know",
        "tell",
        "remember",
    ]
)


class MemoryError_(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _vec(v: Sequence[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def fts_query(text: str) -> str:
    """An FTS5 query that ORs the meaningful words; '' if there are none."""
    words = [w for w in _TOKEN.findall(text.lower()) if w not in _STOP and len(w) > 1]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


@dataclass(frozen=True)
class Fact:
    id: int
    text: str
    kind: str
    subject: str
    source: str
    created_at: str
    last_used: str | None = None

    @property
    def ref(self) -> str:
        return f"f{self.id}"


@dataclass(frozen=True)
class Episode:
    id: int
    summary: str
    session_id: str
    created_at: str

    @property
    def ref(self) -> str:
        return f"e{self.id}"


@dataclass(frozen=True)
class DocHit:
    path: str
    page: int | None
    text: str
    score: float


@dataclass
class IndexReport:
    folder: str
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    chunks: int = 0
    errors: list[str] = field(default_factory=list)


class MemoryStore:
    def __init__(self, path: Path, embedder: Embedder) -> None:
        self.path = path
        self.embedder = embedder
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._load_vec()
        self._migrate()

    # ------------------------------------------------------------------ setup

    def _load_vec(self) -> None:
        import sqlite_vec

        self._db.enable_load_extension(True)
        try:
            sqlite_vec.load(self._db)
        finally:
            self._db.enable_load_extension(False)

    def _migrate(self) -> None:
        dim = self.embedder.dim
        with self._lock:
            db = self._db
            db.execute("PRAGMA secure_delete = ON")
            db.execute("PRAGMA journal_mode = DELETE")
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY, text TEXT NOT NULL, kind TEXT NOT NULL,
                    subject TEXT NOT NULL, source TEXT NOT NULL,
                    created_at TEXT NOT NULL, last_used TEXT);
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY, text TEXT NOT NULL, session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS folders (
                    path TEXT PRIMARY KEY, added_at TEXT NOT NULL, indexed_at TEXT);
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY,
                    folder TEXT NOT NULL REFERENCES folders(path) ON DELETE CASCADE,
                    path TEXT NOT NULL UNIQUE, mtime REAL NOT NULL, size INTEGER NOT NULL,
                    indexed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY,
                    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    page INTEGER, idx INTEGER NOT NULL, text TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
                """
            )
            for table in ("facts", "episodes", "chunks"):
                db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {table}_fts USING fts5(text)")
                db.execute(f"INSERT INTO {table}_fts({table}_fts, rank) VALUES('secure-delete', 1)")
                db.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {table}_vec USING "
                    f"vec0(embedding float[{dim}] distance_metric=cosine)"
                )
            row = db.execute("SELECT value FROM meta WHERE key='embed_dim'").fetchone()
            if row is None:
                db.execute("INSERT INTO meta VALUES ('embed_dim', ?)", (str(dim),))
                db.execute("INSERT INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
            elif int(row["value"]) != dim:
                raise MemoryError_(
                    f"memory.db was built with {row['value']}-dim embeddings, not {dim}. "
                    "Change memory.embed_model back, or move memory.db away to start fresh."
                )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ indexing helpers

    def _index(self, table: str, rowid: int, text: str, vector: Sequence[float]) -> None:
        _check_table(table)
        fts = f"INSERT INTO {table}_fts(rowid, text) VALUES (?, ?)"  # noqa: S608 - from TABLES
        vec = f"INSERT INTO {table}_vec(rowid, embedding) VALUES (?, ?)"  # noqa: S608 - from TABLES
        self._db.execute(fts, (rowid, text))
        self._db.execute(vec, (rowid, _vec(vector)))

    def _unindex(self, table: str, rowids: Sequence[int]) -> None:
        _check_table(table)
        for rid in rowids:
            self._db.execute(f"DELETE FROM {table}_fts WHERE rowid = ?", (rid,))  # noqa: S608 - table from TABLES
            self._db.execute(f"DELETE FROM {table}_vec WHERE rowid = ?", (rid,))  # noqa: S608 - table from TABLES

    def _search(
        self,
        table: str,
        query: str,
        k: int,
        vector: Sequence[float] | None = None,
        min_sim: float = MIN_SIM,
        margin: float | None = REL_MARGIN,
    ) -> list[tuple[int, float]]:
        """(rowid, fused score) best first. Keyword matches always count; vector-only
        matches must clear min_sim and (if margin is set) be near the best match."""
        _check_table(table)
        if k <= 0 or not query.strip():
            return []
        vector = vector if vector is not None else self.embedder.embed([query])[0]
        pool = max(k * 4, 20)
        vec_rows = self._db.execute(
            f"SELECT rowid, distance FROM {table}_vec WHERE embedding MATCH ? AND k = ? "  # noqa: S608 - table from TABLES
            "ORDER BY distance",
            (_vec(vector), pool),
        ).fetchall()
        fts_rows: list[sqlite3.Row] = []
        q = fts_query(query)
        if q:
            fts_rows = self._db.execute(
                f"SELECT rowid FROM {table}_fts WHERE {table}_fts MATCH ? ORDER BY rank LIMIT ?",  # noqa: S608 - table from TABLES
                (q, pool),
            ).fetchall()
        keyword_ids = {int(r["rowid"]) for r in fts_rows}
        floor = min_sim
        if vec_rows and margin is not None:
            floor = max(floor, 1.0 - float(vec_rows[0]["distance"]) - margin)
        scores: dict[int, float] = {}
        for rank, r in enumerate(vec_rows):
            rid, sim = int(r["rowid"]), 1.0 - float(r["distance"])
            if sim >= floor or rid in keyword_ids:
                scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank)
        for rank, r in enumerate(fts_rows):
            rid = int(r["rowid"])
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank)
        return sorted(scores.items(), key=lambda x: -x[1])[:k]

    # ------------------------------------------------------------------ facts

    def remember(
        self,
        text: str,
        kind: str = "fact",
        subject: str = "user",
        source: str = "you told me",
        created_at: str | None = None,
    ) -> tuple[Fact, bool]:
        """Save a fact; returns (fact, is_new). A near-duplicate replaces the old wording."""
        text = " ".join(text.split())
        if not text:
            raise MemoryError_("Nothing to remember.")
        if len(text) > MAX_FACT_CHARS:
            raise MemoryError_(f"Facts are limited to {MAX_FACT_CHARS} characters.")
        kind = kind if kind in FACT_KINDS else "fact"
        subject = (subject or "user").strip()[:100]
        vector = self.embedder.embed([text])[0]
        created = created_at or now_iso()
        with self._lock:
            near = self._db.execute(
                "SELECT rowid, distance FROM facts_vec WHERE embedding MATCH ? AND k = 1",
                (_vec(vector),),
            ).fetchone()
            self._db.execute("BEGIN")
            try:
                if near is not None and 1.0 - float(near["distance"]) >= DUPLICATE_SIM:
                    fid = int(near["rowid"])
                    self._db.execute(
                        "UPDATE facts SET text=?, kind=?, subject=?, source=?, created_at=? "
                        "WHERE id=?",
                        (text, kind, subject, source, created, fid),
                    )
                    self._unindex("facts", [fid])
                    self._index("facts", fid, text, vector)
                    is_new = False
                else:
                    cur = self._db.execute(
                        "INSERT INTO facts(text, kind, subject, source, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (text, kind, subject, source, created),
                    )
                    fid = int(cur.lastrowid or 0)
                    self._index("facts", fid, text, vector)
                    is_new = True
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        fact = self.fact(fid)
        if fact is None:  # pragma: no cover - the row was written in this transaction
            raise MemoryError_("The fact could not be read back.")
        return fact, is_new

    def fact(self, fid: int) -> Fact | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()
        return _fact(r) if r else None

    def facts(self, limit: int = 500) -> list[Fact]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM facts ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_fact(r) for r in rows]

    def count_facts(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT count(*) FROM facts").fetchone()[0])

    def recall(self, query: str, k: int = 8) -> list[Fact | Episode]:
        """The facts and conversation summaries most relevant to query."""
        vector = self.embedder.embed([query])[0] if query.strip() else None
        with self._lock:
            fact_ids = self._search("facts", query, k, vector)
            ep_ids = self._search("episodes", query, max(1, k // 2), vector)
            merged = sorted(
                [("f", i, s) for i, s in fact_ids] + [("e", i, s) for i, s in ep_ids],
                key=lambda x: -x[2],
            )[:k]
            out: list[Fact | Episode] = []
            used: list[int] = []
            for kind, rid, _ in merged:
                if kind == "f":
                    r = self._db.execute("SELECT * FROM facts WHERE id=?", (rid,)).fetchone()
                    if r:
                        out.append(_fact(r))
                        used.append(rid)
                else:
                    r = self._db.execute("SELECT * FROM episodes WHERE id=?", (rid,)).fetchone()
                    if r:
                        out.append(_episode(r))
            if used:
                stamp = now_iso()
                self._db.executemany(
                    "UPDATE facts SET last_used=? WHERE id=?", [(stamp, i) for i in used]
                )
        return out

    def lookup(self, refs: Sequence[str]) -> list[Fact | Episode]:
        """Facts (f12) and episodes (e3) by reference; unknown refs are skipped."""
        out: list[Fact | Episode] = []
        with self._lock:
            for ref in dict.fromkeys(r.strip().lower() for r in refs):
                m = re.fullmatch(r"([fe])(\d+)", ref)
                if not m:
                    continue
                table = "facts" if m[1] == "f" else "episodes"
                r = self._db.execute(f"SELECT * FROM {table} WHERE id=?", (int(m[2]),)).fetchone()  # noqa: S608 - table from TABLES
                if r:
                    out.append(_fact(r) if table == "facts" else _episode(r))
        return out

    def forget(self, refs: Sequence[str]) -> list[Fact | Episode]:
        """Delete facts/episodes for good (table, keyword and vector index). Returns them."""
        items = self.lookup(refs)
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for item in items:
                    table = "facts" if isinstance(item, Fact) else "episodes"
                    self._db.execute(f"DELETE FROM {table} WHERE id=?", (item.id,))  # noqa: S608 - table from TABLES
                    self._unindex(table, [item.id])
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        if items:
            log.info("memory_forgot", refs=[i.ref for i in items])
        return items

    def clear(self) -> int:
        """Forget every fact and conversation summary (documents stay indexed)."""
        with self._lock:
            ids_f = [int(r[0]) for r in self._db.execute("SELECT id FROM facts")]
            ids_e = [int(r[0]) for r in self._db.execute("SELECT id FROM episodes")]
        return len(self.forget([f"f{i}" for i in ids_f] + [f"e{i}" for i in ids_e]))

    # ------------------------------------------------------------------ episodes

    def add_episode(self, summary: str, session_id: str, created_at: str | None = None) -> Episode:
        summary = " ".join(summary.split())[:2000]
        vector = self.embedder.embed([summary])[0]
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO episodes(text, session_id, created_at) VALUES (?, ?, ?)",
                (summary, session_id, created_at or now_iso()),
            )
            eid = int(cur.lastrowid or 0)
            self._index("episodes", eid, summary, vector)
            r = self._db.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
        return _episode(r)

    def episodes(self, limit: int = 3) -> list[Episode]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM episodes ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_episode(r) for r in rows]

    # ------------------------------------------------------------------ context

    def context_for(self, query: str, all_up_to: int, top_k: int, n_episodes: int) -> str:
        """What to tell the model about the user before a request ('' if nothing)."""
        if self.count_facts() <= all_up_to:
            facts: list[Fact] = self.facts(all_up_to)
        else:
            facts = [h for h in self.recall(query, top_k) if isinstance(h, Fact)]
        eps = self.episodes(n_episodes) if n_episodes else []
        if not facts and not eps:
            return ""
        lines: list[str] = []
        if facts:
            lines.append("Facts about the user (id, saved on):")
            lines += [f"- [{f.ref}] {f.text} ({f.created_at[:10]})" for f in facts]
        if eps:
            lines.append("Recent conversations:")
            lines += [f"- [{e.ref}] {e.created_at[:10]}: {e.summary}" for e in reversed(eps)]
        return "\n".join(lines)

    # ------------------------------------------------------------------ documents

    def add_folder(self, folder: Path) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO folders(path, added_at) VALUES (?, ?)",
                (str(folder), now_iso()),
            )

    def folders(self) -> list[tuple[str, int, int, str | None]]:
        """(folder, documents, chunks, last indexed) per indexed folder."""
        with self._lock:
            rows = self._db.execute(
                """SELECT f.path, f.indexed_at,
                          (SELECT count(*) FROM documents d WHERE d.folder = f.path) AS docs,
                          (SELECT count(*) FROM chunks c JOIN documents d ON c.doc_id = d.id
                           WHERE d.folder = f.path) AS chunks
                   FROM folders f ORDER BY f.path"""
            ).fetchall()
        return [(r["path"], int(r["docs"]), int(r["chunks"]), r["indexed_at"]) for r in rows]

    def remove_folder(self, folder: str) -> int:
        """Stop indexing a folder and delete everything indexed from it."""
        with self._lock:
            docs = [
                int(r[0])
                for r in self._db.execute("SELECT id FROM documents WHERE folder=?", (folder,))
            ]
            self._db.execute("BEGIN")
            try:
                for doc_id in docs:
                    self._delete_doc(doc_id)
                self._db.execute("DELETE FROM folders WHERE path=?", (folder,))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return len(docs)

    def _delete_doc(self, doc_id: int) -> None:
        ids = [
            int(r[0]) for r in self._db.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))
        ]
        self._unindex("chunks", ids)
        self._db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        self._db.execute("DELETE FROM documents WHERE id=?", (doc_id,))

    def index_folder(
        self,
        folder: Path,
        scope: FileScope,
        progress: Callable[[str], None] | None = None,
    ) -> IndexReport:
        """(Re)index one folder: new and changed files are read, deleted ones dropped."""
        report = IndexReport(str(folder))
        self.add_folder(folder)
        seen: set[str] = set()
        for path in scan(folder, scope):
            key = str(path)
            seen.add(key)
            st = path.stat()
            with self._lock:
                old = self._db.execute(
                    "SELECT id, mtime, size FROM documents WHERE path=?", (key,)
                ).fetchone()
            if old is not None and old["mtime"] == st.st_mtime and old["size"] == st.st_size:
                report.unchanged += 1
                continue
            try:
                chunks = chunk(extract(path))
            except ExtractError as e:
                report.errors.append(f"{path.name}: {e}")
                continue
            vectors = self.embedder.embed([c.text for c in chunks]) if chunks else []
            with self._lock:
                self._db.execute("BEGIN")
                try:
                    if old is not None:
                        self._delete_doc(int(old["id"]))
                    cur = self._db.execute(
                        "INSERT INTO documents(folder, path, mtime, size, indexed_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (str(folder), key, st.st_mtime, st.st_size, now_iso()),
                    )
                    doc_id = int(cur.lastrowid or 0)
                    for c, v in zip(chunks, vectors, strict=True):
                        cid = int(
                            self._db.execute(
                                "INSERT INTO chunks(doc_id, page, idx, text) VALUES (?, ?, ?, ?)",
                                (doc_id, c.page, c.index, c.text),
                            ).lastrowid
                            or 0
                        )
                        self._index("chunks", cid, c.text, v)
                    self._db.execute("COMMIT")
                except Exception:
                    self._db.execute("ROLLBACK")
                    raise
            report.chunks += len(chunks)
            if old is None:
                report.added += 1
            else:
                report.updated += 1
            if progress:
                progress(path.name)
        with self._lock:
            gone = [
                int(r["id"])
                for r in self._db.execute(
                    "SELECT id, path FROM documents WHERE folder=?", (str(folder),)
                ).fetchall()
                if r["path"] not in seen
            ]
            for doc_id in gone:
                self._delete_doc(doc_id)
            self._db.execute(
                "UPDATE folders SET indexed_at=? WHERE path=?", (now_iso(), str(folder))
            )
        report.removed = len(gone)
        log.info(
            "folder_indexed",
            added=report.added,
            updated=report.updated,
            removed=report.removed,
            chunks=report.chunks,
            errors=len(report.errors),
        )
        return report

    def search_docs(self, query: str, k: int = 6) -> list[DocHit]:
        with self._lock:
            hits = self._search("chunks", query, k, min_sim=DOC_MIN_SIM, margin=None)
            out: list[DocHit] = []
            for rid, score in hits:
                r = self._db.execute(
                    "SELECT c.text, c.page, d.path FROM chunks c JOIN documents d "
                    "ON c.doc_id = d.id WHERE c.id=?",
                    (rid,),
                ).fetchone()
                if r:
                    out.append(DocHit(r["path"], r["page"], r["text"], score))
        return out


def _check_table(table: str) -> None:
    if table not in TABLES:
        raise ValueError(f"unknown table {table!r}")


def _fact(r: sqlite3.Row) -> Fact:
    return Fact(
        int(r["id"]), r["text"], r["kind"], r["subject"], r["source"], r["created_at"],
        r["last_used"],
    )  # fmt: skip


def _episode(r: sqlite3.Row) -> Episode:
    return Episode(int(r["id"]), r["text"], r["session_id"], r["created_at"])
