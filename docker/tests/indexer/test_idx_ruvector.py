"""Ruvector mirror against a fake in-memory ruvector-server (httpx.MockTransport)."""
from __future__ import annotations

import json

import httpx
import numpy as np
import pytest

from conftest import files, para, run, src


class FakeRuvector:
    """Mimics stock ruvector-server: in-memory collections, no point delete,
    cosine search returning a *distance* as `score`."""

    def __init__(self):
        self.collections: dict[str, dict] = {}
        self.fail = False
        self.requests: list[tuple[str, str]] = []
        self.transport = httpx.MockTransport(self.handle)

    def restart(self):
        self.collections.clear()

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append((req.method, req.url.path))
        if self.fail:
            return httpx.Response(500, text="internal error")
        parts = req.url.path.strip("/").split("/")
        body = json.loads(req.content) if req.content else {}
        if parts == ["collections"] and req.method == "POST":
            assert body["metric"] == "Cosine"
            self.collections[body["name"]] = {"dim": body["dimension"], "points": {}}
            return httpx.Response(200, json={"name": body["name"]})
        name = parts[1]
        col = self.collections.get(name)
        if col is None:
            return httpx.Response(404, json={"error": f"CollectionNotFound: {name}"})
        if len(parts) == 2:
            if req.method == "DELETE":
                del self.collections[name]
            return httpx.Response(200, json={"name": name, "count": len(col["points"])})
        if parts[2:] == ["points"] and req.method == "PUT":
            for p in body["points"]:
                assert len(p["vector"]) == col["dim"]
                col["points"][p["id"]] = (np.array(p["vector"], dtype=np.float32), p["metadata"])
            return httpx.Response(200, json={"ids": [p["id"] for p in body["points"]]})
        if parts[2:] == ["points", "search"]:
            q = np.array(body["vector"], dtype=np.float32)
            flt = body.get("filter") or {}
            scored = []
            for pid, (v, meta) in col["points"].items():
                if any(meta.get(k) != val for k, val in flt.items()):
                    continue
                cos = float(v @ q / (np.linalg.norm(v) * np.linalg.norm(q)))
                scored.append({"id": pid, "score": 1.0 - cos, "metadata": meta})
            scored.sort(key=lambda r: r["score"])
            return httpx.Response(200, json={"results": scored[: body["k"]]})
        return httpx.Response(405)


@pytest.fixture
def ruv():
    return FakeRuvector()


def _index(svc, docs: dict[str, str], fs="fsA"):
    s = src(svc, fs)
    for p, t in docs.items():
        s.write(p, t)
    run(svc, fs)
    return s


def test_upsert_and_search_converts_distance_to_similarity(make_service, ruv):
    svc = make_service(ruvector=ruv.transport)
    _index(svc, {"/alpha.md": para("alpha"), "/beta.md": para("beta"), "/gamma.md": para("gamma")})
    name = svc.vectors.mirror.collection
    assert name == "ll_fake_embed_64"
    assert len(ruv.collections[name]["points"]) == svc.store.chunk_count()
    meta = next(iter(ruv.collections[name]["points"].values()))[1]
    assert set(meta) == {"workspaceId", "filespace", "fileId", "path", "chunkIndex"}

    hits = svc.search("ws1", "fsA", "alpha detail", k=3)
    assert svc.vectors.last_backend == "ruvector"
    assert hits[0]["path"] == "/alpha.md"
    assert all(0.0 <= h["score"] <= 1.0 for h in hits)
    assert hits == sorted(hits, key=lambda h: -h["score"])

    # Same similarity as the authoritative local index.
    q = svc.embedder.embed_query("alpha detail")
    local = dict(svc.vectors.local.search(q, 3, {("ws1", "fsA")}))
    remote = dict(svc.vectors.mirror.search(q, 3, {("ws1", "fsA")}))
    for pid, score in remote.items():
        assert score == pytest.approx(local[pid], abs=1e-4)
    assert svc.status()["vectorStore"] == {"backend": "ruvector", "ok": True,
                                           "points": svc.store.chunk_count(), "error": None}


def test_dead_points_are_filtered(make_service, ruv):
    svc = make_service(ruvector=ruv.transport)
    docs = {f"/f{i}.md": para(f"filler{i}") for i in range(5)}
    s = _index(svc, {"/a.md": para("orchid"), **docs})
    name = svc.vectors.mirror.collection
    old_ids = set(ruv.collections[name]["points"])

    s.write("/a.md", para("cactus"))   # old orchid chunk becomes a dead point
    run(svc)
    assert set(ruv.collections[name]["points"]) > old_ids   # server could not delete it
    assert svc.vectors.mirror.rebuilds == 0                  # 1 dead of 6 live is under 20%

    q = svc.embedder.embed_query(para("orchid"))
    raw = sorted(ruv.collections[name]["points"].items(),
                 key=lambda kv: -float(kv[1][0] @ q))
    dead_top = raw[0][0]
    assert int(dead_top) not in svc.store.live_point_ids([int(dead_top)])   # server's best is dead
    hits = svc.vectors.mirror.search(q, 5, {("ws1", "fsA")})
    live = svc.store.live_point_ids([pid for pid, _ in hits])
    assert hits and all(pid in live for pid, _ in hits)
    assert int(dead_top) not in {pid for pid, _ in hits}


def test_rehydrate_after_restart_without_reembedding(make_service, ruv):
    svc = make_service(ruvector=ruv.transport)
    _index(svc, {"/a.md": para("alpha"), "/b.md": para("beta")})
    embedded = svc.embedder.texts_embedded
    ruv.restart()                                   # ruvector lost its in-memory collection

    hits = svc.search("ws1", "fsA", "beta detail")
    assert hits[0]["path"] == "/b.md"
    assert svc.vectors.last_backend == "ruvector"
    assert svc.vectors.mirror.rehydrations >= 1
    name = svc.vectors.mirror.collection
    assert len(ruv.collections[name]["points"]) == svc.store.chunk_count()
    # Only the query was embedded; chunk vectors came from SQLite.
    assert svc.embedder.texts_embedded == embedded + 1


def test_rename_reupserts_metadata(make_service, ruv):
    svc = make_service(ruvector=ruv.transport)
    s = _index(svc, {"/a.md": para("alpha")})
    s.move("/a.md", "/renamed.md")
    run(svc)
    name = svc.vectors.mirror.collection
    assert {m["path"] for _, m in ruv.collections[name]["points"].values()} == {"/renamed.md"}


def test_fallback_to_local_on_error(make_service, ruv):
    svc = make_service(ruvector=ruv.transport)
    _index(svc, {"/a.md": para("alpha"), "/b.md": para("beta")})
    ruv.fail = True

    hits = svc.search("ws1", "fsA", "alpha detail")
    assert hits[0]["path"] == "/a.md"
    assert svc.vectors.last_backend == "local"
    vs = svc.status()["vectorStore"]
    assert vs["ok"] is False and vs["backend"] == "local" and "500" in vs["error"]
    assert vs["points"] == svc.store.chunk_count()

    ruv.fail = False
    svc.search("ws1", "fsA", "alpha detail")
    assert svc.status()["vectorStore"]["ok"] is True


def test_rebuild_when_dead_exceeds_twenty_percent(make_service, ruv):
    svc = make_service(ruvector=ruv.transport)
    s = _index(svc, {f"/d{i}.md": para(f"doc{i}") for i in range(5)})
    name = svc.vectors.mirror.collection
    for i in range(3):
        s.write(f"/d{i}.md", para(f"changed{i}"))
    run(svc)   # sync pushes new points; next sync sees >20% dead and rebuilds
    svc.vectors.sync_mirror()
    assert svc.vectors.mirror.rebuilds >= 1
    assert ("DELETE", f"/collections/{name}") in ruv.requests
    live, dead = svc.store.mirror_counts(name)
    assert dead == 0 and live == svc.store.chunk_count()
    assert set(map(int, ruv.collections[name]["points"])) == {
        r["point_id"] for r in svc.store.query("SELECT point_id FROM chunks")}


def test_cross_filespace_search_with_mirror(make_service, ruv):
    svc = make_service(ruvector=ruv.transport)
    _index(svc, {"/a.md": para("apple")}, fs="fsA")
    _index(svc, {"/b.md": para("banana")}, fs="fsB")
    hits = svc.search("ws1", None, "banana detail", k=5)
    assert {h["filespace"] for h in hits} == {"fsA", "fsB"}
    assert hits[0]["filespace"] == "fsB"
    assert files(svc, "fsB")["/b.md"]["status"] == "indexed"
