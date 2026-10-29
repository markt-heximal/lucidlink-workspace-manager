"""Cloud storage through the engine computer's sync apps: provider detection,
online-only placeholders skipped unless downloading is allowed, cloud prompts."""
from __future__ import annotations

import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from conftest import KEY, para

from indexer import block
from indexer.app import create_app
from indexer.folders import provider_of

H = {"X-Internal-Key": KEY}


@contextmanager
def client(svc):
    with TestClient(create_app(lambda: svc, start_scheduler=False)) as c:
        yield c


@pytest.mark.parametrize("folder,expected", [
    ("/GoogleDrive-mark@heximal.com", {"id": "google-drive", "label": "Google Drive", "account": "mark@heximal.com"}),
    ("/GoogleDrive-mark@heximal.com/My Drive/Board", {"id": "google-drive", "label": "Google Drive",
                                                       "account": "mark@heximal.com"}),
    ("/Dropbox", {"id": "dropbox", "label": "Dropbox", "account": None}),
    ("/Dropbox-Heximal", {"id": "dropbox", "label": "Dropbox", "account": "Heximal"}),
    ("/Dropbox (Personal)", {"id": "dropbox", "label": "Dropbox", "account": "Personal"}),
    ("/OneDrive-Personal", {"id": "onedrive", "label": "OneDrive", "account": "Personal"}),
    ("/Box-Box", {"id": "box", "label": "Box", "account": None}),
    ("/Something", {"id": "other", "label": "Something", "account": None}),
])
def test_provider_of(folder, expected):
    assert provider_of("cloud", folder) == expected


def test_provider_only_in_the_cloud_root():
    assert provider_of("volumes", "/Dropbox") is None and provider_of("cloud", "/") is None


@pytest.fixture
def cloud(tmp_path):
    base = tmp_path / "host"
    gd = base / "cloud" / "GoogleDrive-mark@heximal.com" / "My Drive"
    gd.mkdir(parents=True)
    (gd / "local.md").write_text(para("downloaded board notes"))
    (gd / "online.md").write_text(para("online only budget"))
    (gd / "Plan.gdoc").write_text('{"url": "https://docs.google.com/x"}')
    (base / "cloud" / "Dropbox").mkdir()
    return {"base": base, "gd": gd}


@pytest.fixture
def placeholders(monkeypatch, cloud):
    """Pretend online.md is a sync app placeholder (size, no blocks)."""
    real = block.online_only
    monkeypatch.setattr(block, "online_only",
                        lambda st: st.st_ino == os.stat(cloud["gd"] / "online.md").st_ino or real(st))


def test_cloud_folder_skips_online_only_until_allowed(make_service, cloud, placeholders):
    svc = make_service(block_root_base=str(cloud["base"]), block_source_workspace="",
                       block_root_hosts={"cloud": "/Users/m/Library/CloudStorage"})
    with client(svc) as c:
        roots = {r["id"]: r for r in c.get("/index/folders", headers=H).json()["roots"]}
        assert roots["cloud"]["available"] and roots["cloud"]["hostPath"] == "/Users/m/Library/CloudStorage"
        top = c.get("/index/folders/browse", params={"root": "cloud"}, headers=H).json()["entries"]
        assert [(e["name"], e["provider"]["id"]) for e in top] == [("Dropbox", "dropbox"),
                                                                   ("GoogleDrive-mark@heximal.com", "google-drive")]
        src = c.post("/index/folders", json={"root": "cloud", "path": "/GoogleDrive-mark@heximal.com"},
                     headers=H).json()
        assert src["name"] == "Google Drive (mark)" and src["downloadOnlineOnly"] is False
        assert src["provider"] == {"id": "google-drive", "label": "Google Drive", "account": "mark@heximal.com"}
        name = src["name"]

        svc.scheduler.run_job("local", name, full=True)
        files = {r["path"]: r for r in svc.store.query(
            "SELECT path, status, reason FROM files WHERE filespace=?", (name,))}
        assert files["/My Drive/local.md"]["status"] == "indexed"
        assert files["/My Drive/online.md"]["status"] == "skipped"
        assert files["/My Drive/online.md"]["reason"] == block.ONLINE_ONLY
        assert files["/My Drive/Plan.gdoc"]["reason"].startswith("unsupported type")
        listing = c.get("/index/folders/list", params={"name": name, "path": "/My Drive"}, headers=H).json()
        by = {e["name"]: e for e in listing["entries"]}
        assert by["online.md"]["status"] == "skipped" and by["online.md"]["reason"] == block.ONLINE_ONLY
        assert by["local.md"]["reason"] is None

        # Allow downloading: the skipped files go back to the queue and are read.
        r = c.post("/index/folders/options", json={"name": name, "downloadOnlineOnly": True}, headers=H).json()
        assert r["downloadOnlineOnly"] is True
        svc.scheduler.run_job("local", name, full=False)
        status = svc.store.one("SELECT status FROM files WHERE filespace=? AND path='/My Drive/online.md'", (name,))
        assert status["status"] == "indexed"
        assert c.post("/index/folders/options", json={"name": "nope", "downloadOnlineOnly": True},
                      headers=H).status_code == 404


def test_other_roots_never_skip_online_only(make_service, tmp_path, monkeypatch):
    share = tmp_path / "share"
    share.mkdir()
    (share / "a.md").write_text(para("nas file"))
    monkeypatch.setattr(block, "online_only", lambda st: True)
    svc = make_service(block_sources=f"NAS={share}", block_source_workspace="")
    svc.scheduler.run_job("local", "NAS", full=True)
    assert svc.store.one("SELECT status FROM files WHERE filespace='NAS'")["status"] == "indexed"
    with client(svc) as c:
        assert c.post("/index/folders/options", json={"name": "NAS", "downloadOnlineOnly": True},
                      headers=H).status_code == 400


def test_new_cloud_account_is_offered(make_service, cloud):
    svc = make_service(block_root_base=str(cloud["base"]), block_source_workspace="",
                       block_root_hosts={"cloud": "/Users/m/Library/CloudStorage"})
    with client(svc) as c:
        assert c.get("/index/folders/prompts", headers=H).json()["newDrives"] == []
        (cloud["base"] / "cloud" / "OneDrive-Personal").mkdir()
        new = c.get("/index/folders/prompts", headers=H).json()["newDrives"]
        assert [(d["root"], d["name"], d["provider"]["id"]) for d in new] == [("cloud", "OneDrive-Personal", "onedrive")]
        assert c.post("/index/folders/prompts/dismiss", json={"kind": "new", "root": "cloud",
                                                              "path": "/OneDrive-Personal"}, headers=H).status_code == 200
        assert c.get("/index/folders/prompts", headers=H).json()["newDrives"] == []
