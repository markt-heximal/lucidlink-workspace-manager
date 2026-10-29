"""Simplification phase 2: sizes, kinds across locations and topics everywhere."""
from __future__ import annotations

import json
import time
from contextlib import contextmanager

from fastapi.testclient import TestClient

from conftest import KEY, para, run, src

from indexer.app import create_app

H = {"X-Internal-Key": KEY}


@contextmanager
def client(svc):
    with TestClient(create_app(lambda: svc, start_scheduler=False)) as c:
        yield c


def _with_nas(make_service, tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    (share / "plan.md").write_text(para("plan"))
    (share / "board.KEY").write_bytes(b"not really keynote")
    svc = make_service(block_sources=f"NAS={share}", block_source_workspace="")
    src(svc).write("/a.md", para("alpha"))
    src(svc).write("/b.md", para("beta"))
    run(svc)
    svc.scheduler.run_job("local", "NAS", full=True)
    return svc


def test_status_totals(make_service, tmp_path):
    svc = _with_nas(make_service, tmp_path)
    svc.store.insert_file("ws1", "fsA", "gone", "/gone.md", 999, 1, 1, status="deleted")
    expected = {k: {"files": 0, "bytes": 0} for k in (("ws1", "fsA"), ("ws1", "fsB"), ("local", "NAS"))}
    for r in svc.store.query("SELECT workspace_id, filespace, size FROM files WHERE status != 'deleted'"):
        e = expected[(r["workspace_id"], r["filespace"])]
        e["files"] += 1
        e["bytes"] += r["size"]
    with client(svc) as c:
        st = c.get("/index/status", headers=H).json()
        # The single-source path (PUT /index/filespaces) computes its own.
        single = svc.filespace_status("ws1", "fsA")["totals"]
    got = {(f["workspaceId"], f["filespace"]): f["totals"] for f in st["filespaces"]}
    assert got[("ws1", "fsA")] == expected[("ws1", "fsA")] and got[("ws1", "fsA")]["files"] == 2
    assert got[("local", "NAS")] == expected[("local", "NAS")] and got[("local", "NAS")]["files"] == 2
    assert got[("ws1", "fsB")] == {"files": 0, "bytes": 0}
    assert single == expected[("ws1", "fsA")]


def test_by_kind_across_locations(make_service, tmp_path):
    svc = _with_nas(make_service, tmp_path)
    now = int(time.time())
    ins = svc.store.insert_file
    ins("ws1", "fsA", "r", "/Reports/Q3.PDF", 10, (now - 100) * 10**9, 1, status="skipped")      # ns, upper case
    ins("ws1", "fsA", "s", "/budget.xlsx", 20, (now - 50) * 1000, 1, status="pending")           # ms
    ins("ws1", "fsA", "f", "/future.docx", 30, now + 400 * 86400, 1, status="skipped")           # wrong clock
    ins("ws1", "fsA", "d", "/old.pdf", 40, now - 10, 1, status="deleted")                        # not live
    ins("ws1", "fsA", "p", "/cover.psd", 50, now - 5, 1, status="skipped")                       # design
    ins("ws1", "fsA", "v", "/clip.mov", 60, now - 5, 1, status="skipped")
    ins("ws1", "fsA", "x", "/tool.py", 70, now - 1, 1, status="skipped")                         # code: no kind
    with client(svc) as c:
        get = lambda **p: c.get("/index/bykind", params={"workspaceId": "*", **p}, headers=H)
        docs = get(kind="documents").json()
        assert docs["kind"] == "documents"
        paths = [f["path"] for f in docs["files"]]
        assert paths[-1] == "/future.docx"                     # future-dated last
        assert "/old.pdf" not in paths and "/tool.py" not in paths and "/cover.psd" not in paths
        assert set(paths) == {"/a.md", "/b.md", "/plan.md", "/Reports/Q3.PDF", "/budget.xlsx", "/future.docx"}
        assert docs["total"] == 6
        q3 = next(f for f in docs["files"] if f["path"] == "/Reports/Q3.PDF")
        assert q3["type"] == "pdf" and q3["name"] == "Q3.PDF" and q3["status"] == "skipped"
        assert q3["location"] == {"kind": "lucidlink", "root": None, "provider": None}
        assert q3["mtime"].endswith("Z") and q3["size"] == 10
        assert paths.index("/budget.xlsx") < paths.index("/Reports/Q3.PDF")     # newest first, mixed units
        nas = next(f for f in docs["files"] if f["path"] == "/plan.md")
        assert nas["workspaceId"] == "local" and nas["location"]["kind"] == "block"

        page = get(kind="documents", limit=2, offset=1).json()
        assert page["files"] == docs["files"][1:3] and page["total"] == 6

        pres = get(kind="presentations").json()
        assert [(f["path"], f["type"]) for f in pres["files"]] == [("/board.KEY", "presentation")]
        assert {f["type"] for f in get(kind="images").json()["files"]} == {"design"}
        assert [f["path"] for f in get(kind="video").json()["files"]] == ["/clip.mov"]

        # Default scope: the active workspace only.
        assert {f["workspaceId"] for f in c.get("/index/bykind", params={"kind": "documents"},
                                                headers=H).json()["files"]} == {"ws1"}
        assert get(kind="documents", filespace="NAS").json()["total"] == 1
        assert get(kind="music").status_code == 400
        assert get().status_code == 400
        assert len(get(kind="documents", limit=9999).json()["files"]) == 6     # capped, not refused


def test_surveys_everywhere(make_service, tmp_path):
    svc = _with_nas(make_service, tmp_path)
    topic = {"name": "Plans", "description": "", "dateRange": "", "documents": []}
    svc.store.put_survey("ws1", "fsA", "/", json.dumps({"topics": [topic], "raw": "", "sources": []}),
                         "2026-01-01T00:00:00Z")
    svc.store.put_survey("local", "NAS", "/", json.dumps({"topics": [], "raw": "", "sources": []}),
                         "2026-02-01T00:00:00Z")
    svc.store.put_survey("ws1", "fsA", "/sub", json.dumps({"topics": [topic]}), "2026-03-01T00:00:00Z")
    svc.store.mark_surveys_stale("local", "NAS")
    llm_before = svc.llm.configured
    with client(svc) as c:
        every = c.get("/index/surveys", params={"workspaceId": "*"}, headers=H).json()["surveys"]
        assert [(s["workspaceId"], s["filespace"]) for s in every] == [("local", "NAS"), ("ws1", "fsA")]
        assert every[0]["stale"] is True and every[0]["location"]["kind"] == "block"
        assert every[1] == {"workspaceId": "ws1", "filespace": "fsA",
                            "location": {"kind": "lucidlink", "root": None, "provider": None},
                            "generatedAt": "2026-01-01T00:00:00Z", "stale": False, "topics": [topic]}
        active = c.get("/index/surveys", headers=H).json()["surveys"]
        assert [s["filespace"] for s in active] == ["fsA"]        # fsB never surveyed: left out
    assert svc.llm.configured == llm_before and not llm_before    # nothing generated (no LLM at all)
