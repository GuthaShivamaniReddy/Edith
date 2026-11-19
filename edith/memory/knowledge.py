"""Read your documents into citable chunks (PDF, DOCX, Markdown, text).

Only folders you add with `edith index add` are read, only when you run it (no
background scanning), and only inside files.allowed_roots minus blocked_roots.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from edith.safety.scope import FileScope, ScopeError

EXTENSIONS = frozenset({".pdf", ".docx", ".md", ".markdown", ".txt"})
MAX_FILE_BYTES = 25_000_000
MAX_FILES = 5_000
CHUNK_CHARS = 900
OVERLAP_CHARS = 150


class ExtractError(RuntimeError):
    pass


@dataclass(frozen=True)
class Page:
    text: str
    page: int | None = None  # 1-based, PDFs only


@dataclass(frozen=True)
class Chunk:
    text: str
    page: int | None
    index: int  # position in the file


def extract(path: Path) -> list[Page]:
    """The text of one document, page by page for PDFs."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            if reader.is_encrypted:
                raise ExtractError("the PDF is encrypted")
            return [Page(p.extract_text() or "", i) for i, p in enumerate(reader.pages, 1)]
        if suffix == ".docx":
            import docx

            d = docx.Document(str(path))
            parts = [p.text for p in d.paragraphs]
            for table in d.tables:
                for row in table.rows:
                    parts.append(" | ".join(c.text for c in row.cells))
            return [Page("\n".join(parts))]
        if suffix in EXTENSIONS:
            return [Page(path.read_text(encoding="utf-8", errors="replace"))]
    except ExtractError:
        raise
    except Exception as e:
        raise ExtractError(f"{type(e).__name__}: {e}") from e
    raise ExtractError(f"unsupported file type {suffix}")


def chunk(pages: list[Page], size: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS) -> list[Chunk]:
    """Split on paragraph breaks into ~size-char chunks that overlap a little."""
    out: list[Chunk] = []
    for page in pages:
        text = re.sub(r"[ \t]+", " ", page.text).strip()
        if not text:
            continue
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        buf = ""
        for para in paragraphs:
            while len(para) > size:  # one huge paragraph: hard split
                head, para = para[:size], para[size - overlap :]
                if buf:
                    out.append(Chunk(buf, page.page, len(out)))
                    buf = ""
                out.append(Chunk(head, page.page, len(out)))
            if buf and len(buf) + len(para) + 2 > size:
                out.append(Chunk(buf, page.page, len(out)))
                buf = buf[-overlap:].split(" ", 1)[-1] + "\n\n" + para if overlap else para
            else:
                buf = f"{buf}\n\n{para}" if buf else para
        if buf:
            out.append(Chunk(buf, page.page, len(out)))
    return out


def scan(folder: Path, scope: FileScope) -> Iterator[Path]:
    """Indexable files under folder, skipping hidden and blocked paths."""
    count = 0
    for path in sorted(folder.rglob("*")):
        if count >= MAX_FILES:
            return
        rel = path.relative_to(folder)
        if any(part.startswith((".", "~$")) for part in rel.parts):
            continue  # hidden folders, Office lock files
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            continue
        try:
            scope.resolve(str(path))  # follows symlinks; refuses blocked or outside roots
        except ScopeError:
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        count += 1
        yield path


def check_folder(folder: str, scope: FileScope) -> Path:
    """A folder you may index: inside an allowed root and not blocked."""
    try:
        path = scope.resolve(folder)
    except ScopeError as e:
        raise ExtractError(str(e)) from e
    if not path.is_dir():
        raise ExtractError(f"{path} is not a folder.")
    return path


def citation(path: str, page: int | None) -> str:
    return f"{path} (page {page})" if page else path
