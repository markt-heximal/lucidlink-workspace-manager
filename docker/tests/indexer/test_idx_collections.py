"""Collections step 1: search/ask limited to files, and POST /index/collection/files."""
from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from conftest import KEY, para, run, src

from indexer.app import create_app

H = {"X-Internal-Key": KEY}


@contextmanager
def client(svc):
    with TestClient(create_app(lambda: svc, start_scheduler=False)) as c:
        yield c


def ref(path, fs="fsA", ws="ws1"):
    return {"workspaceId": ws, "filespace": fs, "path": path}


def paths(rows):
    return [r["path"] for r in rows]


def corpus(svc):
    a = src(svc, "fsA")
    a.write("/a/roses.md", para("roses garden pruning"))
    a.write("/a/tulips.md", para("tulips garden bulbs"))
    a.write("/a/budget.md", para("budget invoice quarterly"))
    a.write("/a/photo.png", b"\x89PNG not really")
    b = src(svc, "fsB")
    b.write("/b/orchids.md", para("orchids garden greenhouse"))
    b.write("/b/taxes.md", para("taxes invoice filing"))
    run(svc, "fsA")
    run(svc, "fsB")


# ---------------------------------------------------------------- search + ask limited to files
def test_search_limited_to_files_across_two_locations(make_service):
    svc = make_service()
    corpus(svc)
    every = svc.search_all("ws1", None, "garden", k=50)
    assert {"/a/roses.md", "/a/tulips.md", "/b/orchids.md"} <= set(paths(every["files"]))

    only = [ref("/a/roses.md"), ref("/b/orchids.md", "fsB"), ref("/nope.md"), ref("/a/roses.md", "fsZ")]
    for mode in ("all", "meaning", "keyword"):
        body = svc.search_all(None, None, "garden", k=50, mode=mode, files=only)
        assert set(paths(body["files"])) <= {"/a/roses.md", "/b/orchids.md"}, mode
        assert set(paths(body["results"])) <= {"/a/roses.md", "/b/orchids.md"}, mode
        assert body["files"], mode
    body = svc.search_all(None, None, "garden", k=50, files=only)
    assert set(paths(body["files"])) == {"/a/roses.md", "/b/orchids.md"}
    # A name match is limited too.
    assert paths(svc.search_all(None, None, "tulips", files=only)["files"]) == []
    # The body's own filespace narrows the refs.
    assert paths(svc.search_all(None, "fsB", "garden", files=only)["files"]) == ["/b/orchids.md"]
    # Unknown refs only, or none: nothing.
    assert svc.search_all(None, None, "garden", files=[ref("/nope.md")])["files"] == []
    assert svc.search_all(None, None, "garden", files=[])["total"] == 0


def test_search_route_files_ignore_workspace_header_and_limit_500(make_service):
    svc = make_service()
    corpus(svc)
    with client(svc) as c:
        r = c.post("/index/search", headers={**H, "X-Workspace-Id": "other"},
                   json={"query": "invoice", "files": [ref("/b/taxes.md", "fsB")]})
        assert r.status_code == 200 and paths(r.json()["files"]) == ["/b/taxes.md"]
        many = [ref(f"/x{i}.md") for i in range(499)] + [ref("/a/budget.md")]
        r = c.post("/index/search", headers=H, json={"query": "invoice", "files": many})
        assert r.status_code == 200 and paths(r.json()["files"]) == ["/a/budget.md"]
        r = c.post("/index/search", headers=H, json={"query": "invoice", "files": many + [ref("/y.md")]})
        assert r.status_code == 422
        r = c.post("/index/search", headers=H, json={"query": "invoice", "files": [ref("no-slash")]})
        assert r.status_code == 422
        r = c.post("/index/ask", headers=H, json={"question": "invoice?", "files": many + [ref("/y.md")]})
        assert r.status_code == 422


def test_ask_cites_only_the_given_files(make_service):
    calls = []
    svc = make_service(llm_answer="See /b/taxes.md.", llm_calls=calls)
    corpus(svc)
    # No resolvable file: the empty answer, no LLM call.
    n = len(calls)
    res = svc.ask(None, None, "invoice?", files=[ref("/gone.md")])
    assert res["empty"] is True and len(calls) == n
    with client(svc) as c:
        r = c.post("/index/ask", headers=H, json={"question": "What about the invoice?",
                                                  "files": [ref("/b/taxes.md", "fsB"), ref("/gone.md")]})
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"answer", "sources", "citations"}
    assert body["sources"] == ["/b/taxes.md"]
    assert {(c["filespace"], c["path"]) for c in body["citations"]} == {("fsB", "/b/taxes.md")}
    prompt = calls[-1]["messages"][-1]["content"]
    assert "Question: What about the invoice?" in prompt and "budget invoice" not in prompt


# ---------------------------------------------------------------- collection/files
def test_collection_union_minus_and_order(make_service):
    svc = make_service()
    corpus(svc)
    with client(svc) as c:
        r = c.post("/index/collection/files", headers=H, json={
            "query": "garden", "mode": "all", "filters": {}, "scope": None,
            "pinned": [ref("/a/photo.png"), ref("/a/budget.md"), ref("/a/roses.md"), ref("/missing.md")],
            "excluded": [ref("/a/tulips.md")],
        })
    assert r.status_code == 200
    body = r.json()
    files = body["files"]
    got = paths(files)
    # Pinned first: the matched pin (by score), then unmatched pins in pin order.
    assert got[:3] == ["/a/roses.md", "/a/photo.png", "/a/budget.md"]
    assert [f["pinned"] for f in files[:3]] == [True, True, True]
    assert "/b/orchids.md" in got[3:] and all(not f["pinned"] for f in files[3:])
    assert "/a/tulips.md" not in got and "/missing.md" not in got
    rest = [f["score"] for f in files[3:]]
    assert rest == sorted(rest, reverse=True)
    roses = files[0]
    assert roses["why"]["meaning"] or roses["why"]["keyword"]
    assert roses["snippets"] and roses["score"] > 0
    photo = files[1]
    assert photo["why"] == {"meaning": False, "keyword": False, "name": False, "mentions": 0}
    assert photo["score"] == 0 and photo["snippets"] == [] and photo["name"] == "photo.png"
    assert photo["size"] > 0 and photo["mtime"] and photo["location"]["kind"] == "lucidlink"
    assert body["total"] == len(set(got))


def test_collection_pins_only_scope_and_excluded_wins(make_service):
    svc = make_service()
    corpus(svc)
    body = svc.collection_files("", pinned=[ref("/b/taxes.md", "fsB"), ref("/a/roses.md"),
                                            ref("/a/budget.md")],
                                excluded=[ref("/a/budget.md")])
    assert paths(body["files"]) == ["/b/taxes.md", "/a/roses.md"]       # pin order; excluded wins
    assert all(f["pinned"] for f in body["files"]) and body["total"] == 2
    # A scoped search: matches from fsB only, pins from anywhere.
    body = svc.collection_files("garden", scope={"workspaceId": "ws1", "filespace": "fsB"},
                                pinned=[ref("/a/budget.md")])
    assert paths(body["files"]) == ["/a/budget.md", "/b/orchids.md"]
    # k caps the search results, never the pins.
    body = svc.collection_files("garden", k=1, pinned=[ref("/a/budget.md")])
    assert len(body["files"]) == 2 and body["files"][0]["path"] == "/a/budget.md"


def test_collection_pinned_match_beyond_k_keeps_its_why(make_service):
    svc = make_service()
    corpus(svc)
    body = svc.collection_files("garden", k=1, pinned=[ref("/b/orchids.md", "fsB"), ref("/a/tulips.md")])
    pinned = [f for f in body["files"] if f["pinned"]]
    assert {f["path"] for f in pinned} == {"/b/orchids.md", "/a/tulips.md"}
    assert all(f["why"]["meaning"] or f["why"]["keyword"] for f in pinned)


def test_collection_route_validation(make_service):
    svc = make_service()
    corpus(svc)
    with client(svc) as c:
        assert c.post("/index/collection/files", json={}).status_code == 401
        assert c.post("/index/collection/files", headers=H, json={"k": 501}).status_code == 422
        assert c.post("/index/collection/files", headers=H,
                      json={"pinned": [ref(f"/p{i}") for i in range(501)]}).status_code == 422
        assert c.post("/index/collection/files", headers=H, json={"mode": "x"}).status_code == 422
        r = c.post("/index/collection/files", headers=H, json={})
        assert r.status_code == 200 and r.json() == {"files": [], "total": 0}
        r = c.post("/index/collection/files", headers=H,
                   json={"query": "invoice", "mode": "keyword", "filters": {"pathPrefix": "/b"}})
        assert paths(r.json()["files"]) == ["/b/taxes.md"]
