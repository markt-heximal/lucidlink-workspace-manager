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

import json
import logging
import re
import threading
import time
from typing import Optional

import httpx
import numpy as np

from .store import Store, blob_to_vec

logger = logging.getLogger("indexer.vectors")

Scope = tuple[str, str]  # (workspace_id, filespace)

DEAD_RATIO_REBUILD = 0.2
UPSERT_BATCH = 256
# ruvector-server (axum) rejects bodies over 2 MB. Stay well under it: a
# 768-dim vector is ~10 KB of JSON at 6 decimals, so this is ~90 points.
UPSERT_MAX_BYTES = 900_000
# After a mirror failure, leave Ruvector alone for this long (search runs
# locally meanwhile) instead of retrying on every request.
MIRROR_COOLDOWN_SEC = 60.0




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
               path_prefix: Optional[str] = None, point_ids: Optional[set[int]] = None) -> list[tuple[int, float]]:
        """point_ids: only these points (a request limited to some files)."""
        with self._lock:
            self._load()
            if not len(self._ids) or self._matrix.shape[1] != len(q):
                return []
            mask = np.ones(len(self._ids), dtype=bool)
            if scopes is not None:
                mask &= np.array([s in scopes for s in self._scopes], dtype=bool)
            if path_prefix and path_prefix != "/":
                mask &= np.array([_under(p, path_prefix) for p in self._paths], dtype=bool)
            if point_ids is not None:
                mask &= np.isin(self._ids, np.fromiter(point_ids, dtype=np.int64, count=len(point_ids)))
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


class PayloadTooLarge(MirrorError):
    pass


def _byte_batches(points: list[dict], max_bytes: int):
    """Split points into runs whose JSON stays under `max_bytes`."""
    batch: list[dict] = []
    size = 0
    for p in points:
        n = len(json.dumps(p, separators=(",", ":"))) + 1
        if batch and size + n > max_bytes:
            yield batch
            batch, size = [], 0
        batch.append(p)
        size += n
    if batch:
        yield batch


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
        if r.status_code == 413:
            raise PayloadTooLarge(f"{method} {path}: {r.text[:200]}")
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
        else:
            self._verify(name)
        self._ready = name

    def _verify(self, name: str) -> None:
        """Rehydrate when the collection exists but no longer holds our points.

        A restart can bring a collection back empty (e.g. a restore script that
        recreates collection definitions), which the 404 check cannot see.
        """
        sample = self.store.mirror_sample(name)
        if sample is None:
            return
        try:
            r = self._req("GET", f"/collections/{name}/points/{sample}")
            held = r.text.strip() not in ("", "null")
        except CollectionMissing:
            held = False
        if not held:
            logger.info("ruvector: %s no longer holds mirrored points", name)
            self._rehydrate(name)

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
                # 6 decimals keeps cosine scores exact to ~1e-6 and halves the JSON.
                "id": str(r["point_id"]),
                "vector": [round(float(x), 6) for x in blob_to_vec(r["vector"])],
                "metadata": {"workspaceId": r["workspace_id"], "filespace": r["filespace"],
                             "fileId": r["file_id"], "path": r["path"], "chunkIndex": r["chunk_index"]},
            } for r in rows]
            for batch in _byte_batches(points, UPSERT_MAX_BYTES):
                self._upsert(name, batch)
                pushed += len(batch)

    def _upsert(self, name: str, points: list[dict]) -> None:
        """PUT points, halving the batch if the server still finds it too large."""
        try:
            self._req("PUT", f"/collections/{name}/points", {"points": points})
        except PayloadTooLarge:
            if len(points) == 1:
                raise MirrorError(f"ruvector rejects even a single point for {name} as too large")
            mid = len(points) // 2
            self._upsert(name, points[:mid])
            self._upsert(name, points[mid:])
            return
        self.store.mirror_add(name, [int(p["id"]) for p in points])

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
            self._verify(name)  # each sync cycle, so a silent restart is caught
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
        self._mirror_retry_at = 0.0  # monotonic time before which the mirror is skipped

    def _mirror_usable(self) -> bool:
        return self.mirror is not None and time.monotonic() >= self._mirror_retry_at

    def _mirror_failed(self, e: Exception, what: str) -> None:
        self._mirror_retry_at = time.monotonic() + MIRROR_COOLDOWN_SEC
        logger.warning("ruvector %s failed, using local for %.0fs: %s", what, MIRROR_COOLDOWN_SEC, e)

    def search(self, q: np.ndarray, k: int, scopes: Optional[set[Scope]] = None,
               path_prefix: Optional[str] = None, point_ids: Optional[set[int]] = None) -> list[tuple[int, float]]:
        # A path filter or a file limit (point_ids) is applied exactly by the
        # local index; the mirror is only an accelerator for the plain case.
        if point_ids is not None:
            if not point_ids:
                return []
            self.last_backend = "local"
            return self.local.search(q, k, scopes, path_prefix, point_ids)
        if self._mirror_usable() and not (path_prefix and path_prefix != "/"):
            try:
                hits = self.mirror.search(q, k, scopes)
                if hits or not self.local.search(q, 1, scopes):
                    self.last_backend = "ruvector"
                    return hits
                # The mirror came back empty while local has matches: it has
                # likely lost its points. Answer locally and re-check it next time.
                logger.warning("ruvector returned no hits but local has some; using local")
                self.mirror._ready = None
            except MirrorError as e:
                self._mirror_failed(e, "search")
        self.last_backend = "local"
        return self.local.search(q, k, scopes, path_prefix)

    def sync_mirror(self) -> None:
        if not self._mirror_usable():
            return
        try:
            self.mirror.sync()
        except MirrorError as e:
            self._mirror_failed(e, "sync")

    def status(self) -> dict:
        if self.mirror is None:
            return {"backend": "local", "ok": True, "points": self.local.count(), "error": None}
        healthy = self.mirror.ok is not False
        return {"backend": "ruvector" if healthy else "local", "ok": healthy,
                "points": self.mirror.points() if healthy else self.local.count(),
                "error": self.mirror.error}
