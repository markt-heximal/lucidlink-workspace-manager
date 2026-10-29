"""Connected block storage end to end: a real directory indexed as a target of
kind "block" next to the in-memory filespaces."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from conftest import KEY, para, run, src

from indexer.app import create_app
from indexer.block import NOT_MOUNTED, LocalSource, path_file_id

H = {"X-Internal-Key": KEY}


@contextmanager
def client(svc):
    with TestClient(create_app(lambda: svc, start_scheduler=False)) as c:
        yield c


def write(root, rel: str, text: str, mtime: float | None = None):
    p = root / rel.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def ledger(svc, fs="NAS", ws="ws1"):
    return {r["path"]: r for r in svc.store.query(
        "SELECT * FROM files WHERE workspace_id=? AND filespace=? AND status != 'deleted'", (ws, fs))}


@pytest.fixture
def share(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    return root


@pytest.fixture
def block(make_service, share):
    def build(**cfg):
        cfg.setdefault("block_sources", f"NAS={share}")
        return make_service(**cfg)
    return build


@pytest.fixture
def reads(monkeypatch):
    seen: list[str] = []
    orig = LocalSource.read_file

    def read(self, path):
        seen.append(path)
        return orig(self, path)

    monkeypatch.setattr(LocalSource, "read_file", read)
    return seen


def test_block_source_is_a_target_of_kind_block(block, share):
    svc = block()
    assert svc.scheduler.targets() == [("ws1", "fsA"), ("ws1", "fsB"), ("ws1", "NAS")]
    assert svc.store.filespace_row("ws1", "NAS")["kind"] == "block"
    st = svc.status()
    kinds = {f["filespace"]: f["kind"] for f in st["filespaces"]}
    assert kinds == {"fsA": "lucidlink", "fsB": "lucidlink", "NAS": "block"}
    assert st["blockSources"] == [{"name": "NAS", "workspaceId": "ws1", "path": str(share), "kind": "block",
                                   "accepted": True, "error": None, "origin": "engine",
                                   "hostPath": None, "root": None, "folder": None}]


def test_block_workspace_from_config_or_local(block, make_service, share):
    svc = block(block_source_workspace="studio")
    assert ("studio", "NAS") in svc.scheduler.targets()
    # Unset: the engine computer's own workspace, whatever the vault's active workspace is.
    svc = block(block_source_workspace="")
    assert ("local", "NAS") in svc.scheduler.targets()
    svc.settings.payload["activeWorkspaceId"] = None
    svc.scheduler.refresh_targets(force=True)
    assert ("local", "NAS") in svc.scheduler.targets()


def test_index_tree_then_unchanged_rescan_reads_nothing(block, share, reads):
    write(share, "/Clients/Harbor/brief.md", para("harbor"), mtime=3000)
    write(share, "/notes.txt", para("notes"), mtime=2000)
    write(share, "/Clients/old.md", para("old"), mtime=1000)
    svc = block()
    run(svc, "NAS")
    assert reads == ["/Clients/Harbor/brief.md", "/notes.txt", "/Clients/old.md"]   # newest first
    led = ledger(svc)
    assert {p: r["status"] for p, r in led.items()} == {
        "/Clients/Harbor/brief.md": "indexed", "/notes.txt": "indexed", "/Clients/old.md": "indexed"}
    assert led["/notes.txt"]["file_id"] == path_file_id("notes.txt")
    assert led["/notes.txt"]["mtime"] == os.stat(share / "notes.txt").st_mtime_ns
    st = svc.filespace_status("ws1", "NAS")
    assert st["files"]["indexed"] == 3 and st["lastError"] is None and st["kind"] == "block"

    reads.clear()
    embedded = svc.embedder.texts_embedded
    run(svc, "NAS")
    run(svc, "NAS")
    assert reads == []
    assert svc.embedder.texts_embedded == embedded


def test_edit_reembeds_only_changed_chunks(block, share):
    paragraphs = [para(f"section{i}", 60) for i in range(6)]
    write(share, "/long.md", "\n\n".join(paragraphs), mtime=1000)
    svc = block()
    run(svc, "NAS")
    total = ledger(svc)["/long.md"]["chunk_count"]
    assert total >= 5
    before = svc.embedder.texts_embedded
    paragraphs[-1] = para("rewritten", 60)
    write(share, "/long.md", "\n\n".join(paragraphs), mtime=2000)
    run(svc, "NAS")
    assert 1 <= svc.embedder.texts_embedded - before <= 2
    assert ledger(svc)["/long.md"]["status"] == "indexed"


def test_rename_is_delete_plus_new_without_new_embeddings(block, share, reads):
    write(share, "/a/report.md", para("quarterly report"))
    svc = block()
    run(svc, "NAS")
    old_id = ledger(svc)["/a/report.md"]["file_id"]
    embedded = svc.embedder.texts_embedded
    reads.clear()
    (share / "b").mkdir()
    os.rename(share / "a" / "report.md", share / "b" / "report.md")
    run(svc, "NAS")
    led = ledger(svc)
    assert set(led) == {"/b/report.md"}
    assert led["/b/report.md"]["file_id"] == path_file_id("b/report.md") != old_id
    assert svc.store.get_file("ws1", "NAS", old_id)["status"] == "deleted"
    assert reads == ["/b/report.md"]                     # re-read …
    assert svc.embedder.texts_embedded == embedded       # … but not re-embedded
    hits = svc.search("ws1", "NAS", "quarterly report")
    assert hits and hits[0]["path"] == "/b/report.md"


def test_deletion_is_pruned(block, share):
    write(share, "/keep.md", para("keep"))
    write(share, "/gone.md", para("gone"))
    svc = block()
    run(svc, "NAS")
    (share / "gone.md").unlink()
    run(svc, "NAS")
    assert set(ledger(svc)) == {"/keep.md"}
    assert svc.store.chunk_count("ws1", "NAS") == ledger(svc)["/keep.md"]["chunk_count"]


def _unreadable(path) -> bool:
    try:
        os.listdir(path)
        return False
    except PermissionError:
        return True


def test_unreadable_directory_prevents_deletions(block, share):
    write(share, "/locked/secret.md", para("secret"))
    write(share, "/open.md", para("open"))
    svc = block()
    run(svc, "NAS")
    os.chmod(share / "locked", 0)
    try:
        if not _unreadable(share / "locked"):
            pytest.skip("running as root: chmod 000 does not block reads")
        (share / "open.md").unlink()
        run(svc, "NAS")
        assert set(ledger(svc)) == {"/locked/secret.md", "/open.md"}   # nothing deleted
        assert "deletions deferred" in svc.filespace_status("ws1", "NAS")["lastError"]
    finally:
        os.chmod(share / "locked", 0o755)


def test_listing_error_prevents_deletions(block, share, monkeypatch):
    """Same guarantee as above without depending on file permissions (root in CI)."""
    write(share, "/locked/secret.md", para("secret"))
    write(share, "/open.md", para("open"))
    svc = block()
    run(svc, "NAS")
    orig = LocalSource.list_dir

    def list_dir(self, path):
        if path == "/locked":
            raise PermissionError(13, "Permission denied", path)
        return orig(self, path)

    monkeypatch.setattr(LocalSource, "list_dir", list_dir)
    (share / "open.md").unlink()
    run(svc, "NAS")
    assert set(ledger(svc)) == {"/locked/secret.md", "/open.md"}
    assert "deletions deferred" in svc.filespace_status("ws1", "NAS")["lastError"]


def test_symlink_outside_root_and_system_files_ignored(block, share, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside, "/private.md", para("private payroll"))
    os.symlink(outside, share / "escape")
    os.symlink(outside / "private.md", share / "private.md")
    write(share, "/real.md", para("real"))
    for rel in ("/.DS_Store", "/._real.md", "/#recycle/old.md", "/@eaDir/thumb.md", "/.Spotlight-V100/x.md",
                "/.Trashes/x.md", "/.fseventsd/x.md", "/.TemporaryItems/x.md"):
        write(share, rel, para("system"))
    svc = block()
    run(svc, "NAS")
    assert set(ledger(svc)) == {"/real.md"}
    assert svc.search("ws1", "NAS", "private payroll")[0]["path"] == "/real.md"


def test_hints_never_apply_to_block_sources(block, share):
    svc = block()
    assert svc.hint("ws1", "NAS", "write", "/x.md") is False
    assert svc.hint("ws1", "fsA", "write", "/x.md") is True


@pytest.mark.parametrize("state", ["missing", "empty", "placeholder"])
def test_unmounted_source_indexes_nothing_and_deletes_nothing(make_service, tmp_path, state):
    root = tmp_path / "share"
    write(root, "/a.md", para("alpha"))
    svc = make_service(block_sources=f"NAS={root}")
    run(svc, "NAS")
    assert set(ledger(svc)) == {"/a.md"}
    (root / "a.md").unlink()
    if state == "missing":
        root.rmdir()
    elif state == "placeholder":
        (root / ".gitkeep").write_text("")
    run(svc, "NAS")
    st = svc.filespace_status("ws1", "NAS")
    assert st["lastError"] == NOT_MOUNTED and st["state"] == "error"
    assert set(ledger(svc)) == {"/a.md"}
    assert svc.store.chunk_count("ws1", "NAS") > 0


def test_placeholder_mount_never_indexes(make_service, tmp_path):
    root = tmp_path / "block-empty"
    root.mkdir()
    (root / ".gitkeep").write_text("")
    svc = make_service(block_sources=f"NAS={root}")
    run(svc, "NAS")
    st = svc.filespace_status("ws1", "NAS")
    assert st["lastError"] == NOT_MOUNTED and st["files"]["indexed"] == 0


def test_mount_vanishing_mid_job_deletes_nothing(block, share, monkeypatch):
    write(share, "/a.md", para("alpha"))
    svc = block()
    run(svc, "NAS")
    monkeypatch.setattr("indexer.block.mounted", lambda root: True)   # passed the up-front check …
    (share / "a.md").unlink()                                         # … then the share went empty
    run(svc, "NAS")
    assert set(ledger(svc)) == {"/a.md"}
    assert svc.filespace_status("ws1", "NAS")["lastError"]


def test_name_collision_with_filespace_is_refused(make_service, share):
    write(share, "/a.md", para("alpha"))
    svc = make_service(block_sources=f"fsB={share};NAS={share}")
    assert svc.scheduler.targets() == [("ws1", "fsA"), ("ws1", "fsB"), ("ws1", "NAS")]
    assert svc.scheduler.block_root("ws1", "fsB") is None
    assert svc.store.filespace_row("ws1", "fsB")["kind"] == "lucidlink"
    st = svc.filespace_status("ws1", "fsB")
    assert st["kind"] == "lucidlink" and "refused" in st["lastError"]
    listing = {b["name"]: b for b in svc.status()["blockSources"]}
    assert listing["fsB"]["accepted"] is False and "refused" in listing["fsB"]["error"]
    assert listing["NAS"]["accepted"] is True
    # fsB stays a LucidLink filespace: its job reads the filespace, not the folder.
    src(svc, "fsB").write("/lucid.md", para("lucid"))
    run(svc, "fsB")
    assert set(ledger(svc, "fsB")) == {"/lucid.md"}


def test_enable_disable_and_fair_scheduling(block, share):
    write(share, "/a.md", para("alpha"))
    svc = block()
    with client(svc) as c:
        r = c.put("/index/filespaces", json={"workspaceId": "ws1", "filespace": "NAS", "enabled": False}, headers=H)
        assert r.json()["state"] == "disabled" and r.json()["kind"] == "block"
        assert ("ws1", "NAS") in svc.scheduler.targets()      # still a block target, not a filespace override
        assert svc.store.filespace_row("ws1", "NAS")["kind"] == "block"
        assert c.post("/index/sync", json={"filespace": "NAS"}, headers=H).json() == {"queued": []}
        assert ("ws1", "NAS") not in svc.scopes("ws1", None)
        r = c.put("/index/filespaces", json={"workspaceId": "ws1", "filespace": "NAS", "enabled": True}, headers=H)
        assert r.json()["enabled"] is True
        assert {"workspaceId": "ws1", "filespace": "NAS"} in c.post("/index/sync", json={}, headers=H).json()["queued"]


def test_scheduler_runs_block_source_with_filespaces(block, share):
    write(share, "/a.md", para("alpha"))
    svc = block(tick_sec=0.05)
    src(svc).write("/f.md", para("filespace"))
    svc.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not (ledger(svc) and ledger(svc, "fsA")):
            time.sleep(0.05)
    finally:
        svc.scheduler.stop()
    assert set(ledger(svc)) == {"/a.md"} and set(ledger(svc, "fsA")) == {"/f.md"}


def test_kind_in_search_recent_and_citations_workspace_wide(block, share, make_service):
    write(share, "/nas/garden.md", para("garden roses tulips"))
    svc = block(llm_answer="Roses, see /nas/garden.md.")
    src(svc).write("/lucid/garden.md", para("garden roses daisies"))
    run(svc, "fsA")
    run(svc, "NAS")
    with client(svc) as c:
        res = c.post("/index/search", json={"query": "garden roses"}, headers=H).json()["results"]
        assert {(r["filespace"], r["kind"]) for r in res} == {("fsA", "lucidlink"), ("NAS", "block")}
        files = c.get("/index/recent", headers=H).json()["files"]
        assert {(f["filespace"], f["kind"]) for f in files} == {("fsA", "lucidlink"), ("NAS", "block")}
        body = c.post("/index/ask", json={"question": "which roses?"}, headers=H).json()
        assert {c_["kind"] for c_ in body["citations"]} == {"lucidlink", "block"}
        nas_only = c.post("/index/search", json={"query": "roses", "filespace": "NAS"}, headers=H).json()
        assert {r["kind"] for r in nas_only["results"]} == {"block"}


def test_profile_shape_and_numbers(block, share):
    now = time.time()
    day = 86400
    write(share, "/Clients/Harbor/brief.docx", "x" * 100, mtime=now - 1 * day)
    write(share, "/Clients/Harbor/plan.md", para("plan"), mtime=now - 3 * day)
    write(share, "/Clients/budget.xlsx", "y" * 50, mtime=now - 20 * day)
    write(share, "/Media/clip.mov", "z" * 400, mtime=now - 60 * day)
    write(share, "/Media/still.jpg", "z" * 40, mtime=now - 400 * day)
    write(share, "/readme.txt", para("readme"), mtime=now - 200 * day)
    svc = block()
    run(svc, "NAS")
    with client(svc) as c:
        assert c.get("/index/profile", params={"filespace": "NAS"}).status_code == 401
        r = c.get("/index/profile", params={"workspaceId": "ws1", "filespace": "NAS"}, headers=H)
        assert c.get("/index/profile", params={"filespace": "nope"}, headers=H).status_code == 404
    assert r.status_code == 200
    p = r.json()
    assert set(p) == {"workspaceId", "filespace", "kind", "totals", "byType", "ages", "largestFolders", "skipped",
                      "lastScanAt"}
    sizes = {rel: (share / rel.lstrip("/")).stat().st_size for rel in (
        "/Clients/Harbor/brief.docx", "/Clients/Harbor/plan.md", "/Clients/budget.xlsx", "/Media/clip.mov",
        "/Media/still.jpg", "/readme.txt")}
    assert p["workspaceId"] == "ws1" and p["filespace"] == "NAS" and p["kind"] == "block"
    assert p["totals"] == {"files": 6, "bytes": sum(sizes.values())}
    by_type = {t["type"]: t for t in p["byType"]}
    assert set(by_type) == {"document", "spreadsheet", "video", "image"}
    assert by_type["document"] == {"type": "document", "files": 3, "bytes": sizes["/Clients/Harbor/brief.docx"]
                                   + sizes["/Clients/Harbor/plan.md"] + sizes["/readme.txt"]}
    assert by_type["video"] == {"type": "video", "files": 1, "bytes": 400}
    assert [t["bytes"] for t in p["byType"]] == sorted((t["bytes"] for t in p["byType"]), reverse=True)
    assert p["ages"] == {"7d": 2, "30d": 1, "90d": 1, "older": 2}
    assert p["largestFolders"] == [
        {"path": "/Clients", "files": 3, "bytes": sizes["/Clients/Harbor/brief.docx"]
         + sizes["/Clients/Harbor/plan.md"] + 50},
        {"path": "/Media", "files": 2, "bytes": 440}]
    assert p["skipped"] == [{"reason": "unextractable: not a valid OOXML (zip) file", "files": 2},
                            {"reason": "unsupported type (.jpg)", "files": 1},
                            {"reason": "unsupported type (.mov)", "files": 1}]
    assert p["lastScanAt"]


def test_profile_from_ledger_only(block, share, monkeypatch):
    write(share, "/a.md", para("alpha"))
    svc = block()
    run(svc, "NAS")

    def boom(*a, **k):
        raise AssertionError("profile touched the source")

    monkeypatch.setattr(LocalSource, "list_dir", boom)
    monkeypatch.setattr(LocalSource, "read_file", boom)
    monkeypatch.setattr(LocalSource, "stat", boom)
    assert svc.profile("ws1", "NAS")["totals"]["files"] == 1
    assert svc.profile(None, "fsA")["kind"] == "lucidlink"
