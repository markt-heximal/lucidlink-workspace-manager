"""Settings from the vault, and the SDK source provider's lease/LRU logic
(against SDK 1.0.1 with `Client` replaced and a native fake under the real
`FileSystem`; no LucidLink service is reached)."""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

import pytest

from conftest import para, run, src
from sdk_fake import make_fs

from indexer.source import SdkSourceProvider, StaticSettings, VaultSettings, configured_filespaces


def test_configured_filespaces_merges_and_dedupes():
    assert configured_filespaces({"filespace": "a", "filespaces": ["b", "a"]}) == ["b", "a"]
    assert configured_filespaces({"filespace": "", "filespaces": None}) == []


def test_vault_error_is_recorded_not_fatal(make_service, monkeypatch):
    fake = types.ModuleType("vault")

    class VaultError(RuntimeError):
        pass

    def broken(*a, **k):
        raise VaultError("VAULT_KEY is not set")

    fake.VaultError = VaultError
    fake.load = fake.workspaces = fake.token_for = broken
    monkeypatch.setitem(sys.modules, "vault", fake)

    svc = make_service()
    svc.settings = VaultSettings()
    svc.scheduler._targets = []
    st = svc.status()
    assert st["filespaces"] == []
    assert st["settings"]["ok"] is False and "VAULT_KEY" in st["settings"]["error"]
    assert svc.search(None, None, "anything") == []


def test_vault_settings_reads_vault_module(monkeypatch):
    fake = types.ModuleType("vault")
    payload = {"workspaces": [{"id": "w", "token": "t", "filespace": "f", "filespaces": []}],
               "activeWorkspaceId": "w"}
    fake.load = lambda: payload
    fake.workspaces = lambda: payload["workspaces"]
    fake.token_for = lambda wid: "t" if wid in (None, "w") else None
    monkeypatch.setitem(sys.modules, "vault", fake)
    s = VaultSettings()
    assert s.active_workspace_id() == "w" and s.token_for("w") == "t" and s.workspaces()[0]["id"] == "w"


TOKEN = "sa_live:key:secret"


class _FakeFilespace:
    """SDK 1.0-shaped Filespace: `.file_system` is a real lucidlink.FileSystem
    over the native fake, with one file /a.md."""

    def __init__(self, fid, log):
        self.file_system = make_fs()
        self.file_system._native.add_file("/a.md", b"abc")
        self._fid = fid
        self._log = log

    def unlink(self):
        self._log.append(("unlink", self._fid))


@pytest.fixture
def fake_sdk(monkeypatch):
    import lucidlink
    log: list = []

    class Workspace:
        def list_filespaces(self):
            return [lucidlink.FilespaceInfo(id=f"id-{n}", name=n,
                                            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
                    for n in ("a", "b", "c")]

        def link_filespace(self, filespace_id, *, cache=None):
            log.append(("link", filespace_id))
            log.append(("cache", cache))
            return _FakeFilespace(filespace_id, log)

    class Client:
        def login(self, creds):
            log.append(("login", creds.token))

        @property
        def workspace(self):
            return Workspace()

        def close(self):
            log.append(("close",))

    monkeypatch.setattr(lucidlink, "Client", Client)
    return log


def test_sdk_provider_links_by_id_and_maps_entries(fake_sdk, monkeypatch):
    import lucidlink
    monkeypatch.setenv("LUCIDLINK_CACHE_DATA_GB", "2")
    monkeypatch.delenv("LUCIDLINK_CACHE_METADATA_GB", raising=False)
    p = SdkSourceProvider(StaticSettings({"workspaces": [{"id": "w", "token": TOKEN}]}), max_links=1)
    with p.lease("w", "a") as s:
        [e] = s.list_dir("/")
        assert (e.name, e.type, e.size) == ("a.md", "file", 3)
        assert e.file_id and e.mtime > 1_700_000_000 * 10**9       # FsEntry.id, modified_at (ns)
        assert s.stat("/a.md").file_id == e.file_id
        assert s.read_file("/a.md") == b"abc"
        assert s.stat("/missing") is None
    assert ("link", "id-a") in fake_sdk and ("login", TOKEN) in fake_sdk
    assert ("cache", lucidlink.CacheConfig(data_size=2 * 1024**3, metadata_size=1024**3)) in fake_sdk


def test_sdk_provider_never_evicts_a_leased_filespace(fake_sdk):
    p = SdkSourceProvider(StaticSettings({"workspaces": [{"id": "w", "token": TOKEN}]}), max_links=1)
    with p.lease("w", "a"):
        with p.lease("w", "b"):
            assert ("unlink", "id-a") not in fake_sdk      # pinned: over the bound, not evicted
    with p.lease("w", "c"):
        pass
    assert ("unlink", "id-a") in fake_sdk and ("unlink", "id-b") in fake_sdk
    p.close()
    assert ("unlink", "id-c") in fake_sdk and ("close",) in fake_sdk


def test_one_client_is_never_used_from_two_threads_at_once(fake_sdk):
    """SDK 1.0: a Client and everything obtained from it is not thread safe.
    Two jobs on different filespaces of one workspace take turns; two
    workspaces (two Clients) still overlap."""
    import threading
    import time as _time
    settings = StaticSettings({"workspaces": [{"id": "w", "token": TOKEN},
                                              {"id": "v", "token": TOKEN + "2"}]})
    p = SdkSourceProvider(settings, max_links=2)
    active: dict = {}
    peak: dict = {}
    guard = threading.Lock()

    def job(ws, fs):
        with p.lease(ws, fs) as s:
            with guard:
                active[ws] = active.get(ws, 0) + 1
                peak[ws] = max(peak.get(ws, 0), active[ws])
                peak["all"] = max(peak.get("all", 0), sum(active.values()))
            s.list_dir("/")
            _time.sleep(0.05)
            with guard:
                active[ws] -= 1

    threads = [threading.Thread(target=job, args=a) for a in
               [("w", "a"), ("w", "b"), ("w", "a"), ("v", "a"), ("v", "b")]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak["w"] == 1 and peak["v"] == 1
    assert peak["all"] == 2
    p.close()


def test_login_failure_is_recorded_without_the_token(make_service, monkeypatch):
    import lucidlink
    from lucidlink import exceptions as llx

    class Client:
        def login(self, creds):
            raise llx.AuthenticationError(f"token {creds.token} revoked")

        def close(self):
            pass

    monkeypatch.setattr(lucidlink, "Client", Client)
    svc = make_service()
    svc.sources = SdkSourceProvider(StaticSettings({"workspaces": [{"id": "ws1", "token": TOKEN}]}))
    run(svc)
    err = svc.filespace_status("ws1", "fsA")["lastError"]
    assert "could not log in" in err and TOKEN not in err and "[redacted]" in err


def test_missing_token_records_error(make_service):
    from indexer.source import SdkSourceProvider as P
    svc = make_service()
    svc.sources = P(StaticSettings({"workspaces": [{"id": "ws1", "token": ""}]}))
    run(svc)
    assert "no LucidLink token" in svc.filespace_status("ws1", "fsA")["lastError"]


def test_status_lists_overridden_extra_filespace(make_service):
    svc = make_service()
    svc.set_enabled("ws1", "extra", True)
    names = [f["filespace"] for f in svc.status()["filespaces"]]
    assert names == ["fsA", "fsB", "extra"]
    src(svc, "extra").write("/x.md", para("extra"))
    run(svc, "extra")
    assert svc.filespace_status("ws1", "extra")["files"]["indexed"] == 1
