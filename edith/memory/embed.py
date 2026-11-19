"""Local text embeddings. Nothing is sent anywhere: the model runs in this process.

FastEmbedder loads an ONNX model (bge-small, 384 dimensions) through fastembed. The
model files are downloaded once from Hugging Face into the models dir (a plain file
download, no user data); after that EDITH loads them offline.

HashEmbedder is a deterministic, dependency-free stand-in for tests: words are
hashed into buckets, so texts that share words are similar. It never downloads.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from edith.config import EdithConfig
from edith.observability.logging import get_logger

log = get_logger(__name__)

DIM = 384
_WORD = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Unit-length vectors, one per text (cosine similarity = dot product)."""
        ...


class EmbedError(RuntimeError):
    pass


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


class HashEmbedder:
    """Bag-of-words hashing. Deterministic and offline; for tests only."""

    dim = DIM

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            v = [0.0] * self.dim
            for word in _WORD.findall(text.lower()):
                h = int.from_bytes(hashlib.blake2b(word.encode(), digest_size=4).digest(), "big")
                v[h % self.dim] += 1.0
            out.append(_normalize(v))
        return out


class FastEmbedder:
    """bge-small through fastembed, loaded on first use (about 1 s once cached)."""

    dim = DIM

    def __init__(self, model: str, cache_dir: Path) -> None:
        self.model = model
        self.cache_dir = cache_dir
        self._impl: object | None = None
        self._lock = threading.Lock()

    def _load(self) -> object:
        with self._lock:
            if self._impl is not None:
                return self._impl
            os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
            from fastembed import TextEmbedding

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            try:  # offline first: never touch the network once the model is here
                impl = TextEmbedding(
                    self.model, cache_dir=str(self.cache_dir), local_files_only=True
                )
            except Exception:
                log.info("embed_model_download", model=self.model)
                try:
                    impl = TextEmbedding(self.model, cache_dir=str(self.cache_dir))
                except Exception as e:
                    raise EmbedError(f"Could not load the embedding model {self.model}: {e}") from e
            self._impl = impl
            return impl

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        impl = self._load()
        vectors = impl.embed(list(texts))  # type: ignore[attr-defined]
        return [[float(x) for x in v] for v in vectors]


def get_embedder(cfg: EdithConfig) -> Embedder:
    """The configured local embedder. EDITH_FAKE_EMBED=1 gives HashEmbedder (tests)."""
    if os.environ.get("EDITH_FAKE_EMBED") == "1":
        return HashEmbedder()
    return FastEmbedder(cfg.memory.embed_model, cfg.models_dir)
