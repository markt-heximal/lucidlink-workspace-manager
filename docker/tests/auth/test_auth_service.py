"""File-service integration: token resolution, /api/v1 and /index proxies,
index hints, /agent/query delegation, CORS. The SDK is never reached."""
import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import insights_api
import ll_core
import lucidlink_api
from auth_helpers import OWNER_JWT, OTHER_JWT, SA_TOKEN, SA_TOKEN_2, owner_headers


class FakeFS:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = []

    def __getattr__(self, name):
        def op(*args, **kwargs):
            if self.fail:
                raise RuntimeError("disk on fire")
            self.calls.append((name, args))
        return op


@pytest.fixture
def fake_fs(monkeypatch):
    """Patch the SDK boundary; records which token each call used."""
    state = {"fs": FakeFS(), "tokens": []}

    def with_fs(token, filespace, fn):
        state["tokens"].append((token, filespace))
        return fn(state["fs"])

    monkeypatch.setattr(lucidlink_api, "_with_fs", with_fs)
    monkeypatch.setattr(insights_api, "_with_fs", with_fs)
    return state


@pytest.fixture
def client():
    return TestClient(lucidlink_api.app)


class Recorder:
    """httpx MockTransport handler that records requests (sync or async)."""

    def __init__(self, response=None, exc=None):
        self.requests: list[httpx.Request] = []
        self.response = response or (lambda req: httpx.Response(200, json={"ok": True}))
        self.exc = exc

    def __call__(self, request: httpx.Request):
        request.read()
        self.requests.append(request)
        if self.exc:
            raise self.exc(request)
        return self.response(request)


# ---------------------------------------------------------------- require_token
def test_require_token_lucidlink_token_unchanged(env, supabase):
    assert ll_core.require_token(f"Bearer {SA_TOKEN}", None, None) == SA_TOKEN
    assert ll_core.require_token(None, SA_TOKEN, "ws2") == SA_TOKEN
    assert ll_core.require_token_and_filespace(f"Bearer {SA_TOKEN}", None, "fs1", None) == (SA_TOKEN, "fs1")
    assert supabase.calls == 0
    with pytest.raises(HTTPException) as e:
        ll_core.require_token(None, None, None)
    assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        ll_core.require_token_and_filespace(f"Bearer {SA_TOKEN}", None, None, None)
    assert e.value.status_code == 400


def test_require_token_owner_jwt_resolves_vault(seeded_vault, supabase):
    auth = f"Bearer {OWNER_JWT}"
    assert ll_core.require_token(auth, None, "ws2") == SA_TOKEN_2
    assert ll_core.require_token(auth, None, None) == SA_TOKEN  # active workspace
    assert ll_core.require_token_and_filespace(auth, None, "fs2", "ws2") == (SA_TOKEN_2, "fs2")


def test_require_token_owner_jwt_errors(seeded_vault, supabase, monkeypatch):
    with pytest.raises(HTTPException) as e:
        ll_core.require_token(f"Bearer {OTHER_JWT}", None, "ws2")
    assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        ll_core.require_token(f"Bearer {OWNER_JWT}", None, "missing")
    assert e.value.status_code == 401
    assert "missing" in e.value.detail
    seeded_vault.save({"workspaces": [{"id": "default", "token": None}]})
    with pytest.raises(HTTPException) as e:
        ll_core.require_token(f"Bearer {OWNER_JWT}", None, None)
    assert e.value.status_code == 401
    monkeypatch.delenv("VAULT_KEY")
    with pytest.raises(HTTPException) as e:
        ll_core.require_token(f"Bearer {OWNER_JWT}", None, None)
    assert e.value.status_code == 503


def test_auth_error_redacts_token():
    err = ll_core._auth_error(RuntimeError(f"401 Unauthorized for {SA_TOKEN}"), SA_TOKEN)
    assert SA_TOKEN not in err.detail and "[redacted]" in err.detail


def test_file_route_with_owner_jwt_uses_vault_token(client, seeded_vault, supabase, fake_fs):
    r = client.get("/files/exists", params={"path": "/a"},
                   headers=owner_headers(**{"X-LucidLink-Filespace": "fs2", "X-Workspace-Id": "ws2"}))
    assert r.status_code == 200
    assert fake_fs["tokens"] == [(SA_TOKEN_2, "fs2")]


# ---------------------------------------------------------------- /api/v1 proxy
def _mgmt(monkeypatch, recorder):
    monkeypatch.setattr(lucidlink_api, "_http_client", httpx.AsyncClient(
        base_url="http://mgmt.test", transport=httpx.MockTransport(recorder)))


def test_mgmt_proxy_substitutes_vault_token(client, seeded_vault, supabase, monkeypatch):
    rec = Recorder()
    _mgmt(monkeypatch, rec)
    r = client.get("/api/v1/filespaces", headers=owner_headers(**{"X-Workspace-Id": "ws2"}))
    assert r.status_code == 200
    assert rec.requests[0].headers["authorization"] == f"Bearer {SA_TOKEN_2}"


def test_mgmt_proxy_passes_lucidlink_token_and_rejects_non_owner(client, seeded_vault, supabase, monkeypatch):
    rec = Recorder()
    _mgmt(monkeypatch, rec)
    assert client.get("/api/v1/x", headers={"Authorization": f"Bearer {SA_TOKEN}"}).status_code == 200
    assert rec.requests[0].headers["authorization"] == f"Bearer {SA_TOKEN}"
    r = client.get("/api/v1/x", headers={"Authorization": f"Bearer {OTHER_JWT}"})
    assert r.status_code == 401
    assert len(rec.requests) == 1


@pytest.mark.parametrize("path", [
    "/api/v1/%2e%2e/%2e%2e/internal/thing",
    "/api/v1/%2E%2E/internal",
    "/api/v1/a/%2e/b",
    "/api/v1/%252e%252e/internal",
    "/api/v1/..%5cinternal",
])
def test_mgmt_proxy_rejects_dot_segments(client, monkeypatch, path):
    rec = Recorder()
    _mgmt(monkeypatch, rec)
    r = client.get(path, headers={"Authorization": f"Bearer {SA_TOKEN}"})
    assert r.status_code == 400
    assert rec.requests == []


def test_mgmt_proxy_normal_path_still_forwarded(client, monkeypatch):
    rec = Recorder()
    _mgmt(monkeypatch, rec)
    assert client.get("/api/v1/filespaces/a.b", headers={"Authorization": f"Bearer {SA_TOKEN}"}).status_code == 200
    assert rec.requests[0].url.path == "/api/v1/filespaces/a.b"


def test_index_proxy_rejects_dot_segments(client, env, supabase, monkeypatch):
    rec = Recorder()
    _indexer(monkeypatch, rec)
    assert client.get("/index/%2e%2e/health", headers=owner_headers()).status_code == 400
    assert rec.requests == []


# ---------------------------------------------------------------- /index proxy
def _indexer(monkeypatch, recorder):
    monkeypatch.setattr(lucidlink_api, "_index_transport", httpx.MockTransport(recorder))


def test_index_proxy_requires_owner(client, env, supabase, monkeypatch):
    rec = Recorder()
    _indexer(monkeypatch, rec)
    assert client.get("/index/status").status_code == 401
    assert client.get("/index/status", headers={"Authorization": f"Bearer {SA_TOKEN}"}).status_code == 401
    assert client.get("/index/status", headers={"Authorization": f"Bearer {OTHER_JWT}"}).status_code == 401
    assert rec.requests == []


def test_index_proxy_forwards(client, env, supabase, monkeypatch):
    rec = Recorder(lambda req: httpx.Response(202, json={"queued": []}))
    _indexer(monkeypatch, rec)
    r = client.post("/index/sync?x=1", content=b'{"filespace":"fs1"}',
                    headers=owner_headers(**{"Content-Type": "application/json", "X-Workspace-Id": "ws2"}))
    assert r.status_code == 202 and r.json() == {"queued": []}
    req = rec.requests[0]
    assert req.method == "POST"
    assert req.url.path == "/index/sync" and req.url.query == b"x=1"
    assert req.headers["x-internal-key"] == "internal-secret"
    assert req.headers["content-type"] == "application/json"
    assert req.headers["x-workspace-id"] == "ws2"
    assert "authorization" not in req.headers  # the owner's session stays here
    assert json.loads(req.content) == {"filespace": "fs1"}

    rec.response = lambda req: httpx.Response(404, json={"detail": "never generated"})
    r = client.get("/index/survey", params={"filespace": "fs1"}, headers=owner_headers())
    assert r.status_code == 404 and rec.requests[-1].url.params["filespace"] == "fs1"


@pytest.mark.parametrize("exc,status", [
    (lambda req: httpx.ConnectError("refused", request=req), 502),
    (lambda req: httpx.ReadTimeout("slow", request=req), 504),
])
def test_index_proxy_maps_errors(client, env, supabase, monkeypatch, exc, status):
    _indexer(monkeypatch, Recorder(exc=exc))
    assert client.get("/index/status", headers=owner_headers()).status_code == status


def test_index_proxy_without_internal_key(client, env, supabase, monkeypatch):
    monkeypatch.setenv("INDEXER_INTERNAL_KEY", "")
    assert client.get("/index/status", headers=owner_headers()).status_code == 503


# ---------------------------------------------------------------- hints
@pytest.fixture
def hints(monkeypatch):
    rec = Recorder(lambda req: httpx.Response(202, json={}))
    monkeypatch.setattr(lucidlink_api, "_hint_transport", httpx.MockTransport(rec))
    return rec


def _hint_bodies(rec):
    return [json.loads(r.content) for r in rec.requests]


def _owner_fs(**extra):
    return owner_headers(**{"X-LucidLink-Filespace": "fs1", **extra})


def test_hint_after_owner_write(client, seeded_vault, supabase, fake_fs, hints):
    # No X-Workspace-Id: the hint carries the vault's resolved (active) workspace.
    assert client.post("/files/write", json={"path": "/a.md", "content": "x"},
                       headers=_owner_fs()).status_code == 200
    assert _hint_bodies(hints) == [{"workspaceId": "default", "filespace": "fs1",
                                    "op": "write", "path": "/a.md"}]
    assert hints.requests[0].headers["x-internal-key"] == "internal-secret"
    assert hints.requests[0].url.path == "/index/hint"
    assert fake_fs["tokens"] == [(SA_TOKEN, "fs1")]

    h = _owner_fs(**{"X-Workspace-Id": "ws2"})
    client.post("/files/move", json={"src": "/a.md", "dst": "/b.md"}, headers=h)
    client.post("/files/mkdir", params={"path": "/d"}, headers=h)
    client.delete("/files", params={"path": "/b.md"}, headers=h)
    client.delete("/files/dir", params={"path": "/d"}, headers=h)
    client.post("/files/upload", data={"path": "/u.txt"}, files={"file": ("u.txt", b"hi")}, headers=h)
    assert _hint_bodies(hints)[1:] == [
        {"workspaceId": "ws2", "filespace": "fs1", "op": "move", "path": "/b.md", "src": "/a.md"},
        {"workspaceId": "ws2", "filespace": "fs1", "op": "mkdir", "path": "/d"},
        {"workspaceId": "ws2", "filespace": "fs1", "op": "delete", "path": "/b.md"},
        {"workspaceId": "ws2", "filespace": "fs1", "op": "delete", "path": "/d"},
        {"workspaceId": "ws2", "filespace": "fs1", "op": "write", "path": "/u.txt"},
    ]
    assert all(t == SA_TOKEN_2 for t, _ in fake_fs["tokens"][1:])


def test_no_hint_for_lucidlink_token_caller(client, seeded_vault, supabase, fake_fs, hints):
    # Regression: a token caller (possibly not the owner) must not be able to
    # poke the owner's index, whatever X-Workspace-Id it claims.
    for ws in (None, "default", "ws2"):
        h = {"Authorization": f"Bearer {SA_TOKEN}", "X-LucidLink-Filespace": "fs1"}
        if ws:
            h["X-Workspace-Id"] = ws
        assert client.post("/files/mkdir", params={"path": "/x"}, headers=h).status_code == 200
        assert client.post("/files/write", json={"path": "/a", "content": "x"}, headers=h).status_code == 200
    assert hints.requests == []
    assert supabase.calls == 0


def test_owner_unknown_workspace_is_401_and_no_hint(client, seeded_vault, supabase, fake_fs, hints):
    r = client.post("/files/mkdir", params={"path": "/x"}, headers=_owner_fs(**{"X-Workspace-Id": "nope"}))
    assert r.status_code == 401
    assert hints.requests == [] and fake_fs["tokens"] == []


def test_no_hint_after_failed_write(client, seeded_vault, supabase, fake_fs, hints):
    fake_fs["fs"] = FakeFS(fail=True)
    r = client.post("/files/write", json={"path": "/a.md", "content": "x"}, headers=_owner_fs())
    assert r.status_code == 500
    assert hints.requests == []


def test_no_hint_without_internal_key(client, seeded_vault, supabase, fake_fs, hints, monkeypatch):
    monkeypatch.setenv("INDEXER_INTERNAL_KEY", "")
    client.post("/files/write", json={"path": "/a.md", "content": "x"}, headers=_owner_fs())
    assert hints.requests == []


def test_hint_failure_does_not_break_write(client, seeded_vault, supabase, fake_fs, monkeypatch):
    rec = Recorder(exc=lambda req: httpx.ConnectError("down", request=req))
    monkeypatch.setattr(lucidlink_api, "_hint_transport", httpx.MockTransport(rec))
    r = client.post("/files/write", json={"path": "/a.md", "content": "x"}, headers=_owner_fs())
    assert r.status_code == 200 and len(rec.requests) == 1


# ---------------------------------------------------------------- /agent/query
@pytest.fixture
def shallow_agent(monkeypatch, fake_fs):
    monkeypatch.setattr(insights_api, "LLM_BASE_URL", "http://llm.test")
    monkeypatch.setattr(insights_api, "LLM_MODEL", "m")
    monkeypatch.setattr(insights_api, "_gather_context", lambda fs: ("ctx", ["/shallow.md"]))
    monkeypatch.setattr(insights_api, "_call_llm", lambda q, c: "shallow answer")
    return fake_fs


def _ask(monkeypatch, recorder):
    monkeypatch.setattr(insights_api, "_indexer_transport", httpx.MockTransport(recorder))


def test_agent_query_delegates_to_indexer(client, seeded_vault, supabase, shallow_agent, monkeypatch):
    rec = Recorder(lambda req: httpx.Response(200, json={
        "answer": "indexed answer", "sources": ["/a.md"], "citations": []}))
    _ask(monkeypatch, rec)
    r = client.post("/agent/query", json={"filespace": "fs2", "question": "what?"},
                    headers=owner_headers(**{"X-Workspace-Id": "ws2"}))
    assert r.status_code == 200
    assert r.json() == {"answer": "indexed answer", "sources": ["/a.md"]}
    req = rec.requests[0]
    assert req.url.path == "/index/ask"
    assert req.headers["x-internal-key"] == "internal-secret"
    assert req.headers["x-workspace-id"] == "ws2"
    assert json.loads(req.content) == {"filespace": "fs2", "question": "what?", "workspaceId": "ws2"}
    assert shallow_agent["tokens"] == []  # never touched the filespace


@pytest.mark.parametrize("respond", [
    lambda req: httpx.Response(200, json={"empty": True, "answer": "", "sources": []}),
    lambda req: httpx.Response(503, json={"detail": "no LLM"}),
    lambda req: httpx.Response(500, text="boom"),
    "connect",
])
def test_agent_query_falls_back(client, seeded_vault, supabase, shallow_agent, monkeypatch, respond):
    if respond == "connect":
        rec = Recorder(exc=lambda req: httpx.ConnectError("refused", request=req))
    else:
        rec = Recorder(respond)
    _ask(monkeypatch, rec)
    r = client.post("/agent/query", json={"filespace": "fs1", "question": "q"}, headers=owner_headers())
    assert r.status_code == 200
    assert r.json() == {"answer": "shallow answer", "sources": ["/shallow.md"]}
    assert len(rec.requests) == 1
    assert shallow_agent["tokens"] == [(SA_TOKEN, "fs1")]


def test_agent_query_lucidlink_token_skips_indexer(client, env, supabase, shallow_agent, monkeypatch):
    rec = Recorder()
    _ask(monkeypatch, rec)
    r = client.post("/agent/query", json={"filespace": "fs1", "question": "q"},
                    headers={"Authorization": f"Bearer {SA_TOKEN}"})
    assert r.status_code == 200 and r.json()["answer"] == "shallow answer"
    assert rec.requests == []


# ---------------------------------------------------------------- CORS
def test_cors_preflight_allows_workspace_header(client):
    r = client.options("/files", headers={
        "Origin": "https://app.example",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization,x-workspace-id,x-lucidlink-filespace",
    })
    assert r.status_code == 200
    allowed = r.headers["access-control-allow-headers"].lower()
    assert "x-workspace-id" in allowed
