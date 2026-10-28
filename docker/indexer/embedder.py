"""Embeddings via an OpenAI-compatible `/embeddings` endpoint, with a cache.

Every vector is cached in index.db by `(model, sha256(text))`, so re-indexing a
file whose paragraphs mostly did not change only pays for the chunks that did,
and a renamed or touched file pays nothing. Vectors are L2-normalised once here
so cosine similarity everywhere else is a plain dot product.

The dimension is not configured: it is learned from the first response (or
from the cache) and names the Ruvector collection.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from typing import Optional

import httpx
import numpy as np

from .store import Store

logger = logging.getLogger("indexer.embedder")


class EmbedError(Exception):
    pass


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalise(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    norms = np.linalg.norm(m, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


class Embedder:
    def __init__(self, base_url: str, model: str, api_key: str = "", store: Optional[Store] = None,
                 batch: int = 32, timeout: float = 120.0,
                 transport: Optional[httpx.BaseTransport] = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.store = store
        self.batch = max(1, batch)
        self.timeout = timeout
        self._transport = transport
        self._dim: Optional[int] = None
        self._lock = threading.Lock()
        self.ok: Optional[bool] = None
        self.error: Optional[str] = None
        # Counters, for status and for tests asserting no re-embedding.
        self.requests = 0
        self.texts_embedded = 0

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def dim(self) -> Optional[int]:
        if self._dim is None and self.store is not None and self.model:
            self._dim = self.store.model_dim(self.model)
        return self._dim

    def status(self) -> dict:
        if not self.configured:
            return {"model": self.model or None, "dim": self.dim, "ok": False,
                    "error": "EMBED_BASE_URL / EMBED_MODEL not set"}
        return {"model": self.model, "dim": self.dim, "ok": self.ok is not False, "error": self.error}

    # ------------------------------------------------------------ transport
    def _request(self, texts: list[str]) -> list[list[float]]:
        """One POST /embeddings call. Overridden by the fake embedder in tests."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
            r = client.post(f"{self.base_url}/embeddings", json={"model": self.model, "input": texts},
                            headers=headers)
        if r.status_code >= 400:
            raise EmbedError(f"embeddings HTTP {r.status_code}: {r.text[:200]}")
        data = r.json().get("data") or []
        if len(data) != len(texts):
            raise EmbedError(f"embeddings returned {len(data)} vectors for {len(texts)} inputs")
        return [d["embedding"] for d in sorted(data, key=lambda d: d.get("index", 0))]

    def _embed_uncached(self, texts: list[str]) -> np.ndarray:
        if not self.configured:
            raise EmbedError("embedder not configured (EMBED_BASE_URL / EMBED_MODEL)")
        out = []
        try:
            for i in range(0, len(texts), self.batch):
                part = texts[i:i + self.batch]
                vecs = normalise(np.asarray(self._request(part), dtype=np.float32))
                if vecs.ndim != 2 or vecs.shape[0] != len(part):
                    raise EmbedError("malformed embeddings response")
                with self._lock:
                    if self.dim is None:
                        self._dim = int(vecs.shape[1])
                    elif vecs.shape[1] != self._dim:
                        raise EmbedError(f"dimension changed: {vecs.shape[1]} != {self._dim}")
                    self.requests += 1
                    self.texts_embedded += len(part)
                out.append(vecs)
        except EmbedError as e:
            self.ok, self.error = False, str(e)
            raise
        except Exception as e:  # transport, JSON or shape errors: all mean "embedder down"
            self.ok, self.error = False, f"{type(e).__name__}: {e}"
            raise EmbedError(self.error) from e
        self.ok, self.error = True, None
        return np.concatenate(out) if out else np.zeros((0, self.dim or 0), dtype=np.float32)

    # ------------------------------------------------------------ public
    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """Vectors for `texts` (rows in order), embedding only cache misses."""
        if not texts:
            return np.zeros((0, self.dim or 0), dtype=np.float32)
        hashes = [text_hash(t) for t in texts]
        cached = self.store.get_embeddings(self.model, hashes) if self.store else {}
        missing: dict[str, str] = {}
        for h, t in zip(hashes, texts):
            if h not in cached and h not in missing:
                missing[h] = t
        if missing:
            vecs = self._embed_uncached(list(missing.values()))
            fresh = list(zip(missing.keys(), vecs))
            if self.store:
                self.store.put_embeddings(self.model, fresh)
            cached.update(fresh)
        return np.stack([cached[h] for h in hashes])

    def embed_query(self, text: str) -> np.ndarray:
        """Embed a search query. Not cached: queries are one-off."""
        return self._embed_uncached([text])[0]
