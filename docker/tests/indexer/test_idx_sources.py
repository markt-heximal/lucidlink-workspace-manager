"""Settings from the vault, and the SDK source provider's lease/LRU logic
(against a fake `lucidlink` module; the live SDK is not exercised here)."""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from conftest import para, run, src

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


class _FakeFs:
    def __init__(self, name):
        self.name = name

    def read_dir(self, path):
        return [SimpleNamespace(name="a.md", size=3, type="file", file_id=42, file_id_external=1,
                                ctime=1, mtime=1700000000)]

    def read_file(self, path):
        return b"abc"

    def get_entry(self, path):
        raise FileNotFoundError(path)


class _FakeFilespace:
    def __init__(self, fid, log):
        self.fs = _FakeFs(fid)
        self._fid = fid
        self._log = log

    def unlink(self):
        self._log.append(("unlink", self._fid))


@pytest.fixture
def fake_sdk(monkeypatch):
    log: list = []

    class Workspace:
        def list_filespaces(self):
            return [SimpleNamespace(id=f"id-{n}", name=n) for n in ("a", "b", "c")]

        def link_filespace(self, id):
            log.append(("link", id))
            return _FakeFilespace(id, log)

    class Client:
        def login(self, creds):
            log.append(("login", creds.token))

        def list_workspaces(self):
            return [SimpleNamespace(id="w")]

        def get_workspace(self, wid):
            return Workspace()

        def close(self):
            log.append(("close",))

    mod = types.ModuleType("lucidlink")
    mod.Client = Client
    mod.ServiceAccountCredentials = lambda token: SimpleNamespace(token=token)
    monkeypatch.setitem(sys.modules, "lucidlink", mod)
    return log


def test_sdk_provider_links_by_id_and_maps_entries(fake_sdk):
    p = SdkSourceProvider(StaticSettings({"workspaces": [{"id": "w", "token": "tok"}]}), max_links=1)
    with p.lease("w", "a") as s:
        [e] = s.list_dir("/")
        assert (e.name, e.type, e.size, e.mtime, e.file_id) == ("a.md", "file", 3, 1700000000, "42")
        assert s.read_file("/a.md") == b"abc"
        assert s.stat("/missing") is None
    assert ("link", "id-a") in fake_sdk and ("login", "tok") in fake_sdk


def test_sdk_provider_never_evicts_a_leased_filespace(fake_sdk):
    p = SdkSourceProvider(StaticSettings({"workspaces": [{"id": "w", "token": "tok"}]}), max_links=1)
    with p.lease("w", "a"):
        with p.lease("w", "b"):
            assert ("unlink", "id-a") not in fake_sdk      # pinned: over the bound, not evicted
    with p.lease("w", "c"):
        pass
    assert ("unlink", "id-a") in fake_sdk and ("unlink", "id-b") in fake_sdk
    p.close()
    assert ("unlink", "id-c") in fake_sdk and ("close",) in fake_sdk


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
