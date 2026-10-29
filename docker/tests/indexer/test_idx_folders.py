"""Folders from the app: browse the roots, connect and remove block storage at runtime."""
from __future__ import annotations

import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from conftest import KEY, para

from indexer.app import create_app

H = {"X-Internal-Key": KEY}


@contextmanager
def client(svc, restarts=None):
    with TestClient(create_app(lambda: svc, start_scheduler=False,
                               restart=lambda: restarts.append(1) if restarts is not None else None)) as c:
        yield c


@pytest.fixture
def roots(tmp_path):
    """/host/volumes with two shares (one also a .env slot), iCloud with a folder, Desktop missing."""
    base = tmp_path / "host"
    vols = base / "volumes"
    (vols / "Projects" / "Clients" / "Northwind").mkdir(parents=True)
    (vols / "Projects" / "Clients" / "Northwind" / "brief.md").write_text(para("northwind brief"))
    (vols / "Projects" / ".hidden").mkdir()
    (vols / "Projects" / "@eaDir").mkdir()
    (vols / "Archive").mkdir()
    (vols / "Archive" / "old.md").write_text(para("archive"))
    os.symlink("/", vols / "Macintosh HD")
    (vols / "com.apple.TimeMachine.localsnapshots").mkdir()
    (base / "icloud" / "Notes").mkdir(parents=True)
    (base / "desktop").mkdir()
    (base / "desktop" / ".gitkeep").write_text("")
    slot = tmp_path / "slot1"
    slot.mkdir()
    (slot / "a.md").write_text(para("slot"))
    return {"base": base, "vols": vols, "slot": slot}


@pytest.fixture
def svc(make_service, roots):
    return make_service(block_root_base=str(roots["base"]),
                        block_root_hosts={"volumes": "/Volumes", "icloud": "/Users/m/iCloud", "desktop": ""},
                        block_sources=f"Archive-old={roots['slot']}",
                        block_slot_hosts={str(roots["slot"]): "/Volumes/Archive"})


def test_roots_and_browse(svc):
    with client(svc) as c:
        body = c.get("/index/folders", headers=H).json()
        assert body["roots"] == [
            {"id": "volumes", "label": "Drives and network shares", "hostPath": "/Volumes", "available": True},
            {"id": "icloud", "label": "iCloud Drive", "hostPath": "/Users/m/iCloud", "available": True},
            {"id": "desktop", "label": "Desktop", "hostPath": None, "available": False},
            {"id": "documents", "label": "Documents", "hostPath": None, "available": False},
        ]
        assert [s["name"] for s in body["sources"]] == ["Archive-old"]
        assert body["sources"][0]["origin"] == "mini" and body["sources"][0]["hostPath"] == "/Volumes/Archive"

        top = c.get("/index/folders/browse", params={"root": "volumes"}, headers=H).json()
        # No symlink to "/", no dot-names, no system folders; the .env slot's host folder shows as connected.
        assert [(e["name"], e["connected"]) for e in top["entries"]] == [("Archive", "Archive-old"), ("Projects", None)]
        assert top["parent"] is None and top["entries"][1]["hostPath"] == "/Volumes/Projects"
        sub = c.get("/index/folders/browse", params={"root": "volumes", "path": "/Projects"}, headers=H).json()
        assert [e["path"] for e in sub["entries"]] == ["/Projects/Clients"] and sub["parent"] == "/"

        assert c.get("/index/folders/browse", params={"root": "nope"}, headers=H).status_code == 404
        assert c.get("/index/folders/browse", params={"root": "desktop"}, headers=H).status_code == 503
        assert c.get("/index/folders/browse", params={"root": "volumes", "path": "/../etc"}, headers=H).status_code == 400
        assert c.get("/index/folders/browse", params={"root": "volumes", "path": "/Macintosh HD"},
                     headers=H).status_code == 400
        assert c.get("/index/folders", headers={"X-Internal-Key": "wrong"}).status_code == 401


def test_connect_index_and_remove(svc):
    with client(svc) as c:
        r = c.post("/index/folders", json={"root": "volumes", "path": "/Projects/Clients"}, headers=H)
        assert r.status_code == 201, r.text
        src = r.json()
        assert src["name"] == "Clients" and src["origin"] == "app" and src["accepted"] is True
        assert src["hostPath"] == "/Volumes/Projects/Clients" and src["root"] == "volumes"
        assert src["folder"] == "/Projects/Clients"
        assert ("ws1", "Clients") in svc.scheduler.targets()

        svc.scheduler.run_job("ws1", "Clients", full=True)
        hits = c.post("/index/search", json={"query": "northwind brief"}, headers=H).json()["results"]
        assert any(h["filespace"] == "Clients" and h["kind"] == "block" for h in hits)

        # A second service on the same database keeps the folder (it is stored, not in .env).
        listed = c.get("/index/folders", headers=H).json()["sources"]
        assert [s["name"] for s in listed] == ["Archive-old", "Clients"]

        gone = c.post("/index/folders/remove", json={"name": "Clients"}, headers=H).json()
        assert gone["removed"] == "Clients" and gone["files"] >= 1 and gone["chunks"] >= 1
        assert ("ws1", "Clients") not in svc.scheduler.targets()
        assert svc.store.chunk_count("ws1", "Clients") == 0
        hits = c.post("/index/search", json={"query": "northwind brief"}, headers=H).json()["results"]
        assert not any(h["filespace"] == "Clients" for h in hits)
        assert c.post("/index/folders/remove", json={"name": "Clients"}, headers=H).status_code == 404


def test_refusals(svc):
    with client(svc) as c:
        def add(**body):
            return c.post("/index/folders", json=body, headers=H)

        # Same host folder as the .env slot, and a folder inside it.
        r = add(root="volumes", path="/Archive")
        assert r.status_code == 409 and "already connected as Archive-old" in r.json()["detail"]
        assert add(root="volumes", path="/Projects", name="P").status_code == 201
        r = add(root="volumes", path="/Projects/Clients")
        assert r.status_code == 409 and "inside P" in r.json()["detail"]
        r = add(root="volumes", path="/", name="All")
        assert r.status_code == 409 and "inside that folder" in r.json()["detail"]
        r = add(root="icloud", path="/Notes", name="P")
        assert r.status_code == 409 and "already a folder called P" in r.json()["detail"]
        r = add(root="icloud", path="/Notes", name="fsA")
        assert r.status_code == 409 and "LucidLink filespace" in r.json()["detail"]
        assert add(root="icloud", path="/Notes", name="a;b").status_code == 400
        assert add(root="icloud", path="/Missing").status_code == 400
        assert add(root="desktop", path="/").status_code == 503
        assert add(root="icloud", path="/Notes").json()["name"] == "Notes"


def test_remove_an_env_source_is_remembered(svc, make_service, roots, tmp_path):
    with client(svc) as c:
        svc.scheduler.run_job("ws1", "Archive-old", full=True)
        assert svc.store.chunk_count("ws1", "Archive-old") > 0
        r = c.post("/index/folders/remove", json={"name": "Archive-old"}, headers=H).json()
        assert r["removed"] == "Archive-old" and r["chunks"] > 0
        assert ("ws1", "Archive-old") not in svc.scheduler.targets()
        # Now the same Mini folder can be connected through the root.
        assert c.post("/index/folders", json={"root": "volumes", "path": "/Archive"}, headers=H).status_code == 201

    # The removal survives a restart (same database, same .env).
    again = make_service(db_path=svc.config.db_path, block_root_base=str(roots["base"]),
                         block_root_hosts={"volumes": "/Volumes", "icloud": "", "desktop": ""},
                         block_sources=f"Archive-old={roots['slot']}",
                         block_slot_hosts={str(roots["slot"]): "/Volumes/Archive"})
    names = {k[1] for k in again.scheduler.targets()}
    assert "Archive-old" not in names and "Archive" in names


def test_restart_endpoint(svc):
    restarts: list = []
    with client(svc, restarts) as c:
        r = c.post("/index/folders/restart", headers=H)
        assert r.status_code == 202 and r.json() == {"restarting": True}
    assert restarts == [1]


def test_new_drive_prompts(svc, roots):
    with client(svc) as c:
        # First look: what is mounted now is known, not new.
        assert c.get("/index/folders/prompts", headers=H).json()["newDrives"] == []
        (roots["vols"] / "Archive 3").mkdir()
        (roots["vols"] / "Archive 3" / "x.md").write_text("x")
        (roots["vols"] / "Camera").mkdir()
        new = c.get("/index/folders/prompts", headers=H).json()["newDrives"]
        assert [(d["name"], d["path"], d["hostPath"], d["suggestedName"]) for d in new] == [
            ("Archive 3", "/Archive 3", "/Volumes/Archive 3", "Archive-3"), ("Camera", "/Camera", "/Volumes/Camera", "Camera")]
        assert all(d["root"] == "volumes" and d["firstSeenAt"] for d in new)

        assert c.post("/index/folders/prompts/dismiss", json={"kind": "new", "path": "/Camera"}, headers=H).json() == {
            "dismissed": True}
        # Connecting a folder inside a new drive answers its prompt too.
        assert c.post("/index/folders", json={"root": "volumes", "path": "/Archive 3"}, headers=H).status_code == 201
        assert c.get("/index/folders/prompts", headers=H).json()["newDrives"] == []
        # Unmounted and back again: still not new.
        os.rename(roots["vols"] / "Camera", roots["base"] / "Camera-away")
        c.get("/index/folders/prompts", headers=H)
        os.rename(roots["base"] / "Camera-away", roots["vols"] / "Camera")
        assert c.get("/index/folders/prompts", headers=H).json()["newDrives"] == []
        # Drives that were there at the first look can't be dismissed (there is no prompt).
        assert c.post("/index/folders/prompts/dismiss", json={"kind": "new", "path": "/Projects"},
                      headers=H).status_code == 404


def test_missing_source_prompts(make_service, roots):
    svc = make_service(block_root_base=str(roots["base"]),
                       block_root_hosts={"volumes": "/Volumes", "icloud": "", "desktop": ""},
                       block_sources=f"Archive-old={roots['slot']}", block_slot_hosts={},
                       folder_missing_grace_sec=0.0)
    with client(svc) as c:
        assert c.post("/index/folders", json={"root": "volumes", "path": "/Projects"}, headers=H).status_code == 201
        assert c.get("/index/folders/prompts", headers=H).json()["missing"] == []
        os.rename(roots["vols"] / "Projects", roots["base"] / "Projects-away")
        missing = c.get("/index/folders/prompts", headers=H).json()["missing"]
        assert [(m["name"], m["origin"], m["hostPath"], m["root"]) for m in missing] == [
            ("Projects", "app", "/Volumes/Projects", "volumes")]
        since = missing[0]["missingSince"]
        assert c.get("/index/folders/prompts", headers=H).json()["missing"][0]["missingSince"] == since
        # Keep it: hidden while it stays missing...
        assert c.post("/index/folders/prompts/dismiss", json={"kind": "missing", "name": "Projects"},
                      headers=H).status_code == 200
        assert c.get("/index/folders/prompts", headers=H).json()["missing"] == []
        # ...and asked again after it comes back and goes missing again.
        os.rename(roots["base"] / "Projects-away", roots["vols"] / "Projects")
        assert c.get("/index/folders/prompts", headers=H).json()["missing"] == []
        os.rename(roots["vols"] / "Projects", roots["base"] / "Projects-away")
        assert [m["name"] for m in c.get("/index/folders/prompts", headers=H).json()["missing"]] == ["Projects"]
        # Forget it = the normal remove.
        assert c.post("/index/folders/remove", json={"name": "Projects"}, headers=H).status_code == 200
        assert c.get("/index/folders/prompts", headers=H).json()["missing"] == []
        assert c.post("/index/folders/prompts/dismiss", json={"kind": "missing", "name": "Nope"},
                      headers=H).status_code == 404


def test_missing_waits_for_the_grace_period(svc, roots):
    # Default grace (15 min): a share that just dropped is not reported yet.
    import shutil
    with client(svc) as c:
        shutil.rmtree(roots["slot"])
        assert c.get("/index/folders/prompts", headers=H).json()["missing"] == []
        assert svc.store.missing_row("Archive-old") is not None
