"""Simplification phase 1: find by name, where each item lives, and why something is wrong."""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from conftest import KEY, para, run, src

from indexer.app import create_app
from indexer.folders import default_name
from indexer.service import problem_of

H = {"X-Internal-Key": KEY}


@contextmanager
def client(svc):
    with TestClient(create_app(lambda: svc, start_scheduler=False)) as c:
        yield c


def test_find_by_name_across_locations(make_service, tmp_path):
    share = tmp_path / "share"
    (share / "Clients" / "Northwind").mkdir(parents=True)
    (share / "Clients" / "Northwind" / "brief.md").write_text(para("brief"))
    (share / "northwind-logo.mov").write_text("x")
    svc = make_service(block_sources=f"NAS={share}", block_source_workspace="")
    src(svc).write("/Contracts/Northwind MSA.pdf", "%PDF-1.4 not really")
    src(svc).write("/notes.md", para("mentions northwind in the text only"))
    run(svc)
    svc.scheduler.run_job("local", "NAS", full=True)
    with client(svc) as c:
        res = c.get("/index/find", params={"q": "northwind", "workspaceId": "*"}, headers=H).json()["results"]
        names = [r["name"] for r in res]
        # Names starting with the query first; a folder word finds the files inside it;
        # unreadable files (video) are found too; content-only matches are not.
        assert names[0] == "northwind-logo.mov" and set(names) == {"northwind-logo.mov", "Northwind MSA.pdf", "brief.md"}
        nas = next(r for r in res if r["filespace"] == "NAS")
        assert nas["location"] == {"kind": "block", "root": None, "provider": None} and nas["workspaceId"] == "local"
        both = c.get("/index/find", params={"q": "northwind msa", "workspaceId": "*"}, headers=H).json()["results"]
        assert [r["name"] for r in both] == ["Northwind MSA.pdf"]
        assert c.get("/index/find", params={"q": "n"}, headers=H).json() == {"results": []}
        scoped = c.get("/index/find", params={"q": "northwind"}, headers=H).json()["results"]
        assert {r["filespace"] for r in scoped} == {"fsA"}     # default: the active workspace
        assert c.get("/index/find", params={"q": "100%_"}, headers=H).json() == {"results": []}


@pytest.mark.parametrize("error,kind", [
    ("SourceError: could not link filespace 'video': Authentication failed: HTTP request failed: Host not found: api.lucidlink.com",
     "unreachable"),
    ("SourceError: could not link filespace 'x': Authentication failed: invalid token", "sign_in"),
    ("SDK error: filespace mount failed (LL_ERR_37: lease expired)", "sign_in"),
    ("HTTP request failed: Host not found: api.lucidlink.com", "unreachable"),
    ("block source path not mounted", "offline"),
    ("embedder error: 413 payload too large", "too_big"),
    ("Embedder unavailable: connection reset", "engine"),
    ("could not link: connection refused", "unreachable"),
    ("Ruvector upsert failed: 500", "engine"),
    ("something odd", "other"),
])
def test_problem_kinds(error, kind):
    assert problem_of(error) == {"kind": kind, "detail": error}
    assert problem_of(None) is None and problem_of("") is None


def test_status_and_results_say_where_things_live(make_service, tmp_path):
    base = tmp_path / "host"
    (base / "icloud" / "Notes").mkdir(parents=True)
    (base / "icloud" / "Notes" / "ideas.md").write_text(para("ideas about harbor"))
    svc = make_service(block_root_base=str(base), block_source_workspace="",
                       block_root_hosts={"icloud": "/Users/m/Library/Mobile Documents/com~apple~CloudDocs"})
    with client(svc) as c:
        c.post("/index/folders", json={"root": "icloud", "path": "/Notes", "name": "Notes"}, headers=H)
        svc.scheduler.run_job("local", "Notes", full=True)
        st = c.get("/index/status", headers=H).json()
        notes = next(f for f in st["filespaces"] if f["filespace"] == "Notes")
        assert notes["location"] == {"kind": "block", "root": "icloud", "provider": None} and notes["problem"] is None
        fsa = next(f for f in st["filespaces"] if f["filespace"] == "fsA")
        assert fsa["location"] == {"kind": "lucidlink", "root": None, "provider": None}
        recent = c.get("/index/recent", params={"workspaceId": "*"}, headers=H).json()["files"]
        assert recent[0]["location"]["root"] == "icloud"
        hits = c.post("/index/search", json={"workspaceId": "*", "query": "harbor ideas"}, headers=H).json()["results"]
        assert hits and hits[0]["location"]["root"] == "icloud"


def test_suggested_names():
    assert default_name("cloud", "/GoogleDrive-mark.personal@gmail.com") == "Google Drive (mark.personal)"
    assert default_name("cloud", "/GoogleDrive-mark.personal@gmail.com/My Drive") == "Google Drive (mark.personal)"
    assert default_name("cloud", "/GoogleDrive-mark.personal@gmail.com/My Drive/Board") == "Board"
    assert default_name("cloud", "/Dropbox") == "Dropbox"
    assert default_name("icloud", "/") == "iCloud"
    assert default_name("volumes", "/Client Projects") == "Client Projects"
    assert default_name("volumes", "/a;b=c") == "a-b-c"
