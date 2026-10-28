"""Smoke test against the installed SDK (the requirements.txt pin, 1.0.1).

Every module is imported with the real `lucidlink` package, and the code that
touches it runs against the SDK's own Python layer (`FileSystem`, `FsUtils`,
`FsEntry`, `ConnectManager`, data-store handles, `HttpConnect_3_6`, the typed
exceptions, `CacheConfig`, `FilespaceInfo`) over the in-memory native fake in
tests/sdk_fake.py. Only `Client` (which would start the native daemon and log
in) is replaced. No LucidLink service is reached.
"""
from __future__ import annotations

import importlib
import pathlib
import re
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import lucidlink
from lucidlink import exceptions as llx

from sdk_fake import make_filespace

DOCKER = pathlib.Path(__file__).resolve().parents[2]
TOKEN = "sa_live:smoke-key:smoke-secret"
H = {"Authorization": f"Bearer {TOKEN}", "X-LucidLink-Filespace": "fs1"}

MODULES = [
    "ll_cache", "ll_core", "owner_auth", "vault", "connect_api", "insights_api", "lucidlink_api",
    "indexer.app", "indexer.ask", "indexer.chunker", "indexer.config", "indexer.embedder",
    "indexer.extract", "indexer.llm", "indexer.scanner", "indexer.service", "indexer.source",
    "indexer.store", "indexer.survey", "indexer.vectors", "indexer.worker",
    "updates", "updates.api", "updates.components", "updates.monitor", "updates.notifiers",
    "updates.scheduler", "updates.sources", "updates.store", "updates.summarize", "updates.versions",
]


def test_installed_sdk_is_the_pinned_one():
    pin = re.search(r"^lucidlink==(\S+)", (DOCKER / "requirements.txt").read_text(), re.M).group(1)
    assert lucidlink.__version__ == pin == "1.0.1"


@pytest.mark.parametrize("name", MODULES)
def test_every_module_imports(name):
    importlib.import_module(name)


# ---------------------------------------------------------------- ll_cache
def test_cache_sizes_default_and_env(monkeypatch):
    import ll_cache
    monkeypatch.delenv("LUCIDLINK_CACHE_DATA_GB", raising=False)
    monkeypatch.delenv("LUCIDLINK_CACHE_METADATA_GB", raising=False)
    assert ll_cache.cache_sizes() == (5 * 1024**3, 1024**3)
    monkeypatch.setenv("LUCIDLINK_CACHE_DATA_GB", "0.5")
    monkeypatch.setenv("LUCIDLINK_CACHE_METADATA_GB", "0.25")
    assert ll_cache.cache_config() == lucidlink.CacheConfig(data_size=512 * 1024**2, metadata_size=256 * 1024**2)
    for bad in ("0", "-1", "abc", "nan", "inf"):
        monkeypatch.setenv("LUCIDLINK_CACHE_DATA_GB", bad)
        assert ll_cache.cache_sizes()[0] == 5 * 1024**3
    monkeypatch.setenv("LUCIDLINK_CACHE_DATA_GB", "1.0000001")      # rounded down to whole MiB
    assert ll_cache.cache_sizes()[0] % (1024**2) == 0


# ---------------------------------------------------------------- ll_core registry
@pytest.fixture
def sdk_client(monkeypatch):
    """Replace lucidlink.Client: login + workspace + link_filespace(id, cache=)."""
    import ll_core
    calls: list = []
    linked: dict = {}

    class Workspace:
        def list_filespaces(self):
            return [lucidlink.FilespaceInfo(id="fid-1", name="fs1",
                                            created_at=datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc))]

        def link_filespace(self, filespace_id, *, cache=None):
            calls.append(("link", filespace_id, cache))
            fsp = make_filespace()
            fsp.unlink = lambda: calls.append(("unlink", filespace_id))
            linked[filespace_id] = fsp
            return fsp

    class Client:
        def __init__(self, *, storage=None):
            self._ws = None

        def login(self, creds):
            calls.append(("login", creds.token))
            if creds.token.endswith(":revoked"):
                raise llx.AuthenticationError(f"token {creds.token} was revoked")
            self._ws = Workspace()

        @property
        def workspace(self):
            if self._ws is None:
                raise llx.NotLoggedInError("not logged in")
            return self._ws

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(lucidlink, "Client", Client)
    ll_core.shutdown()
    ll_core._fs_list_cache.clear()
    yield calls, linked
    ll_core.shutdown()
    ll_core._fs_list_cache.clear()


def test_ll_core_links_with_cache_and_serialises_created(sdk_client, monkeypatch):
    import ll_core
    calls, linked = sdk_client
    monkeypatch.setenv("LUCIDLINK_CACHE_DATA_GB", "3")
    assert ll_core._list_filespaces_cached(TOKEN) == [
        {"id": "fid-1", "name": "fs1", "created": "2026-03-04T05:06:07Z"}]
    names = ll_core._with_fs(TOKEN, "fs1", lambda fs: (type(fs).__name__, [e.name for e in fs.read_dir("/")]))
    assert names == ("FileSystem", [])
    assert ("link", "fid-1", lucidlink.CacheConfig(data_size=3 * 1024**3, metadata_size=1024**3)) in calls
    stats = ll_core.registry_stats()
    assert stats["linked_filespaces"] == 1
    assert stats["limits"]["cache_bytes_per_link"] == {"data": 3 * 1024**3, "metadata": 1024**3}


def test_ll_core_typed_errors_inside_an_operation(sdk_client):
    import ll_core

    def boom(exc):
        def fn(_filespace):
            raise exc
        return fn

    for exc, status in [(llx.AuthenticationError(f"expired {TOKEN}"), 401),
                        (llx.AccessDeniedError("not a filespace admin"), 403),
                        (llx.RateLimitedError("slow down"), 429),
                        (llx.ServiceUnavailableError("down"), 503)]:
        with pytest.raises(HTTPException) as e:
            ll_core._with_filespace(TOKEN, "fs1", boom(exc))
        assert e.value.status_code == status
        assert TOKEN not in e.value.detail
    # An ordinary failure is left to the route.
    with pytest.raises(KeyError):
        ll_core._with_filespace(TOKEN, "fs1", boom(KeyError("x")))


def test_ll_core_login_failures_are_401_and_redacted(sdk_client):
    import ll_core
    for token in ("sa_live:k:revoked", "not-a-service-account-token"):
        with pytest.raises(HTTPException) as e:
            ll_core._list_filespaces_cached(token)
        assert e.value.status_code == 401
        assert token not in e.value.detail and "LucidLink auth failed" in e.value.detail


def test_auth_error_maps_sdk_types_and_redacts():
    import ll_core
    cases = [(llx.AuthenticationError, 401), (llx.NotLoggedInError, 401), (llx.AccessDeniedError, 403),
             (llx.FilespaceNotFoundError, 404), (llx.RateLimitedError, 429),
             (llx.ServiceUnavailableError, 503), (llx.OperationTimeoutError, 503),
             (llx.LucidLinkError, 502), (RuntimeError, 502)]
    for cls, status in cases:
        err = ll_core._auth_error(cls(f"failed for {TOKEN}"), TOKEN)
        assert err.status_code == status, cls
        assert TOKEN not in err.detail and "[redacted]" in err.detail


# ---------------------------------------------------------------- indexer source
def test_sdk_filespace_source_over_real_file_system():
    from indexer.source import SdkFilespaceSource
    fsp = make_filespace()
    nat = fsp.native
    f = nat.add_file("/docs/a.md", b"hello")
    src = SdkFilespaceSource(fsp.file_system)
    [d] = src.list_dir("/")
    assert (d.name, d.type) == ("docs", "dir") and d.file_id
    [e] = src.list_dir("/docs")
    assert (e.name, e.type, e.size, e.file_id, e.mtime) == ("a.md", "file", 5, f["id"], f["mtime"])
    st = src.stat("/docs/a.md")
    assert (st.file_id, st.mtime, st.size) == (e.file_id, e.mtime, 5)
    assert src.read_file("/docs/a.md") == b"hello"
    assert src.stat("/nope") is None
    nat.nodes["/link"] = nat._node("link")
    assert [x.type for x in src.list_dir("/") if x.name == "link"] == ["symlink"]


# ---------------------------------------------------------------- HTTP routes
@pytest.fixture
def routes(monkeypatch):
    """The app with the SDK boundary pointed at one fake filespace."""
    import connect_api
    import insights_api
    import lucidlink_api
    state = {"fsp": make_filespace(version=12)}

    def with_filespace(token, name, fn):
        return fn(state["fsp"])

    def with_fs(token, name, fn):
        return fn(state["fsp"].file_system)

    monkeypatch.setattr(connect_api, "_with_filespace", with_filespace)
    monkeypatch.setattr(lucidlink_api, "_with_fs", with_fs)
    monkeypatch.setattr(insights_api, "_with_fs", with_fs)
    return TestClient(lucidlink_api.app), state


def test_app_version_is_the_sdk_version(routes):
    client, _ = routes
    import lucidlink_api
    assert lucidlink_api.app.version == lucidlink.__version__ == "1.0.1"


def test_file_routes(routes):
    client, state = routes
    nat = state["fsp"].native
    assert client.post("/files/mkdir", params={"path": "/d"}, headers=H).status_code == 200
    assert client.post("/files/mkdir", params={"path": "/d"}, headers=H).status_code == 409
    assert client.post("/files/write", json={"path": "/d/a.txt", "content": "héllo"}, headers=H).json() == {"status": "ok"}
    assert client.get("/files/read", params={"path": "/d/a.txt"}, headers=H).json() == {"content": "héllo", "encoding": "utf-8"}
    assert client.get("/files", params={"path": "/d"}, headers=H).json() == [
        {"name": "a.txt", "is_dir": False, "size": 6, "path": "/d/a.txt"}]
    assert client.get("/files/stat", params={"path": "/d"}, headers=H).json() == {
        "name": "d", "size": 0, "is_dir": True, "is_file": False}
    assert client.get("/files/exists", params={"path": "/d/a.txt"}, headers=H).json() == {"exists": True}
    assert client.get("/files/exists", params={"path": "/zz"}, headers=H).json() == {"exists": False}
    assert client.get("/files/read", params={"path": "/zz"}, headers=H).status_code == 404
    r = client.get("/files/download", params={"path": "/d/a.txt"}, headers={**H, "Range": "bytes=1-2"})
    assert r.status_code == 206 and r.content == "héllo".encode()[1:3]
    assert r.headers["content-range"] == "bytes 1-2/6"
    assert client.get("/files/download", params={"path": "/d/a.txt"}, headers=H).content == "héllo".encode()
    up = client.post("/files/upload", data={"path": "/d/b.bin"}, files={"file": ("b", b"\x00\xff")}, headers=H)
    assert up.json() == {"status": "ok", "path": "/d/b.bin", "size": 2}
    assert client.post("/files/move", json={"src": "/d/b.bin", "dst": "/d/a.txt"}, headers=H).status_code == 409
    assert client.post("/files/move", json={"src": "/d/b.bin", "dst": "/d/c.bin"}, headers=H).status_code == 200
    assert client.delete("/files", params={"path": "/d"}, headers=H).status_code == 400          # a directory
    assert client.delete("/files", params={"path": "/d/none"}, headers=H).status_code == 404
    assert client.delete("/files/dir", params={"path": "/d", "recursive": "false"}, headers=H).status_code == 409
    assert "/d/a.txt" in nat.nodes                                  # nothing deleted by the refusal
    assert client.delete("/files/dir", params={"path": "/d"}, headers=H).json() == {"status": "ok"}
    assert [k for k in nat.nodes if k.startswith("/d")] == []


def test_stats_keeps_the_response_keys(routes):
    client, state = routes
    state["fsp"].native.add_file("/a", b"12345")
    body = client.get("/filespaces/fs1/stats", headers=H).json()
    assert body == {"sizeBytes": 5, "fileCount": None, "dirCount": None, "symlinkCount": None,
                    "externalFileCount": 0, "dataSize": None, "storageSize": 5, "entriesSize": None,
                    "externalFilesSize": 0, "entriesCount": 1}


def test_agent_context_reads_only_the_head(routes):
    import insights_api
    _, state = routes
    state["fsp"].native.add_file("/notes/a.md", b"x" * 5000)
    ctx, sources = insights_api._gather_context(state["fsp"].file_system, per_file_bytes=100)
    assert sources == ["/notes/a.md"] and ctx.count("x") == 100


def _add_store(client, name="s3a"):
    return client.post("/connect/datastores", headers=H, json={
        "name": name, "bucket": "bkt", "accessKeyId": "AKIAEXAMPLE", "secretAccessKey": "s3cr3t-value",
        "region": "us-east-1"})


def test_connect_data_stores(routes):
    client, state = routes
    assert client.get("/connect/datastores/available", headers=H).json() == {"available": True}
    r = _add_store(client)
    assert r.status_code == 200, r.text
    store = r.json()
    assert "s3cr3t-value" not in r.text
    assert store == {"name": "s3a", "kind": "s3", "endpoint": "", "bucket": "bkt", "region": "us-east-1",
                     "keyId": None, "accessKey": "AKIA…", "urlExpirationMinutes": 10080,
                     "useVirtualAddressing": False, "rekeyState": "no_rekey"}
    assert state["fsp"].syncs                                       # synced so count() works
    assert _add_store(client).status_code == 409                   # exists
    listed = client.get("/connect/datastores", headers=H).json()
    assert listed["available"] is True
    assert listed["dataStores"] == [{**store, "externalFileCount": 0}]
    assert "s3cr3t" not in str(listed)
    r = client.post("/connect/datastores/s3a/rekey", headers=H,
                    json={"accessKeyId": "AKIANEW", "secretAccessKey": "new-secret"})
    assert r.json() == {"status": "ok"}
    assert state["fsp"].native_connect.rekeys == [("s3a", "AKIANEW")]
    assert client.post("/connect/datastores/nope/rekey", headers=H,
                       json={"accessKeyId": "a", "secretAccessKey": "b"}).status_code == 404
    assert client.delete("/connect/datastores/nope", headers=H).status_code == 404
    assert client.delete("/connect/datastores/s3a", headers=H).json() == {"status": "ok"}


def test_connect_link_page_and_unlink(routes):
    client, state = routes
    nat = state["fsp"].native
    nat.add_file("/regular.txt", b"keep me")
    nat.nodes["/media"] = nat._node("dir")
    _add_store(client)
    for i in range(5):
        r = client.post("/connect/link", headers=H, json={
            "dataStore": "s3a", "objectKey": f"k{i}", "filePath": f"/media/o{i}.bin",
            **({"size": 10, "checksum": "abc"} if i % 2 else {})})
        assert r.json() == {"status": "ok", "filePath": f"/media/o{i}.bin"}
    assert client.post("/connect/link", headers=H, json={
        "dataStore": "s3a", "objectKey": "k", "filePath": "/media/o0.bin"}).status_code == 409
    assert client.post("/connect/link", headers=H, json={
        "dataStore": "s3a", "objectKey": "k", "filePath": "/nodir/x.bin"}).status_code == 404
    assert client.post("/connect/link", headers=H, json={
        "dataStore": "ghost", "objectKey": "k", "filePath": "/media/x.bin"}).status_code == 404

    pages, cursor = [], ""
    while True:
        body = client.get("/connect/external-files", headers=H,
                          params={"dataStore": "s3a", "limit": 2, "cursor": cursor}).json()
        pages.append([f["filePath"] for f in body["files"]])
        assert all(f["source"] == "s3a" and f["fileId"] for f in body["files"])
        if not body["hasMore"]:
            assert body["cursor"] == ""
            break
        cursor = body["cursor"]
    assert pages == [["/media/o0.bin", "/media/o1.bin"], ["/media/o2.bin", "/media/o3.bin"], ["/media/o4.bin"]]
    assert client.get("/connect/external-files", headers=H,
                      params={"dataStore": "s3a", "cursor": "x"}).status_code == 400
    assert client.get("/connect/datastores", headers=H).json()["dataStores"][0]["externalFileCount"] == 5
    assert client.delete("/connect/datastores/s3a", headers=H).status_code == 409   # in use

    # Deletion safety: /connect/link never deletes a regular file or a directory.
    for path in ("/regular.txt", "/media"):
        r = client.request("DELETE", "/connect/link", headers=H, json={"filePath": path})
        assert r.status_code == 400, path
    assert "/regular.txt" in nat.nodes and "/media" in nat.nodes
    assert client.request("DELETE", "/connect/link", headers=H, json={"filePath": "/gone"}).status_code == 404
    r = client.request("DELETE", "/connect/link", headers=H, json={"filePath": "/media/o0.bin"})
    assert r.json() == {"status": "ok"} and "/media/o0.bin" not in nat.nodes


def test_connect_link_url_on_3_8_uses_an_http_link_store(routes):
    client, state = routes
    state["fsp"].native.nodes["/web"] = state["fsp"].native._node("dir")
    r = client.post("/connect/link-url", headers=H, json={"url": "https://e.x/a", "filePath": "/web/a"})
    assert r.json() == {"status": "ok", "filePath": "/web/a"}
    stores = client.get("/connect/datastores", headers=H).json()["dataStores"]
    assert [(s["name"], s["kind"], s["bucket"], s["accessKey"]) for s in stores] == [
        ("http-links", "http_link", None, None)]
    r = client.post("/connect/link-url", headers=H, json={"url": "https://e.x/b", "filePath": "/web/b"})
    assert r.status_code == 200                                     # reuses the store
    assert client.post("/connect/link-url", headers=H, json={
        "url": "https://e.x/c", "filePath": "/web/c", "dataStore": "other"}).status_code == 404
    _add_store(client)
    assert client.post("/connect/link-url", headers=H, json={
        "url": "https://e.x/c", "filePath": "/web/c", "dataStore": "s3a"}).status_code == 400
    r = client.request("DELETE", "/connect/link", headers=H, json={"filePath": "/web/a"})
    assert r.json() == {"status": "ok"}


def test_connect_link_url_on_3_6_uses_http_connect_3_6(routes):
    client, state = routes
    state["fsp"] = fsp = make_filespace(version=10)
    fsp.native.nodes["/web"] = fsp.native._node("dir")
    r = client.post("/connect/link-url", headers=H, json={"url": "https://e.x/a", "filePath": "/web/a"})
    assert r.json() == {"status": "ok", "filePath": "/web/a"}
    assert fsp.native.nodes["/web/a"]["external"] == {"kind": "HttpLinkDataStore", "store": None,
                                                       "url": "https://e.x/a", "size": None}
    assert fsp.native_connect.stores == {}
    # No store owns a 3.6 HTTP link, so /connect/link refuses it (documented).
    assert client.request("DELETE", "/connect/link", headers=H, json={"filePath": "/web/a"}).status_code == 400


def test_connect_unavailable(routes):
    client, state = routes
    state["fsp"] = make_filespace(connect_available=False)
    assert client.get("/connect/datastores/available", headers=H).json() == {"available": False}
    assert client.get("/connect/datastores", headers=H).json() == {"available": False, "dataStores": []}
    r = _add_store(client)
    assert r.status_code == 409 and "not available" in r.json()["detail"]
