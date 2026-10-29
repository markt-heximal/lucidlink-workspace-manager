"""This Mini: drives the Mini reaches get their own workspace, existing ones move there once,
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


def test_existing_drive_moves_to_the_mini_without_rereading(make_service, tmp_path):
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
    assert ("mini", "NAS") in svc.scheduler.targets() and ("ws1", "NAS") not in svc.scheduler.targets()
    assert svc.store.chunk_count("mini", "NAS") == chunks and svc.store.chunk_count("ws1", "NAS") == 0
    assert {r["point_id"] for r in svc.store.query("SELECT point_id FROM chunks WHERE filespace='NAS'")} == points
    assert svc.store.query("SELECT * FROM mirror_points WHERE point_id IN (%s)" % ",".join(map(str, points))) == []
    assert svc.store.get_survey("mini", "NAS", "/") is not None
    prio = svc.store.control()["priority"]
    assert {"workspaceId": "mini", "filespace": "NAS"} in prio and {"workspaceId": "ws1", "filespace": "fsA"} in prio
    assert svc.filespace_status("mini", "NAS")["kind"] == "block"
    # A rescan finds everything unchanged: nothing re-read.
    run(svc, "NAS", "mini")
    assert svc.store.chunk_count("mini", "NAS") == chunks
    st = json.loads(json.dumps(svc.status()))
    assert [b["workspaceId"] for b in st["blockSources"]] == ["mini"]


def test_all_drives_scope_spans_every_workspace(make_service, tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    (share / "harbor.md").write_text(para("harbor lease"))
    svc = make_service(block_sources=f"NAS={share}", block_source_workspace="")
    src(svc, "fsA", "ws1").write("/fsa.md", para("harbor invoice"))
    run(svc, "fsA", "ws1")
    run(svc, "NAS", "mini")
    with client(svc) as c:
        one = c.post("/index/search", json={"query": "harbor"}, headers=H).json()["results"]
        assert {(h["workspaceId"], h["filespace"]) for h in one} == {("ws1", "fsA")}   # the active workspace only
        everything = c.post("/index/search", json={"workspaceId": "*", "query": "harbor"}, headers=H).json()["results"]
        assert {(h["workspaceId"], h["filespace"]) for h in everything} == {("ws1", "fsA"), ("mini", "NAS")}
        assert {h["kind"] for h in everything} == {"lucidlink", "block"}
        named = c.post("/index/search", json={"workspaceId": "*", "filespace": "NAS", "query": "harbor"},
                       headers=H).json()["results"]
        assert {h["filespace"] for h in named} == {"NAS"}
        recent = c.get("/index/recent", params={"workspaceId": "*"}, headers=H).json()["files"]
        assert {(f["workspaceId"], f["filespace"]) for f in recent} == {("ws1", "fsA"), ("mini", "NAS")}
