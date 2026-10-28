"""Vector search: exact local search over SQLite, optionally mirrored to Ruvector.

`LocalIndex` is always available and always correct: it loads every live chunk
vector from index.db into one numpy matrix (reloaded when the store's
generation changes) and does a brute-force dot product. At personal scale —
tens of thousands of chunks — that is milliseconds.

`RuvectorMirror` keeps a copy in a Ruvector collection to accelerate search.
The stock server keeps collections in memory and cannot delete points, so the
mirror is treated as disposable:

* `mirror_points` in SQLite records what was upserted; a sync upserts whatever
  live chunk is not recorded, which also covers renames (their rows are
  forgotten so the new path is re-upserted under the same id).
* Search results are filtered against live point ids, so dead points (deleted
  or re-chunked text) never surface.
* A missing collection (Ruvector restarted) is recreated and rehydrated from
  SQLite vectors — no re-embedding. When dead points exceed 20% of live ones the
  collection is dropped and rebuilt the same way.
* Any Ruvector failure falls back to local search for that request and is
  recorded for /index/status.

Scores are similarities in [0, 1]. Ruvector's cosine `score` is a distance, so
similarity = 1 - score; locally it is the dot product of unit vectors. Both are
clamped to [0, 1].
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Optional

import httpx
import numpy as np

from .store import Store, blob_to_vec

logger = logging.getLogger("indexer.vectors")

Scope = tuple[str, str]  # (workspace_id, filespace)

DEAD_RATIO_REBUILD = 0.2
UPSERT_BATCH = 256


def model_slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (model or "model").lower()).strip("_") or "model"


def _clamp(x: float) -> float:
    return float(min(1.0, max(0.0, x)))


class LocalIndex:
    def __init__(self, store: Store, model: str):
        self.store = store
        self.model = model
        self._lock = threading.Lock()
        self._gen = -1
        self._ids = np.zeros(0, dtype=np.int64)
        self._scopes: list[Scope] = []
        self._paths: list[str] = []
        self._matrix = np.zeros((0, 0), dtype=np.float32)

    def _load(self) -> None:
        gen = self.store.generation
        if gen == self._gen:
            return
        rows = self.store.live_vectors(self.model)
        vecs = [blob_to_vec(r["vector"]) for r in rows]
        if vecs:
            # Guard against a mixed-dimension cache (model swapped under one name).
            dim = max(set(len(v) for v in vecs), key=[len(v) for v in vecs].count)
            keep = [i for i, v in enumerate(vecs) if len(v) == dim]
            rows = [rows[i] for i in keep]
            self._matrix = np.stack([vecs[i] for i in keep]) if keep else np.zeros((0, dim), np.float32)
        else:
            self._matrix = np.zeros((0, 0), dtype=np.float32)
        self._ids = np.array([r["point_id"] for r in rows], dtype=np.int64)
        self._scopes = [(r["workspace_id"], r["filespace"]) for r in rows]
        self._paths = [r["path"] for r in rows]
        self._gen = gen

    def count(self) -> int:
        with self._lock:
            self._load()
            return len(self._ids)

    def search(self, q: np.ndarray, k: int, scopes: Optional[set[Scope]] = None,
               path_prefix: Optional[str] = None) -> list[tuple[int, float]]:
        with self._lock:
            self._load()
            if not len(self._ids) or self._matrix.shape[1] != len(q):
                return []
            mask = np.ones(len(self._ids), dtype=bool)
            if scopes is not None:
                mask &= np.array([s in scopes for s in self._scopes], dtype=bool)
            if path_prefix and path_prefix != "/":
                mask &= np.array([_under(p, path_prefix) for p in self._paths], dtype=bool)
            idx = np.nonzero(mask)[0]
            if not len(idx):
                return []
            sims = self._matrix[idx] @ np.asarray(q, dtype=np.float32)
            kk = min(k, len(idx))
            top = np.argpartition(-sims, kk - 1)[:kk]
            top = top[np.argsort(-sims[top], kind="stable")]
            return [(int(self._ids[idx[i]]), _clamp(sims[i])) for i in top]


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


class MirrorError(Exception):
    pass


class CollectionMissing(MirrorError):
    pass


class RuvectorMirror:
    def __init__(self, base_url: str, api_key: str, store: Store, model: str, dim_getter,
                 transport: Optional[httpx.BaseTransport] = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.store = store
        self.model = model
        self._dim_getter = dim_getter
        self._transport = transport
        self.timeout = timeout
        self._lock = threading.RLock()
        self._ready: Optional[str] = None   # collection verified to exist this process
        self.ok: Optional[bool] = None
        self.error: Optional[str] = None
        self.rebuilds = 0
        self.rehydrations = 0

    @property
    def collection(self) -> Optional[str]:
        dim = self._dim_getter()
        return f"ll_{model_slug(self.model)}_{dim}" if dim else None

    # ------------------------------------------------------------ http
    def _req(self, method: str, path: str, body: Optional[dict] = None) -> httpx.Response:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport) as c:
                r = c.request(method, f"{self.base_url}{path}", json=body, headers=headers)
        except httpx.HTTPError as e:
            raise MirrorError(f"ruvector unreachable: {type(e).__name__}: {e}") from e
        if r.status_code == 404 or "CollectionNotFound" in r.text[:500]:
            raise CollectionMissing(f"{method} {path}: {r.status_code} {r.text[:200]}")
        if r.status_code >= 400:
            raise MirrorError(f"ruvector {method} {path}: HTTP {r.status_code} {r.text[:200]}")
        return r

    def _create(self, name: str) -> None:
        try:
            self._req("POST", "/collections", {"name": name, "dimension": self._dim_getter(),
                                               "metric": "Cosine"})
        except MirrorError as e:
            if "exist" not in str(e).lower():  # already there is fine
                raise

    # ------------------------------------------------------------ lifecycle
    def _ensure(self, name: str) -> None:
        if self._ready == name:
            return
        try:
            self._req("GET", f"/collections/{name}")
        except CollectionMissing:
            self._rehydrate(name)
        self._ready = name

    def _rehydrate(self, name: str) -> None:
        """Recreate the collection and refill it from SQLite (no re-embedding)."""
        logger.info("ruvector: (re)creating collection %s and rehydrating", name)
        self.rehydrations += 1
        self.store.mirror_clear(name)
        self._create(name)
        self._ready = name
        self._push_missing(name)

    def _rebuild(self, name: str) -> None:
        logger.info("ruvector: rebuilding %s (dead points over %.0f%%)", name, DEAD_RATIO_REBUILD * 100)
        self.rebuilds += 1
        try:
            self._req("DELETE", f"/collections/{name}")
        except CollectionMissing:
            pass
        self._rehydrate(name)

    def _push_missing(self, name: str) -> int:
        pushed = 0
        while True:
            rows = self.store.mirror_missing(name, self.model, UPSERT_BATCH)
            if not rows:
                return pushed
            points = [{
                "id": str(r["point_id"]),
                "vector": blob_to_vec(r["vector"]).tolist(),
                "metadata": {"workspaceId": r["workspace_id"], "filespace": r["filespace"],
                             "fileId": r["file_id"], "path": r["path"], "chunkIndex": r["chunk_index"]},
            } for r in rows]
            self._req("PUT", f"/collections/{name}/points", {"points": points})
            self.store.mirror_add(name, [r["point_id"] for r in rows])
            pushed += len(rows)

    def _run(self, fn):
        """Run `fn(name)`, healing a vanished collection once; record health."""
        with self._lock:
            name = self.collection
            if name is None:
                return None  # nothing embedded yet, so nothing to mirror
            try:
                try:
                    self._ensure(name)
                    result = fn(name)
                except CollectionMissing:
                    self._ready = None
                    self._rehydrate(name)
                    result = fn(name)
            except MirrorError as e:
                self.ok, self.error = False, str(e)
                self._ready = None
                raise
            self.ok, self.error = True, None
            return result

    # ------------------------------------------------------------ public
    def sync(self) -> int:
        """Bring the collection up to date with SQLite; rebuild if too many dead."""
        def op(name):
            live, dead = self.store.mirror_counts(name)
            if dead and dead > DEAD_RATIO_REBUILD * max(live, 1):
                self._rebuild(name)
                return 0
            return self._push_missing(name)
        return self._run(op) or 0

    def search(self, q: np.ndarray, k: int, scopes: Optional[set[Scope]] = None) -> list[tuple[int, float]]:
        def op(name):
            self._push_missing(name)
            live, dead = self.store.mirror_counts(name)
            body: dict = {"vector": np.asarray(q, dtype=np.float32).tolist(),
                          "k": int(min(2000, k * 2 + dead))}
            if scopes is not None and len(scopes) == 1:
                ws, fs = next(iter(scopes))
                body["filter"] = {"workspaceId": ws, "filespace": fs}
            r = self._req("POST", f"/collections/{name}/points/search", body)
            hits = []
            for item in (r.json() or {}).get("results") or []:
                try:
                    hits.append((int(item["id"]), _clamp(1.0 - float(item["score"]))))
                except (KeyError, TypeError, ValueError):
                    continue
            return hits

        hits = self._run(op) or []
        # Drop dead points and anything outside the requested scope.
        rows = self.store.chunk_rows([pid for pid, _ in hits])
        out = []
        for pid, score in hits:
            row = rows.get(pid)
            if row is None:
                continue
            if scopes is not None and (row["workspace_id"], row["filespace"]) not in scopes:
                continue
            out.append((pid, score))
        out.sort(key=lambda h: -h[1])
        return out[:k]

    def points(self) -> int:
        name = self.collection
        return self.store.mirror_counts(name)[0] if name else 0


class VectorService:
    """Search facade: Ruvector when configured and healthy, else local."""

    def __init__(self, store: Store, local: LocalIndex, mirror: Optional[RuvectorMirror] = None):
        self.store = store
        self.local = local
        self.mirror = mirror
        self.last_backend = "ruvector" if mirror else "local"

    def search(self, q: np.ndarray, k: int, scopes: Optional[set[Scope]] = None,
               path_prefix: Optional[str] = None) -> list[tuple[int, float]]:
        # A path filter is applied exactly by the local index; the mirror is
        # only an accelerator for the plain case.
        if self.mirror is not None and not (path_prefix and path_prefix != "/"):
            try:
                hits = self.mirror.search(q, k, scopes)
                self.last_backend = "ruvector"
                return hits
            except MirrorError as e:
                logger.warning("ruvector search failed, using local: %s", e)
        self.last_backend = "local"
        return self.local.search(q, k, scopes, path_prefix)

    def sync_mirror(self) -> None:
        if self.mirror is None:
            return
        try:
            self.mirror.sync()
        except MirrorError as e:
            logger.warning("ruvector sync failed: %s", e)

    def status(self) -> dict:
        if self.mirror is None:
            return {"backend": "local", "ok": True, "points": self.local.count(), "error": None}
        healthy = self.mirror.ok is not False
        return {"backend": "ruvector" if healthy else "local", "ok": healthy,
                "points": self.mirror.points() if healthy else self.local.count(),
                "error": self.mirror.error}
