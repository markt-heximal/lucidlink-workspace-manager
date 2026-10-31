"""Indexing controls: files by status, retry, pause and priority."""
from __future__ import annotations

import time
from contextlib import contextmanager

from fastapi.testclient import TestClient

from conftest import KEY, files, para, run, src

from indexer.app import create_app

H = {"X-Internal-Key": KEY}


@contextmanager
def client(svc):
    """A client whose shutdown leaves the service open (the fixture closes it),
    so a test can keep using the service between requests."""
    stop, svc.stop = svc.stop, lambda: None
    try:
        with TestClient(create_app(lambda: svc, start_scheduler=False)) as c:
            yield c
    finally:
        svc.stop = stop


class FakePool:
    """Records what dispatch submits instead of running it."""

    def __init__(self):
        self.submitted: list = []

    def submit(self, fn, key):
        self.submitted.append(key)


def dispatch(svc) -> list:
    pool = FakePool()
    svc.scheduler._pool = pool
    svc.scheduler._dispatch()
    for key in pool.submitted:            # as if each job had returned
        svc.scheduler._runtime[key].running = False
    svc.scheduler._pool = None
    return pool.submitted


def mixed(svc):
    """fsA with indexed, skipped (two reasons) and failed files."""
    s = src(svc)
    for i in range(3):
        s.write(f"/docs/n{i}.md", para(f"note{i}"))
    for i in range(3):
        s.write(f"/media/v{i}.mov", b"\x00" * 10)
    s.write("/media/pic.png", b"\x00" * 10)
    for i in range(4):
        s.write(f"/bad/Report{i}.md", para(f"bad{i}"))
    s.fail_reads = {f"/bad/Report{i}.md" for i in range(4)}
    run(svc)
    return s


# ------------------------------------------------------------ auth
def test_internal_key_enforced_on_control_routes(make_service):
    with client(make_service()) as c:
        for method, url, kw in (("get", "/index/files", {"params": {"filespace": "fsA", "status": "failed"}}),
                                ("post", "/index/retry", {"json": {"filespace": "fsA", "all": True}}),
                                ("get", "/index/control", {}),
                                ("put", "/index/control", {"json": {"paused": True}})):
            assert getattr(c, method)(url, **kw).status_code == 401
            assert getattr(c, method)(url, headers={"X-Internal-Key": "wrong"}, **kw).status_code == 401
            assert getattr(c, method)(url, headers=H, **kw).status_code == 200
    with client(make_service(internal_key="")) as c:
        assert c.get("/index/control", headers=H).status_code == 503


# ------------------------------------------------------------ /index/files
def test_files_shape_and_by_reason(make_service):
    svc = make_service()
    mixed(svc)
    with client(svc) as c:
        body = c.get("/index/files", params={"filespace": "fsA", "status": "skipped"}, headers=H).json()
        assert set(body) == {"files", "total", "byReason"}
        assert body["total"] == 4
        f = body["files"][0]
        assert set(f) == {"path", "size", "mtime", "status", "reason", "indexedAt", "changedAt", "kind"}
        assert f["status"] == "skipped" and f["kind"] == "lucidlink" and f["mtime"].endswith("Z")
        assert body["byReason"] == [{"reason": "unsupported type (.mov)", "files": 3},
                                    {"reason": "unsupported type (.png)", "files": 1}]

        # byReason covers the whole status, not just the page.
        page = c.get("/index/files", params={"filespace": "fsA", "status": "skipped", "limit": 1},
                     headers=H).json()
        assert len(page["files"]) == 1 and page["total"] == 4 and page["byReason"] == body["byReason"]

        failed = c.get("/index/files", params={"filespace": "fsA", "status": "failed"}, headers=H).json()
        assert failed["total"] == 4 and sum(r["files"] for r in failed["byReason"]) == 4
        assert all("read failed" in r["reason"] for r in failed["byReason"])

        idx = c.get("/index/files", params={"filespace": "fsA", "status": "indexed"}, headers=H).json()
        assert idx["total"] == 3 and idx["byReason"] == []
        assert all(f["indexedAt"] for f in idx["files"])

        assert c.get("/index/files", params={"filespace": "fsA", "status": "deleted"},
                     headers=H).status_code == 422
        assert c.get("/index/files", params={"status": "failed"}, headers=H).status_code == 422


def test_files_pagination_filter_and_order(make_service):
    svc = make_service(job_max_files=0)
    s = src(svc)
    for i in range(7):
        s.write(f"/q/File{i}.md", para(f"x{i}"), mtime=1_700_000_000 + i)
    s.write("/other.md", para("other"), mtime=1_600_000_000)
    run(svc)          # job_max_files=0: everything stays pending
    with client(svc) as c:
        def get(**params):
            return c.get("/index/files", params={"filespace": "fsA", "status": "pending", **params},
                         headers=H).json()

        everything = get()
        assert everything["total"] == 8
        # Queue order: newest mtime first, same as the indexer's work queue.
        assert [f["path"] for f in everything["files"]] == [r["path"] for r in svc.store.pending_files("ws1", "fsA")]
        assert everything["files"][0]["path"] == "/q/File6.md"
        assert everything["files"][-1]["path"] == "/other.md"

        p1, p2 = get(limit=3), get(limit=3, offset=3)
        assert [f["path"] for f in p1["files"] + p2["files"]] == [f["path"] for f in everything["files"][:6]]
        assert p1["total"] == p2["total"] == 8
        assert get(limit=1000)["files"] == everything["files"]     # capped at 200, still all 8

        q = get(q="file")                                           # case-insensitive substring
        assert q["total"] == 7 and all("/q/" in f["path"] for f in q["files"])
        assert get(q="%")["total"] == 0                             # LIKE wildcards are literal
        assert get(q="_")["total"] == 0


def test_files_order_failed_and_indexed(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/old.md", para("old"), mtime=1_600_000_000)
    s.write("/new.md", para("new"), mtime=1_700_000_000)
    s.fail_reads = {"/old.md", "/new.md"}
    run(svc)
    with client(svc) as c:
        failed = c.get("/index/files", params={"filespace": "fsA", "status": "failed"}, headers=H).json()
        assert [f["path"] for f in failed["files"]] == ["/new.md", "/old.md"]
    svc.store.execute("UPDATE files SET status='indexed', indexed_at=? WHERE path='/old.md'",
                      ("2026-02-01T00:00:00Z",))
    svc.store.execute("UPDATE files SET status='indexed', indexed_at=? WHERE path='/new.md'",
                      ("2026-01-01T00:00:00Z",))
    with client(svc) as c:
        idx = c.get("/index/files", params={"filespace": "fsA", "status": "indexed"}, headers=H).json()
        assert [f["path"] for f in idx["files"]] == ["/old.md", "/new.md"]   # newest indexedAt first


def test_files_workspace_header_default(make_service):
    svc = make_service()
    mixed(svc)
    with client(svc) as c:
        params = {"filespace": "fsA", "status": "failed"}
        assert c.get("/index/files", params=params, headers={**H, "X-Workspace-Id": "wsX"}).json()["total"] == 0
        assert c.get("/index/files", params=params, headers={**H, "X-Workspace-Id": "ws1"}).json()["total"] == 4
        assert c.get("/index/files", params={**params, "workspaceId": "ws1"},
                     headers={**H, "X-Workspace-Id": "wsX"}).json()["total"] == 4


# ------------------------------------------------------------ /index/retry
def test_retry_named_failed_files(make_service):
    svc = make_service()
    s = mixed(svc)
    rt = svc.scheduler.runtime("ws1", "fsA")
    rt.full_requested = False
    with client(svc) as c:
        r = c.post("/index/retry", json={"filespace": "fsA",
                                         "paths": ["/bad/Report0.md", "/docs/n0.md", "/media/v0.mov", "/nope"]},
                   headers=H)
        assert r.status_code == 200 and r.json() == {"requeued": 1}   # only the failed one
    led = files(svc)
    assert led["/bad/Report0.md"]["status"] == "pending"
    assert led["/bad/Report0.md"]["indexed_at"] is None               # back-off cleared
    assert led["/bad/Report1.md"]["status"] == "failed"
    assert led["/docs/n0.md"]["status"] == "indexed"
    assert led["/media/v0.mov"]["status"] == "skipped"                # skipped files are never retried
    assert rt.full_requested is True                                  # sync requested

    s.fail_reads = set()
    run(svc, full=False)
    assert files(svc)["/bad/Report0.md"]["status"] == "indexed"


def test_retry_ignores_non_failed_and_does_not_sync(make_service):
    svc = make_service()
    mixed(svc)
    rt = svc.scheduler.runtime("ws1", "fsA")
    rt.full_requested = False
    assert svc.retry("ws1", "fsA", ["/docs/n0.md", "/media/v0.mov"]) == 0
    assert svc.retry("ws1", "fsA") == 0
    assert rt.full_requested is False
    assert files(svc)["/docs/n0.md"]["status"] == "indexed"


def test_retry_all_bypasses_back_off(make_service):
    svc = make_service()
    s = mixed(svc)
    s.fail_reads = set()
    run(svc)          # back-off (failed_retry_sec) not over: a rescan leaves them failed
    assert sum(f["status"] == "failed" for f in files(svc).values()) == 4
    rt = svc.scheduler.runtime("ws1", "fsA")
    rt.full_requested = False
    with client(svc) as c:
        assert c.post("/index/retry", json={"filespace": "fsA", "all": True}, headers=H).json() == {"requeued": 4}
    assert rt.full_requested is True
    assert all(f["indexed_at"] is None for f in files(svc).values() if f["status"] == "pending")
    run(svc, full=False)
    led = files(svc)
    assert all(led[f"/bad/Report{i}.md"]["status"] == "indexed" for i in range(4))
    assert led["/media/v0.mov"]["status"] == "skipped"


# ------------------------------------------------------------ /index/control
def test_control_shape_and_validation(make_service):
    svc = make_service()
    with client(svc) as c:
        assert c.get("/index/control", headers=H).json() == {"paused": False, "pausedAt": None, "priority": []}
        body = c.put("/index/control", json={"priority": [
            {"workspaceId": "ws1", "filespace": "fsB"},
            {"workspaceId": "ws1", "filespace": "nope"},
            {"workspaceId": "wsX", "filespace": "fsA"},
            {"workspaceId": "ws1", "filespace": "fsB"},
            {"workspaceId": "ws1", "filespace": "fsA"}]}, headers=H).json()
        assert body["priority"] == [{"workspaceId": "ws1", "filespace": "fsB"},
                                    {"workspaceId": "ws1", "filespace": "fsA"}]
        assert body["paused"] is False and body["pausedAt"] is None

        paused = c.put("/index/control", json={"paused": True}, headers=H).json()
        assert paused["paused"] is True and paused["pausedAt"].endswith("Z")
        assert paused["priority"] == body["priority"]                 # untouched when omitted
        again = c.put("/index/control", json={"paused": True}, headers=H).json()
        assert again["pausedAt"] == paused["pausedAt"]                # pausing twice keeps the time
        assert c.get("/index/control", headers=H).json() == again

        resumed = c.put("/index/control", json={"paused": False, "priority": []}, headers=H).json()
        assert resumed == {"paused": False, "pausedAt": None, "priority": []}
        assert c.put("/index/control", json={"priority": [{"filespace": "fsA"}]}, headers=H).status_code == 422


def test_pause_blocks_dispatch_not_queries(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/a.md", para("alpha garden"))
    run(svc)
    svc.set_control(paused=True)
    svc.sync(None, None)
    assert dispatch(svc) == []                                       # nothing new starts

    # Hints accumulate while paused.
    svc.hint("ws1", "fsA", "write", "/h1.md")
    svc.hint("ws1", "fsA", "write", "/h2.md")
    svc.scheduler.runtime("ws1", "fsA").full_requested = False
    svc.hint("ws1", "fsA", "write", "/h3.md")
    assert [h["path"] for h in svc.scheduler.runtime("ws1", "fsA").hints] == ["/h3.md"]
    svc.hint("ws1", "fsA", "write", "/h4.md")
    assert len(svc.scheduler.runtime("ws1", "fsA").hints) == 2
    assert dispatch(svc) == []

    with client(svc) as c:
        status = c.get("/index/status", headers=H).json()
        assert status["paused"] is True
        assert {f["state"] for f in status["filespaces"]} == {"paused"}
        res = c.post("/index/search", json={"query": "alpha garden"}, headers=H).json()["results"]
        assert res and res[0]["path"] == "/a.md"
        assert c.get("/index/files", params={"filespace": "fsA", "status": "indexed"},
                     headers=H).json()["total"] == 1

    # A running job keeps its state while paused.
    rt = svc.scheduler.runtime("ws1", "fsA")
    rt.running, rt.phase = True, "indexing"
    assert svc.filespace_status("ws1", "fsA")["state"] == "indexing"
    rt.running, rt.phase = False, None

    svc.set_control(paused=False)
    # Same workspace (one SDK session): they take turns rather than one
    # holding a slot while it waits for the other's session.
    sch, pool = svc.scheduler, FakePool()
    sch._pool = pool
    sch._dispatch()
    assert len(pool.submitted) == 1
    sch._dispatch()
    assert len(pool.submitted) == 1                  # the other waits: its session is busy
    sch._runtime[pool.submitted[0]].running = False  # first job returns
    sch._runtime[pool.submitted[0]].last_run_end = time.monotonic()
    sch._dispatch()
    assert sorted(pool.submitted) == [("ws1", "fsA"), ("ws1", "fsB")]
    for key in pool.submitted:
        sch._runtime[key].running = False
    sch._pool = None
    assert svc.status()["paused"] is False
    assert svc.filespace_status("ws1", "fsA")["state"] == "idle"


def test_pause_and_priority_survive_restart(make_service, tmp_path):
    db = str(tmp_path / "shared.db")
    first = make_service(db_path=db)
    first.set_control(paused=True, priority=[{"workspaceId": "ws1", "filespace": "fsB"}])
    at = first.control()["pausedAt"]
    first.scheduler.stop()
    first.store.close()

    second = make_service(db_path=db)
    assert second.control() == {"paused": True, "pausedAt": at,
                                "priority": [{"workspaceId": "ws1", "filespace": "fsB"}]}
    second.sync(None, None)
    assert dispatch(second) == []


def test_priority_before_round_robin(make_service):
    svc = make_service(concurrency=1)
    sched = svc.scheduler
    # Without priority, the least recently run goes first: fsB ran long ago, fsA never.
    sched.runtime("ws1", "fsB", create=True).last_run_end = 5.0
    svc.sync(None, None)
    assert dispatch(svc) == [("ws1", "fsA")]

    svc.set_control(priority=[{"workspaceId": "ws1", "filespace": "fsB"}])
    svc.sync(None, None)
    assert dispatch(svc) == [("ws1", "fsB")]

    # A priority source with nothing to do does not hold the slot.
    sched.runtime("ws1", "fsB").full_requested = False
    sched.runtime("ws1", "fsB").next_scan_at = 1e12
    assert dispatch(svc) == [("ws1", "fsA")]


def test_status_priority_positions(make_service):
    svc = make_service()
    svc.set_control(priority=[{"workspaceId": "ws1", "filespace": "fsB"},
                              {"workspaceId": "ws1", "filespace": "fsA"}])
    with client(svc) as c:
        body = c.get("/index/status", headers=H).json()
    assert body["paused"] is False
    assert {f["filespace"]: f["priority"] for f in body["filespaces"]} == {"fsA": 1, "fsB": 0}
    svc.set_control(priority=[{"workspaceId": "ws1", "filespace": "fsA"}])
    assert {f["filespace"]: f["priority"] for f in svc.status()["filespaces"]} == {"fsA": 0, "fsB": None}


def test_by_reason_top_20_largest_first(make_service):
    svc = make_service()
    s = src(svc)
    for i in range(25):
        for j in range(i + 1):
            s.write(f"/t/f{i}_{j}.x{i:02d}", b"\x00")
    run(svc)
    body = svc.files("ws1", "fsA", "skipped", limit=1)
    assert body["total"] == sum(range(1, 26))
    assert len(body["byReason"]) == 20
    assert body["byReason"][0] == {"reason": "unsupported type (.x24)", "files": 25}
    assert [r["files"] for r in body["byReason"]] == list(range(25, 5, -1))
