"""Simplification phase 3: What's new (GET /index/recent filters, `when`, `type`, `next`)."""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from conftest import KEY, para, run, src

from indexer.app import create_app

H = {"X-Internal-Key": KEY}
NOW = int(time.time()) - 7 * 86400   # a fixed base well in the past


@contextmanager
def client(svc):
    with TestClient(create_app(lambda: svc, start_scheduler=False)) as c:
        yield c


def iso(secs: float) -> str:
    return datetime.fromtimestamp(secs, timezone.utc).isoformat().replace("+00:00", "Z")


def put(svc, path, mtime, *, fs="fsA", ws="ws1", status="indexed", change=None, changed=None, prev=None, fid=None):
    """A ledger row. `changed` (Unix seconds) becomes changed_at."""
    fid = fid or f"{fs}:{path}"
    svc.store.insert_file(ws, fs, fid, path, 10, mtime, 1, status=status)
    svc.store.execute("UPDATE files SET change_kind=?, changed_at=?, prev_path=? "
                      "WHERE workspace_id=? AND filespace=? AND file_id=?",
                      (change, iso(changed).replace(".000000", "") if changed is not None else None, prev,
                       ws, fs, fid))


def get(c, **params):
    r = c.get("/index/recent", params=params, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def paths(body):
    return [f["path"] for f in body["files"]]


def sample(svc):
    put(svc, "/docs/report.pdf", (NOW - 10) * 10**9, change="new")                     # ns
    put(svc, "/docs/notes.DOCX", (NOW - 20) * 1000, change="modified")                 # ms
    put(svc, "/pics/cat.jpg", (NOW - 30) * 10**6, change="modified")                   # us
    put(svc, "/talks/deck.pptx", NOW - 40)                                             # s, first crawl
    put(svc, "/old/clip.mov", NOW - 900, status="deleted", change="deleted", changed=NOW - 5)
    put(svc, "/moved/sheet.xlsx", NOW - 1000, change="renamed", changed=NOW - 15, prev="/sheet.xlsx")
    put(svc, "/src/main.py", NOW - 50, change="new")
    put(svc, "/.pdf", NOW - 60, change="new")                                          # hidden: no extension


def test_when_type_and_order(make_service):
    svc = make_service()
    sample(svc)
    with client(svc) as c:
        body = get(c, filespace="fsA")
    assert body["next"] is None
    assert paths(body) == ["/old/clip.mov", "/docs/report.pdf", "/moved/sheet.xlsx", "/docs/notes.DOCX",
                           "/pics/cat.jpg", "/talks/deck.pptx", "/src/main.py", "/.pdf"]
    by = {f["path"]: f for f in body["files"]}
    assert by["/old/clip.mov"]["when"] == iso(NOW - 5) and by["/old/clip.mov"]["type"] == "video"
    assert by["/moved/sheet.xlsx"]["when"] == iso(NOW - 15) and by["/moved/sheet.xlsx"]["type"] == "spreadsheet"
    assert by["/docs/notes.DOCX"]["when"] == iso(NOW - 20) and by["/docs/notes.DOCX"]["type"] == "document"
    assert by["/talks/deck.pptx"]["when"] == iso(NOW - 40) and by["/talks/deck.pptx"]["type"] == "presentation"
    assert by["/.pdf"]["type"] == "other" and by["/src/main.py"]["type"] == "code"
    # Existing fields are kept.
    assert by["/moved/sheet.xlsx"]["previousPath"] == "/sheet.xlsx" and by["/docs/report.pdf"]["change"] == "new"


def test_kind_filter(make_service):
    svc = make_service()
    sample(svc)
    with client(svc) as c:
        assert paths(get(c, kind="documents")) == ["/docs/report.pdf", "/moved/sheet.xlsx", "/docs/notes.DOCX"]
        assert paths(get(c, kind="presentations")) == ["/talks/deck.pptx"]
        assert paths(get(c, kind="images")) == ["/pics/cat.jpg"]
        # Deletions are classed by their last name.
        assert paths(get(c, kind="video")) == ["/old/clip.mov"]
        r = c.get("/index/recent", params={"kind": "music"}, headers=H)
        assert r.status_code == 400 and "music" in r.json()["detail"]


def test_change_filter(make_service):
    svc = make_service()
    sample(svc)
    with client(svc) as c:
        assert paths(get(c, change="new")) == ["/docs/report.pdf", "/src/main.py", "/.pdf"]
        assert paths(get(c, change="deleted")) == ["/old/clip.mov"]
        assert paths(get(c, change="renamed,modified")) == ["/moved/sheet.xlsx", "/docs/notes.DOCX",
                                                            "/pics/cat.jpg"]
        assert paths(get(c, change="deleted, renamed")) == ["/old/clip.mov", "/moved/sheet.xlsx"]
        for bad in ("moved", "new,moved", ","):
            assert c.get("/index/recent", params={"change": bad}, headers=H).status_code == 400


def test_since_before(make_service):
    svc = make_service()
    sample(svc)
    with client(svc) as c:
        # since is inclusive, before exclusive; both on `when`.
        assert paths(get(c, since=iso(NOW - 15))) == ["/old/clip.mov", "/docs/report.pdf", "/moved/sheet.xlsx"]
        assert paths(get(c, before=iso(NOW - 15), since=iso(NOW - 30))) == ["/docs/notes.DOCX", "/pics/cat.jpg"]
        # Other offsets and a naive time (taken as UTC) mean the same instant.
        local = datetime.fromtimestamp(NOW - 15, timezone.utc).astimezone().isoformat()
        naive = datetime.fromtimestamp(NOW - 15, timezone.utc).replace(tzinfo=None).isoformat()
        assert paths(get(c, since=local)) == paths(get(c, since=naive)) == paths(get(c, since=iso(NOW - 15)))
        assert get(c, since=iso(NOW), before=iso(NOW - 100))["files"] == []
        for p in ({"since": "yesterday"}, {"before": "2026-13-01"}):
            r = c.get("/index/recent", params=p, headers=H)
            assert r.status_code == 400 and list(p)[0] in r.json()["detail"]


def test_combined_filters(make_service):
    svc = make_service()
    sample(svc)
    with client(svc) as c:
        assert paths(get(c, kind="documents", change="modified,renamed")) == ["/moved/sheet.xlsx",
                                                                             "/docs/notes.DOCX"]
        assert paths(get(c, kind="documents", change="modified,renamed", before=iso(NOW - 15))) == [
            "/docs/notes.DOCX"]
        assert paths(get(c, kind="documents", since=iso(NOW - 12), change="new")) == ["/docs/report.pdf"]


def test_filters_apply_before_the_limit(make_service):
    svc = make_service()
    for i in range(250):
        put(svc, f"/noise/n{i:03d}.txt", NOW - i, change="modified")
    for i in range(3):
        put(svc, f"/deck{i}.key", NOW - 1000 - i, change="new")
    with client(svc) as c:
        first = get(c, kind="presentations", change="new", limit=2)
        assert paths(first) == ["/deck0.key", "/deck1.key"] and first["next"] == iso(NOW - 1001)
        second = get(c, kind="presentations", change="new", limit=2, before=first["next"])
        assert paths(second) == ["/deck2.key"] and second["next"] is None


def test_ties_are_not_split(make_service):
    svc = make_service()
    put(svc, "/a.txt", NOW - 1)
    for p in ("/t3.txt", "/t1.txt", "/t2.txt"):
        put(svc, p, NOW - 2)
    put(svc, "/b.txt", NOW - 3)
    with client(svc) as c:
        first = get(c, limit=2)
        # The page runs over limit to keep all of NOW-2 together.
        assert paths(first) == ["/a.txt", "/t1.txt", "/t2.txt", "/t3.txt"]
        assert first["next"] == iso(NOW - 2)
        second = get(c, limit=2, before=first["next"])
        assert paths(second) == ["/b.txt"] and second["next"] is None
        # A page that ends exactly on the last file has no next.
        assert get(c, limit=5)["next"] is None
        assert get(c, limit=4)["next"] == iso(NOW - 2)


def _with_nas(make_service, tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    (share / "plan.md").write_text(para("plan"))
    svc = make_service(block_sources=f"NAS={share}", block_source_workspace="")
    src(svc).write("/a.md", para("alpha"))
    run(svc)
    svc.scheduler.run_job("local", "NAS", full=True)
    return svc


def test_paging_through_everything_with_next(make_service, tmp_path):
    svc = _with_nas(make_service, tmp_path)
    units = (1, 1000, 10**6, 10**9)
    for i in range(60):
        secs = NOW - (i // 3) * 7                       # groups of three ties
        fs, ws = (("NAS", "local") if i % 4 == 0 else ("fsB", "ws1") if i % 4 == 1 else ("fsA", "ws1"))
        name = f"/f{i:02d}" + (".pdf", ".jpg", ".txt", ".pptx")[i % 4]
        if i % 10 == 7:
            put(svc, name, NOW - 5000, fs=fs, ws=ws, status="deleted", change="deleted", changed=secs)
        elif i % 10 == 3:
            put(svc, name, NOW - 6000, fs=fs, ws=ws, change="renamed", changed=secs, prev="/x" + name)
        else:
            put(svc, name, secs * units[i % 4], fs=fs, ws=ws, change="modified")
    with client(svc) as c:
        whole = get(c, workspaceId="*", limit=200)
        assert whole["next"] is None
        everything = [(f["workspaceId"], f["filespace"], f["path"]) for f in whole["files"]]
        assert {k[:2] for k in everything} == {("ws1", "fsA"), ("ws1", "fsB"), ("local", "NAS")}
        assert len(everything) == len(set(everything)) == 60 + 2       # plus /a.md and NAS plan.md
        whens = [f["when"] for f in whole["files"]]
        assert whens == sorted(whens, reverse=True)

        got, cursor, pages = [], None, []
        while True:
            params = {"workspaceId": "*", "limit": 4}
            if cursor:
                params["before"] = cursor
            body = get(c, **params)
            assert body["files"], "a page with a cursor is never empty"
            pages.append(body["files"])
            got += [(f["workspaceId"], f["filespace"], f["path"]) for f in body["files"]]
            cursor = body["next"]
            if cursor is None:
                break
            assert cursor == body["files"][-1]["when"]
        assert got == everything                                    # no duplicates, no omissions, same order
        for a, b in zip(pages, pages[1:]):
            assert a[-1]["when"] > b[0]["when"]                       # ties never straddle pages
        assert max(len(p) for p in pages) <= 6

        # The same with filters: page through documents only.
        docs = [f["path"] for f in get(c, workspaceId="*", kind="documents", limit=200)["files"]]
        got, cursor = [], None
        while True:
            body = get(c, workspaceId="*", kind="documents", limit=3, **({"before": cursor} if cursor else {}))
            got += paths(body)
            cursor = body["next"]
            if cursor is None:
                break
        assert got == docs and len(docs) > 10
        assert not any(p.endswith((".jpg", ".pptx")) for p in docs)


def test_scope_all_workspaces(make_service, tmp_path):
    svc = _with_nas(make_service, tmp_path)
    with client(svc) as c:
        every = get(c, workspaceId="*")
        only_ws1 = get(c, workspaceId="ws1")
        nas = get(c, workspaceId="*", filespace="NAS")
    assert {(f["workspaceId"], f["path"]) for f in every["files"]} == {("ws1", "/a.md"), ("local", "/plan.md")}
    assert paths(only_ws1) == ["/a.md"]
    assert paths(nas) == ["/plan.md"] and nas["files"][0]["type"] == "document" and nas["next"] is None


def test_future_dated_files_stay_excluded_with_filters(make_service):
    svc = make_service()
    put(svc, "/now.pdf", NOW, change="new")
    put(svc, "/future.pdf", (int(time.time()) + 400 * 86400) * 10**9, change="new")
    with client(svc) as c:
        assert paths(get(c, kind="documents", change="new")) == ["/now.pdf"]
        assert paths(get(c, since=iso(NOW - 1))) == ["/now.pdf"]
