"""HTTP contract: auth, status shape, sync/hint/filespaces, search, ask, survey."""
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


def test_internal_key_enforced(make_service):
    with client(make_service()) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/index/status").status_code == 401
        assert c.get("/index/status", headers={"X-Internal-Key": "wrong"}).status_code == 401
        assert c.post("/index/search", json={"query": "x"}).status_code == 401
        assert c.get("/index/status", headers=H).status_code == 200


def test_empty_key_refuses_everything(make_service):
    with client(make_service(internal_key="")) as c:
        assert c.get("/health").status_code == 200
        for headers in ({}, {"X-Internal-Key": ""}, {"X-Internal-Key": "anything"}):
            assert c.get("/index/status", headers=headers).status_code == 503
            assert c.post("/index/sync", json={}, headers=headers).status_code == 503


def test_status_shape(make_service):
    svc = make_service()
    src(svc).write("/a.md", para("alpha"))
    run(svc)
    with client(svc) as c:
        body = c.get("/index/status", headers=H).json()
    assert set(body) >= {"filespaces", "embedder", "vectorStore"}
    assert [(f["workspaceId"], f["filespace"]) for f in body["filespaces"]] == [("ws1", "fsA"), ("ws1", "fsB")]
    fa = body["filespaces"][0]
    assert set(fa) >= {"workspaceId", "filespace", "enabled", "state", "lastScanAt", "lastSyncAt", "nextScanAt",
                       "files", "chunks", "lastError", "progress"}
    assert fa["enabled"] is True and fa["state"] == "idle"
    assert fa["files"] == {"indexed": 1, "pending": 0, "failed": 0, "skipped": 0}
    assert fa["chunks"] >= 1
    assert fa["progress"] == {"done": 0, "total": 0, "current": None}
    assert body["embedder"] == {"model": "fake-embed", "dim": 64, "ok": True, "error": None}
    assert body["vectorStore"]["backend"] == "local" and body["vectorStore"]["ok"] is True
    assert body["vectorStore"]["points"] == fa["chunks"]


def test_sync_hint_and_filespace_toggle(make_service):
    svc = make_service()
    with client(svc) as c:
        r = c.post("/index/sync", json={}, headers=H)
        assert r.status_code == 202
        assert r.json() == {"queued": [{"workspaceId": "ws1", "filespace": "fsA"},
                                       {"workspaceId": "ws1", "filespace": "fsB"}]}
        r = c.post("/index/sync", json={"filespace": "fsB"}, headers=H)
        assert r.json() == {"queued": [{"workspaceId": "ws1", "filespace": "fsB"}]}

        r = c.put("/index/filespaces", json={"workspaceId": "ws1", "filespace": "fsB", "enabled": False}, headers=H)
        assert r.status_code == 200
        assert r.json()["enabled"] is False and r.json()["state"] == "disabled"
        assert c.post("/index/sync", json={"filespace": "fsB"}, headers=H).json() == {"queued": []}
        # The override survives in index.db.
        assert svc.store.filespace_row("ws1", "fsB")["enabled"] == 0

        r = c.post("/index/hint", json={"filespace": "fsA", "op": "write", "path": "/x.md"}, headers=H)
        assert r.status_code == 202 and r.json() == {}
        assert svc.scheduler.runtime("ws1", "fsA").hints[-1]["path"] == "/x.md"
        assert c.post("/index/hint", json={"filespace": "fsA", "op": "chmod", "path": "/x"},
                      headers=H).status_code == 422


def test_workspace_header_is_default_and_body_wins(make_service):
    svc = make_service()
    with client(svc) as c:
        c.post("/index/hint", json={"filespace": "fsA", "op": "write", "path": "/h.md"},
               headers={**H, "X-Workspace-Id": "wsX"})
        assert svc.scheduler.runtime("wsX", "fsA").hints[-1]["path"] == "/h.md"
        c.post("/index/hint", json={"workspaceId": "ws1", "filespace": "fsA", "op": "write", "path": "/b.md"},
               headers={**H, "X-Workspace-Id": "wsX"})
        assert svc.scheduler.runtime("ws1", "fsA").hints[-1]["path"] == "/b.md"


def test_search_across_filespaces_and_path_prefix(make_service):
    svc = make_service()
    src(svc, "fsA").write("/notes/garden.md", para("garden roses"))
    src(svc, "fsA").write("/other/garden.md", para("garden tulips"))
    src(svc, "fsB").write("/garden.md", para("garden roses"))
    run(svc, "fsA")
    run(svc, "fsB")
    with client(svc) as c:
        res = c.post("/index/search", json={"query": "garden roses"}, headers=H).json()["results"]
        assert {r["filespace"] for r in res} == {"fsA", "fsB"}
        r0 = res[0]
        assert set(r0) == {"workspaceId", "filespace", "path", "chunkIndex", "score", "snippet", "mtime"}
        assert 0 <= r0["score"] <= 1 and r0["mtime"].endswith("Z")
        assert res == sorted(res, key=lambda r: -r["score"])

        one = c.post("/index/search", json={"query": "garden", "filespace": "fsB"}, headers=H).json()["results"]
        assert {r["filespace"] for r in one} == {"fsB"}

        pre = c.post("/index/search", json={"query": "garden", "filespace": "fsA", "pathPrefix": "/notes"},
                     headers=H).json()["results"]
        assert pre and {r["path"] for r in pre} == {"/notes/garden.md"}

        k1 = c.post("/index/search", json={"query": "garden", "k": 1}, headers=H).json()["results"]
        assert len(k1) == 1


def test_ask_503_without_llm(make_service):
    svc = make_service()
    src(svc).write("/a.md", para("alpha"))
    run(svc)
    with client(svc) as c:
        r = c.post("/index/ask", json={"question": "what?"}, headers=H)
    assert r.status_code == 503


def test_ask_empty_index(make_service):
    calls: list = []
    svc = make_service(llm_answer="unused", llm_calls=calls)
    with client(svc) as c:
        body = c.post("/index/ask", json={"filespace": "fsA", "question": "anything?"}, headers=H).json()
    assert body["empty"] is True and body["sources"] == [] and body["citations"] == []
    assert body["answer"]
    assert calls == []


def test_ask_answers_with_citations(make_service):
    calls: list = []
    svc = make_service(llm_answer="Rent is 1200 (see /money/budget.md).", llm_calls=calls)
    src(svc).write("/money/budget.md", para("rent budget"))
    src(svc).write("/misc.md", para("holiday photos"))
    run(svc)
    with client(svc) as c:
        body = c.post("/index/ask", json={"filespace": "fsA", "question": "what is the rent budget", "k": 2},
                      headers=H).json()
    assert body["answer"] == "Rent is 1200 (see /money/budget.md)."
    assert body["sources"][0] == "/money/budget.md"
    assert body["citations"][0]["path"] == "/money/budget.md"
    assert set(body["citations"][0]) == {"path", "chunkIndex", "score"}
    sent = calls[-1]          # calls[0] is the post-sync survey
    assert sent["stream"] is True
    assert "PATH: /money/budget.md" in sent["messages"][1]["content"]
    assert "using only the provided excerpts" in sent["messages"][0]["content"]


def test_survey_lifecycle(make_service):
    topics = {"topics": [{"name": "Money", "description": "Budgets", "dateRange": "2026",
                          "documents": [{"path": "/budget.md", "title": "Budget", "date": ""}]}]}
    calls: list = []
    svc = make_service(llm_answer="```json\n" + json.dumps(topics) + "\n```", llm_calls=calls)
    s = src(svc)
    s.write("/budget.md", para("budget invoices pricing"))
    with client(svc) as c:
        assert c.get("/index/survey", params={"filespace": "fsA"}, headers=H).status_code == 404
        run(svc)    # a changed sync generates the root survey
        got = c.get("/index/survey", params={"filespace": "fsA", "scope": "/"}, headers=H).json()
        assert got["topics"] == topics["topics"] and got["stale"] is False
        assert got["sources"] == ["/budget.md"] and got["generatedAt"] and got["raw"].startswith("```")
        assert "document librarian" in calls[0]["messages"][0]["content"]

        s.write("/budget.md", para("budget invoices pricing revised"))
        run(svc)    # changed, but regenerated <10 min ago: stored survey is only marked stale
        assert len(calls) == 1
        assert c.get("/index/survey", params={"filespace": "fsA"}, headers=H).json()["stale"] is True

        fresh = c.post("/index/survey", json={"filespace": "fsA"}, headers=H).json()
        assert fresh["stale"] is False and len(calls) == 2


def test_survey_without_llm(make_service):
    svc = make_service()
    with client(svc) as c:
        assert c.post("/index/survey", json={"filespace": "fsA"}, headers=H).status_code == 503
