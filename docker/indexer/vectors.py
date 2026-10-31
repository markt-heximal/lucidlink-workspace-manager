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
# Mirror bookkeeping (live/dead counts, what is still unmirrored) is a scan of
# every chunk: ~10 s each at 1.6 M chunks. Search never runs it (the worker
# pushes after every indexing job); counts are cached, and above this many
# points they are refreshed only by sync or after COUNTS_TTL_SEC.
COUNTS_EXACT_MAX = 100_000
COUNTS_TTL_SEC = 600.0




def model_slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (model or "model").lower()).strip("_") or "model"


def _clamp(x: float) -> float:
    return float(min(1.0, max(0.0, x)))


class LocalIndex:
    """Exact search. Holds every chunk vector in memory while there are at most
    `max_points` of them; above that (a million chunks of 768 floats is 3 GB)
    every search streams the vectors from SQLite in batches instead. A search
    limited to some points (a collection) always streams just those."""

    def __init__(self, store: Store, model: str, max_points: int = 250_000):
        self.store = store
        self.model = model
        self.max_points = max_points
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

    @property
    def streaming(self) -> bool:
        return self.store.chunk_total() > self.max_points

    def count(self) -> int:
        if self.streaming:
            return self.store.chunk_total()
        with self._lock:
            self._load()
            return len(self._ids)

    def search(self, q: np.ndarray, k: int, scopes: Optional[set[Scope]] = None,
               path_prefix: Optional[str] = None, point_ids: Optional[set[int]] = None) -> list[tuple[int, float]]:
        """point_ids: only these points (a request limited to some files)."""
        if point_ids is not None or self.streaming:
            return self._scan(q, k, scopes, path_prefix, point_ids)
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

    def _scan(self, q: np.ndarray, k: int, scopes: Optional[set[Scope]], path_prefix: Optional[str],
              point_ids: Optional[set[int]]) -> list[tuple[int, float]]:
        """The same search, streamed from SQLite: memory stays one batch."""
        if point_ids is not None and not point_ids:
            return []
        q = np.asarray(q, dtype=np.float32)
        best_ids = np.zeros(0, dtype=np.int64)
        best = np.zeros(0, dtype=np.float32)
        for ids, mat in self.store.iter_chunk_vectors(self.model, scopes, path_prefix, point_ids):
            if mat.shape[1] != len(q):
                continue
            best_ids = np.concatenate([best_ids, ids])
            best = np.concatenate([best, mat @ q])
            if len(best) > k:
                top = np.argpartition(-best, k - 1)[:k]
                best_ids, best = best_ids[top], best[top]
        order = np.lexsort((best_ids, -best))
        return [(int(best_ids[i]), _clamp(best[i])) for i in order[:k]]


class FileIndex:
    """Meaning per file, for Related files: the file vectors (store.file_vectors)
    in one float16 matrix (1.5 KB per file at 768 dimensions). Loaded once, then
    only rows written since (`seq`), so indexing never forces a full reload."""

    def __init__(self, store: Store, model: str):
        self.store = store
        self.model = model
        self._lock = threading.Lock()
        self._seq = 0
        self._row: dict[tuple[str, str, str], int] = {}
        self._keys: list[tuple[str, str, str]] = []
        self._scope_of: dict[Scope, int] = {}
        self._scopes = np.zeros(0, dtype=np.int32)
        self._valid = np.zeros(0, dtype=bool)
        self._mat: Optional[np.ndarray] = None

    def _grow(self, n: int, dim: int) -> None:
        cap = 0 if self._mat is None else len(self._mat)
        if n <= cap:
            return
        new = max(n, cap * 2, 1024)
        mat = np.zeros((new, dim), dtype=np.float16)
        if self._mat is not None:
            mat[:cap] = self._mat
        self._mat = mat
        self._scopes = np.concatenate([self._scopes, np.zeros(new - cap, dtype=np.int32)])
        self._valid = np.concatenate([self._valid, np.zeros(new - cap, dtype=bool)])

    def refresh(self) -> None:
        with self._lock:
            for rows in self.store.iter_file_vectors(self.model, self._seq):
                for ws, fs, fid, seq, blob in rows:
                    self._seq = max(self._seq, seq)
                    key = (ws, fs, fid)
                    i = self._row.get(key)
                    if blob is None:
                        if i is not None:
                            self._valid[i] = False
                        continue
                    v = blob_to_vec(blob)
                    if self._mat is not None and self._mat.shape[1] != len(v):
                        continue          # another dimension under the same model name
                    if i is None:
                        i = len(self._keys)
                        self._keys.append(key)
                        self._row[key] = i
                        self._grow(i + 1, len(v))
                    self._mat[i] = v
                    self._scopes[i] = self._scope_of.setdefault((ws, fs), len(self._scope_of))
                    self._valid[i] = True

    def count(self) -> int:
        self.refresh()
        with self._lock:
            return int(self._valid[:len(self._keys)].sum())

    def vectors(self, keys: list[tuple[str, str, str]]) -> tuple[list[int], np.ndarray]:
        """(positions in `keys` that have a vector, their vectors as a float16 copy)."""
        self.refresh()
        with self._lock:
            have = [(i, self._row[k]) for i, k in enumerate(keys)
                    if k in self._row and self._valid[self._row[k]]]
            if not have or self._mat is None:
                return [], np.zeros((0, 0), dtype=np.float16)
            return [i for i, _ in have], self._mat[np.array([r for _, r in have], dtype=np.int64)]

    def search(self, q: np.ndarray, k: int, scopes: Optional[set[Scope]] = None,
               block: int = 20000) -> list[tuple[tuple[str, str, str], float]]:
        """[((ws, fs, file_id), similarity)], best first."""
        self.refresh()
        with self._lock:
            n = len(self._keys)
            if not n or self._mat is None or self._mat.shape[1] != len(q):
                return []
            mask = self._valid[:n].copy()
            if scopes is not None:
                codes = [self._scope_of[s] for s in scopes if s in self._scope_of]
                mask &= np.isin(self._scopes[:n], np.array(codes, dtype=np.int32))
            q = np.asarray(q, dtype=np.float32)
            sims = np.full(n, -np.inf, dtype=np.float32)
            for i in range(0, n, block):
                part = mask[i:i + block]
                if part.any():
                    sims[i:i + block][part] = self._mat[i:min(i + block, n)][part].astype(np.float32) @ q
            idx = np.nonzero(mask)[0]
            if not len(idx):
                return []
            kk = min(k, len(idx))
            top = idx[np.argpartition(-sims[idx], kk - 1)[:kk]]
            top = top[np.argsort(-sims[top], kind="stable")]
            return [(self._keys[i], _clamp(sims[i])) for i in top]


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
        # (collection, monotonic time, store generation, live, dead)
        self._counts_cache: Optional[tuple[str, float, int, int, int]] = None

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
        self._counts_cache = None
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
            # A full scan on a large index: on a private read connection, so
            # searches and indexing never wait behind it for the shared lock.
            with self.store.read_view():
                rows = self.store.mirror_missing(name, self.model, UPSERT_BATCH)
            if not rows:
                c = self._counts_cache
                if pushed and c is not None and c[0] == name:
                    self._counts_cache = (name, c[1], c[2], c[3] + pushed, c[4])
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
            live, dead = self._counts(name, refresh=True)
            if dead and dead > DEAD_RATIO_REBUILD * max(live, 1):
                self._rebuild(name)
                return 0
            return self._push_missing(name)
        return self._run(op) or 0

    def _counts(self, name: str, refresh: bool = False, compute: bool = True,
                max_age: Optional[float] = COUNTS_TTL_SEC) -> Optional[tuple[int, int]]:
        """(live, dead) mirrored points. Cached: a small index is re-counted on
        any change, a large one only on refresh (sync) or after max_age (None:
        never). compute=False never scans: the cached value, or None."""
        c = self._counts_cache
        now = time.monotonic()
        if not refresh and c is not None and c[0] == name:
            stale = ((c[2] != self.store.generation and c[3] + c[4] < COUNTS_EXACT_MAX)
                     or (max_age is not None and now - c[1] > max_age))
            if not stale or not compute:
                return c[3], c[4]
        if not compute:
            return None
        gen = self.store.generation
        with self.store.read_view():
            live, dead = self.store.mirror_counts(name)
        self._counts_cache = (name, now, gen, live, dead)
        return live, dead

    def search(self, q: np.ndarray, k: int, scopes: Optional[set[Scope]] = None) -> list[tuple[int, float]]:
        # Nothing here scans SQLite: the worker pushes new points after every
        # indexing job (sync), so search only asks the server. Dead points are
        # over-fetched by the last known dead count and dropped below.
        small = self.store.chunk_total() < COUNTS_EXACT_MAX

        def op(name):
            counts = self._counts(name, compute=small)
            dead = counts[1] if counts else 0
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

        name = self.collection
        if name is not None and self._ready == name:
            # Verified collection: ask without the mirror lock, so a search
            # never waits behind a sync that is pushing points.
            try:
                hits = op(name)
                self.ok, self.error = True, None
            except CollectionMissing:
                self._ready = None
                hits = self._run(op) or []   # heals (rehydrates) under the lock
            except MirrorError as e:
                self.ok, self.error = False, str(e)
                self._ready = None
                raise
        else:
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
        # Status: a large index shows the count from the last sync (which keeps
        # it current) rather than re-scanning on every poll.
        return self._counts(name, max_age=None)[0] if name else 0


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
                # (A cheap "is anything indexed here" stands in for a local search,
                # which on a large index would stream every vector.)
                if hits or not (self.store.has_chunks(scopes) if scopes is not None else self.store.chunk_total()):
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
