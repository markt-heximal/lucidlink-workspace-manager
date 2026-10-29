"""Engine console feed: recent indexer log lines, after a sequence number, credentials masked."""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from conftest import KEY, para, run, src

from indexer import activity
from indexer.app import create_app

H = {"X-Internal-Key": KEY}


def test_ring_keeps_indexer_lines_and_warnings_only():
    ring = activity.ActivityLog(size=10)
    log = logging.getLogger("indexer.worker")
    for name, level, msg in [("indexer.worker", logging.INFO, "[local/NAS] scan 3: seen=12"),
                             ("httpx", logging.INFO, "HTTP Request: GET https://x"),
                             ("uvicorn.access", logging.WARNING, "GET /index/activity 200"),
                             ("indexer.vectors", logging.WARNING, "Ruvector upsert failed"),
                             ("somelib", logging.ERROR, "boom")]:
        ring.handle(log.makeRecord(name, level, __file__, 1, msg, (), None))
    out = ring.read()
    assert [(e["source"], e["level"], e["text"]) for e in out["events"]] == [
        ("worker", "info", "[local/NAS] scan 3: seen=12"), ("vectors", "warning", "Ruvector upsert failed"),
        ("somelib", "error", "boom")]
    assert out["dropped"] is False and out["last"] == out["events"][-1]["seq"]


def test_after_limit_and_dropped():
    ring = activity.ActivityLog(size=10)
    log = logging.getLogger("indexer.t")
    for i in range(25):
        ring.handle(log.makeRecord("indexer.t", logging.INFO, __file__, 1, f"line {i}", (), None))
    assert [e["text"] for e in ring.read(after=22)["events"]] == ["line 22", "line 23", "line 24"]
    page = ring.read(after=3, limit=2)
    assert page["dropped"] is True and [e["text"] for e in page["events"]] == ["line 15", "line 16"]
    assert page["last"] == 17
    assert ring.read(after=25) == {"events": [], "last": 25, "dropped": False}


def test_credentials_are_masked():
    assert activity.mask("Authorization: Bearer abc.def.ghi") == "Authorization: Bearer ••••"
    assert activity.mask("token sa_live:XYZ123 used") == "token sa_live:•••• used"
    assert activity.mask("url?api_key=s3cr3t&x=1") == "url?api_key=••••&x=1"


def test_activity_endpoint(make_service, caplog):
    caplog.set_level(logging.INFO, logger="indexer")   # production runs basicConfig(level=INFO)
    svc = make_service()
    with TestClient(create_app(lambda: svc, start_scheduler=False)) as c:
        logging.getLogger("indexer.worker").info("[ws1/fsA] scan 9: seen=4")
        body = c.get("/index/activity", headers=H).json()
        assert any(e["text"] == "[ws1/fsA] scan 9: seen=4" for e in body["events"])
        after = c.get("/index/activity", params={"after": body["last"]}, headers=H).json()
        assert after["events"] == [] and after["last"] == body["last"]
        assert c.get("/index/activity").status_code == 401
        assert c.get("/index/activity", params={"limit": 0}, headers=H).status_code == 422


def test_each_file_gets_a_console_line(make_service, caplog):
    caplog.set_level(logging.INFO, logger="indexer")
    ring = activity.install()
    before = ring.read()["last"]
    svc = make_service()
    src(svc).write("/notes/a.md", para("alpha"))
    src(svc).write("/tiny.md", "x")
    run(svc)
    texts = [e["text"] for e in ring.read(after=before, limit=500)["events"] if e["source"] == "files"]
    assert any(t.startswith("[ws1/fsA] indexed /notes/a.md (") and "chunks)" in t for t in texts)
    assert any(t.startswith("[ws1/fsA] skipped /tiny.md: too little text") for t in texts)
