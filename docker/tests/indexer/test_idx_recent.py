"""GET /index/recent: auth, change kinds, ordering, scope and the limit cap."""
from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from conftest import KEY, para, run, src

from indexer.app import create_app
from indexer.service import RECENT_MAX
from indexer.store import Store

H = {"X-Internal-Key": KEY}


@contextmanager
def client(svc):
    with TestClient(create_app(lambda: svc, start_scheduler=False)) as c:
        yield c


def test_recent_requires_internal_key(make_service):
    with client(make_service()) as c:
        assert c.get("/index/recent").status_code == 401
        assert c.get("/index/recent", headers={"X-Internal-Key": "wrong"}).status_code == 401
        assert c.get("/index/recent", headers=H).status_code == 200
    with client(make_service(internal_key="")) as c:
        assert c.get("/index/recent", headers={"X-Internal-Key": "x"}).status_code == 503


def test_first_crawl_has_no_change_kind_and_orders_by_mtime(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/old.md", para("old"))
    s.write("/mid.md", para("mid"))
    s.write("/new.md", para("new"))
    run(svc)
    with client(svc) as c:
        body = c.get("/index/recent", params={"filespace": "fsA"}, headers=H).json()
    files = body["files"]
    assert [f["path"] for f in files] == ["/new.md", "/mid.md", "/old.md"]
    assert all(f["change"] is None for f in files)
    f = files[0]
    assert set(f) >= {"workspaceId", "filespace", "path", "size", "mtime", "status", "indexedAt", "change",
                      "changedAt", "previousPath"}
    assert f["workspaceId"] == "ws1" and f["filespace"] == "fsA" and f["status"] == "indexed"
    assert f["mtime"].endswith("Z") and f["indexedAt"]


def test_change_kinds_after_first_sync(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/keep.md", para("keep"))
    s.write("/edit.md", para("edit"))
    s.write("/gone.md", para("gone"))
    s.write("/move.md", para("move"))
    run(svc)
    s.write("/fresh.md", para("fresh"))
    s.write("/edit.md", para("edited text"))
    s.delete("/gone.md")
    s.move("/move.md", "/moved/move.md")
    run(svc)
    with client(svc) as c:
        files = c.get("/index/recent", params={"filespace": "fsA"}, headers=H).json()["files"]
    by_path = {f["path"]: f for f in files}
    assert by_path["/fresh.md"]["change"] == "new"
    assert by_path["/edit.md"]["change"] == "modified"
    assert by_path["/gone.md"]["change"] == "deleted" and by_path["/gone.md"]["status"] == "deleted"
    assert by_path["/moved/move.md"]["change"] == "renamed"
    assert by_path["/moved/move.md"]["previousPath"] == "/move.md"
    assert by_path["/keep.md"]["change"] is None
    # Deletions and renames sort by when they were seen: they are among the newest.
    assert {f["path"] for f in files[:4]} >= {"/gone.md", "/moved/move.md"}
    assert all(f["changedAt"] for f in files if f["change"])


def test_limit_and_cap(make_service):
    svc = make_service()
    s = src(svc)
    for i in range(30):
        s.write(f"/f{i:02d}.txt", para(f"topic{i}", 2))
    run(svc)
    with client(svc) as c:
        assert len(c.get("/index/recent", params={"limit": 5}, headers=H).json()["files"]) == 5
        assert len(c.get("/index/recent", headers=H).json()["files"]) == 20
        big = c.get("/index/recent", params={"limit": 10_000}, headers=H)
        assert big.status_code == 200 and len(big.json()["files"]) == 30
        assert c.get("/index/recent", params={"limit": 0}, headers=H).status_code == 422
        assert svc.recent(None, None, 10_000) == svc.recent(None, None, RECENT_MAX)


def test_scope_all_enabled_filespaces_or_one(make_service):
    svc = make_service()
    src(svc).write("/a.md", para("alpha"))
    src(svc, "fsB").write("/b.md", para("beta"))
    run(svc)
    run(svc, "fsB")
    with client(svc) as c:
        everywhere = c.get("/index/recent", headers=H).json()["files"]
        only_b = c.get("/index/recent", params={"filespace": "fsB"}, headers=H).json()["files"]
        c.put("/index/filespaces", json={"workspaceId": "ws1", "filespace": "fsB", "enabled": False}, headers=H)
        enabled_only = c.get("/index/recent", headers=H).json()["files"]
    assert {(f["filespace"], f["path"]) for f in everywhere} == {("fsA", "/a.md"), ("fsB", "/b.md")}
    assert [f["path"] for f in only_b] == ["/b.md"]
    assert [f["filespace"] for f in enabled_only] == ["fsA"]


def test_change_of_rules():
    row = {"status": "indexed", "path": "/a", "mtime": 5}
    assert Store.change_of(row, {"status": "deleted"}) == ("deleted", None)
    assert Store.change_of({**row, "status": "deleted"}, {"status": "pending"}) == ("new", None)
    assert Store.change_of(row, {"path": "/b"}) == ("renamed", "/a")
    assert Store.change_of(row, {"mtime": 6, "status": "pending"}) == ("modified", None)
    assert Store.change_of(row, {"mtime": 6, "path": "/b"}) == ("modified", "/a")
    assert Store.change_of(row, {"mtime": 5, "scan_id": 2}) == (None, None)
    assert Store.change_of(row, {"status": "indexed"}) == (None, None)


def test_recent_leaves_out_future_dated_files(make_service):
    import time as _t
    svc = make_service()
    src(svc).write("/old.md", para("old"))
    run(svc)
    now = int(_t.time())
    ws, fs = "ws1", "fsA"
    fid = svc.store.one("SELECT file_id FROM files WHERE path='/old.md'")["file_id"]
    svc.store.execute("UPDATE files SET mtime=? WHERE file_id=?", ((now - 3600) * 10**9, fid))
    for i, mtime in enumerate([(now + 10 * 365 * 86400) * 10**9, now + 10 * 365 * 86400, (now + 400 * 86400) * 1000]):
        svc.store.insert_file(ws, fs, f"future{i}", f"/future{i}.jpg", 10, mtime, 1, status="skipped")
    paths = [f["path"] for f in svc.recent("ws1", "fsA", 5)]
    assert paths == ["/old.md"]
