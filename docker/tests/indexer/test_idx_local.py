"""The engine computer: drives it reaches get their own workspace, existing ones move there once,
and "All drives" (workspaceId "*") searches every workspace."""
from __future__ import annotations

import json
from contextlib import contextmanager

from fastapi.testclient import TestClient

from conftest import KEY, para, run, src

from indexer.app import create_app

H = {"X-Internal-Key": KEY}


@contextmanager
def client(svc):
    with TestClient(create_app(lambda: svc, start_scheduler=False)) as c:
        yield c


def test_existing_drive_moves_to_local_without_rereading(make_service, tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    (share / "brief.md").write_text(para("harbor brief"))
    old = make_service(block_sources=f"NAS={share}", block_source_workspace="ws1")
    run(old, "NAS", "ws1")
    chunks = old.store.chunk_count("ws1", "NAS")
    points = {r["point_id"] for r in old.store.query("SELECT point_id FROM chunks WHERE filespace='NAS'")}
    old.store.execute("INSERT INTO mirror_points (collection, point_id) SELECT 'c', point_id FROM chunks")
    old.store.set_control(False, None, [{"workspaceId": "ws1", "filespace": "NAS"},
                                        {"workspaceId": "ws1", "filespace": "fsA"}])
    old.store.put_survey("ws1", "NAS", "/", "{}", "2026-10-29T00:00:00Z")

    # The new default on the same database.
    svc = make_service(db_path=old.config.db_path, block_sources=f"NAS={share}", block_source_workspace="")
    assert ("local", "NAS") in svc.scheduler.targets() and ("ws1", "NAS") not in svc.scheduler.targets()
    assert svc.store.chunk_count("local", "NAS") == chunks and svc.store.chunk_count("ws1", "NAS") == 0
    assert {r["point_id"] for r in svc.store.query("SELECT point_id FROM chunks WHERE filespace='NAS'")} == points
    assert svc.store.query("SELECT * FROM mirror_points WHERE point_id IN (%s)" % ",".join(map(str, points))) == []
    assert svc.store.get_survey("local", "NAS", "/") is not None
    prio = svc.store.control()["priority"]
    assert {"workspaceId": "local", "filespace": "NAS"} in prio and {"workspaceId": "ws1", "filespace": "fsA"} in prio
    assert svc.filespace_status("local", "NAS")["kind"] == "block"
    # A rescan finds everything unchanged: nothing re-read.
    run(svc, "NAS", "local")
    assert svc.store.chunk_count("local", "NAS") == chunks
    st = json.loads(json.dumps(svc.status()))
    assert [b["workspaceId"] for b in st["blockSources"]] == ["local"]


def test_all_drives_scope_spans_every_workspace(make_service, tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    (share / "harbor.md").write_text(para("harbor lease"))
    svc = make_service(block_sources=f"NAS={share}", block_source_workspace="")
    src(svc, "fsA", "ws1").write("/fsa.md", para("harbor invoice"))
    run(svc, "fsA", "ws1")
    run(svc, "NAS", "local")
    with client(svc) as c:
        one = c.post("/index/search", json={"query": "harbor"}, headers=H).json()["results"]
        assert {(h["workspaceId"], h["filespace"]) for h in one} == {("ws1", "fsA")}   # the active workspace only
        everything = c.post("/index/search", json={"workspaceId": "*", "query": "harbor"}, headers=H).json()["results"]
        assert {(h["workspaceId"], h["filespace"]) for h in everything} == {("ws1", "fsA"), ("local", "NAS")}
        assert {h["kind"] for h in everything} == {"lucidlink", "block"}
        named = c.post("/index/search", json={"workspaceId": "*", "filespace": "NAS", "query": "harbor"},
                       headers=H).json()["results"]
        assert {h["filespace"] for h in named} == {"NAS"}
        recent = c.get("/index/recent", params={"workspaceId": "*"}, headers=H).json()["files"]
        assert {(f["workspaceId"], f["filespace"]) for f in recent} == {("ws1", "fsA"), ("local", "NAS")}


def test_engine_name_labels_its_drives(make_service, tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    (share / "a.md").write_text("x")
    svc = make_service(block_sources=f"NAS={share}", block_source_workspace="", engine_name="AI-Factory-Mini")
    with client(svc) as c:
        assert c.get("/index/folders", headers=H).json()["engine"] == {"name": "AI-Factory-Mini", "workspaceId": "local"}
        assert c.get("/index/status", headers=H).json()["engine"]["name"] == "AI-Factory-Mini"
    unnamed = make_service(block_sources=f"NAS={share}", block_source_workspace="")
    assert unnamed.engine_info() == {"name": None, "workspaceId": "local"}
