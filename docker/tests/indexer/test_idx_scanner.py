"""Delta detection: what a scan reads, embeds, renames, skips and deletes."""
from __future__ import annotations

import time

from conftest import files, para, run, src

from indexer.scanner import SKIP_DIRS


def _record_reads(source):
    order: list[str] = []
    orig = source.read_file

    def read(path):
        order.append(path)
        return orig(path)

    source.read_file = read
    return order


def _point_ids(svc, path, fs="fsA"):
    fid = files(svc, fs)[path]["file_id"]
    return [r["point_id"] for r in svc.store.file_chunks("ws1", fs, fid)]


def test_first_scan_indexes_everything_newest_first(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/old.md", para("ancient"), mtime=1000)
    s.write("/docs/mid.txt", para("middle"), mtime=2000)
    s.write("/docs/deep/new.md", para("recent"), mtime=3000)
    order = _record_reads(s)

    run(svc)

    assert order == ["/docs/deep/new.md", "/docs/mid.txt", "/old.md"]
    led = files(svc)
    assert {p: r["status"] for p, r in led.items()} == {
        "/old.md": "indexed", "/docs/mid.txt": "indexed", "/docs/deep/new.md": "indexed"}
    assert all(r["chunk_count"] >= 1 for r in led.values())
    st = svc.filespace_status("ws1", "fsA")
    assert st["files"] == {"indexed": 3, "pending": 0, "failed": 0, "skipped": 0}
    assert st["chunks"] == svc.store.chunk_count("ws1", "fsA") > 0
    assert st["lastScanAt"] and st["lastSyncAt"] and st["lastError"] is None


def test_unchanged_rescan_reads_and_embeds_nothing(make_service):
    svc = make_service()
    s = src(svc)
    for i in range(5):
        s.write(f"/f{i}.md", para(f"topic{i}"))
    run(svc)
    s.reads = s.stats = 0
    requests, embedded = svc.embedder.requests, svc.embedder.texts_embedded

    run(svc)
    run(svc)

    assert s.reads == 0
    assert s.stats == 0                       # no get_entry: the per-path fast path held
    assert svc.embedder.requests == requests
    assert svc.embedder.texts_embedded == embedded


def test_modified_file_reembeds_only_changed_chunks(make_service):
    svc = make_service()
    s = src(svc)
    paragraphs = [para(f"section{i}", 60) for i in range(6)]
    s.write("/long.md", "\n\n".join(paragraphs))
    run(svc)
    total = files(svc)["/long.md"]["chunk_count"]
    assert total >= 5
    before = svc.embedder.texts_embedded

    paragraphs[-1] = para("rewritten", 60)
    s.write("/long.md", "\n\n".join(paragraphs))
    run(svc)

    re_embedded = svc.embedder.texts_embedded - before
    assert 1 <= re_embedded <= 2 < total
    assert files(svc)["/long.md"]["status"] == "indexed"


def test_touch_with_same_content_does_not_reembed(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/a.md", para("alpha"), mtime=1000)
    run(svc)
    pids = _point_ids(svc, "/a.md")
    before = svc.embedder.texts_embedded
    s.reads = 0

    s.touch("/a.md", mtime=5000)
    run(svc)

    assert s.reads == 1                       # the hash check has to read it
    assert svc.embedder.texts_embedded == before
    row = files(svc)["/a.md"]
    assert row["list_mtime"] == 5000 and row["mtime"] == 5000   # SDK 1.0: listing == get_entry
    assert row["status"] == "indexed"
    assert _point_ids(svc, "/a.md") == pids
    s.reads = 0
    run(svc)
    assert s.reads == 0                       # and the fast path holds afterwards


def test_rename_keeps_embeddings_and_updates_path(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/a.md", para("alpha"))
    run(svc)
    pids = _point_ids(svc, "/a.md")
    before = svc.embedder.texts_embedded
    s.reads = 0

    s.move("/a.md", "/archive/b.md")
    run(svc)

    assert s.reads == 0
    assert svc.embedder.texts_embedded == before
    led = files(svc)
    assert "/a.md" not in led and led["/archive/b.md"]["status"] == "indexed"
    assert _point_ids(svc, "/archive/b.md") == pids
    hits = svc.search("ws1", "fsA", "alpha detail")
    assert hits and hits[0]["path"] == "/archive/b.md"


def test_deletion_prunes_chunks(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/a.md", para("alpha"))
    s.write("/dir/b.md", para("beta"))
    run(svc)
    s.delete("/a.md")
    s.delete("/dir")

    run(svc)

    assert files(svc) == {}
    assert svc.store.chunk_count("ws1", "fsA") == 0
    rows = svc.store.query("SELECT status FROM files")
    assert {r["status"] for r in rows} == {"deleted"}
    assert svc.search("ws1", "fsA", "alpha") == []


def test_failed_listing_never_deletes(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/keep/x.md", para("xray"))
    s.write("/keep/sub/y.md", para("yankee"))
    s.write("/z.md", para("zulu"))
    run(svc)
    chunks = svc.store.chunk_count("ws1", "fsA")

    s.fail_dirs = {"/keep"}
    s.delete("/z.md")  # a real deletion elsewhere is deferred too: the scan is incomplete
    run(svc)

    assert set(files(svc)) == {"/keep/x.md", "/keep/sub/y.md", "/z.md"}
    assert svc.store.chunk_count("ws1", "fsA") == chunks
    st = svc.filespace_status("ws1", "fsA")
    assert st["state"] == "error" and "/keep" in st["lastError"]

    s.fail_dirs = set()
    run(svc)
    assert set(files(svc)) == {"/keep/x.md", "/keep/sub/y.md"}
    assert svc.filespace_status("ws1", "fsA")["lastError"] is None


def test_root_listing_failure_keeps_everything(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/a.md", para("alpha"))
    run(svc)
    s.fail_dirs = {"/"}
    run(svc)
    assert set(files(svc)) == {"/a.md"}


def test_skipped_files_recorded_with_reasons(make_service):
    svc = make_service(max_file_bytes=2000)
    s = src(svc)
    s.write("/big.txt", "word " * 1000)                       # 5000 bytes
    s.write("/blob.txt", b"\x00\x01\x02binary" * 20)
    s.write("/photo.png", b"\x89PNG....")
    s.write("/tiny.md", "too short")
    s.write("/ok.md", para("fine"))
    s.write("/.hidden.md", para("secret"))
    s.write("/node_modules/pkg/readme.md", para("vendored"))
    s.write("/.git/HEAD.txt", para("git"))
    order = _record_reads(s)

    run(svc)

    led = files(svc)
    assert set(led) == {"/big.txt", "/blob.txt", "/photo.png", "/tiny.md", "/ok.md"}
    assert led["/big.txt"]["status"] == "skipped" and led["/big.txt"]["reason"].startswith("too large")
    assert led["/blob.txt"]["reason"] == "binary content"
    assert led["/photo.png"]["reason"] == "unsupported type (.png)"
    assert led["/tiny.md"]["reason"] == "too little text"
    assert led["/ok.md"]["status"] == "indexed"
    assert "/big.txt" not in order and "/photo.png" not in order   # skipped without reading
    assert svc.filespace_status("ws1", "fsA")["files"]["skipped"] == 4
    s.reads = 0
    run(svc)
    assert s.reads == 0                                             # skips are stable
    assert "node_modules" in SKIP_DIRS


def test_read_failure_is_recorded_not_fatal(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/bad.md", para("bad"))
    s.write("/good.md", para("good"))
    s.fail_reads = {"/bad.md"}
    run(svc)
    led = files(svc)
    assert led["/bad.md"]["status"] == "failed" and "read failed" in led["/bad.md"]["reason"]
    assert led["/good.md"]["status"] == "indexed"
    assert svc.filespace_status("ws1", "fsA")["files"]["failed"] == 1


def test_embedder_outage_leaves_files_pending(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/a.md", para("alpha"))

    def down(texts):
        raise ConnectionError("embedder down")
    svc.embedder._request = down
    run(svc)
    st = svc.filespace_status("ws1", "fsA")
    assert st["files"]["pending"] == 1 and "embedding failed" in st["lastError"]
    assert svc.embedder.status()["ok"] is False


def test_hints_write_move_delete(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/a.md", para("alpha"))
    run(svc)
    pids = _point_ids(svc, "/a.md")

    s.write("/new.md", para("novel"))
    svc.hint("ws1", "fsA", "write", "/new.md")
    run(svc, full=False)
    assert files(svc)["/new.md"]["status"] == "indexed"

    before = svc.embedder.texts_embedded
    s.move("/a.md", "/moved/a.md")
    svc.hint(None, "fsA", "move", "/moved/a.md", src="/a.md")
    run(svc, full=False)
    assert "/a.md" not in files(svc)
    assert _point_ids(svc, "/moved/a.md") == pids
    assert svc.embedder.texts_embedded == before

    s.delete("/new.md")
    svc.hint("ws1", "fsA", "delete", "/new.md")
    run(svc, full=False)
    assert set(files(svc)) == {"/moved/a.md"}

    s.mkdir("/fresh")
    s.write("/fresh/f.md", para("foxtrot"))
    svc.hint("ws1", "fsA", "mkdir", "/fresh")
    run(svc, full=False)
    assert files(svc)["/fresh/f.md"]["status"] == "indexed"


def test_scheduler_processes_hint_within_two_seconds(make_service):
    svc = make_service(tick_sec=0.05)
    s = src(svc)
    svc.scheduler.start()
    def synced():
        row = svc.store.filespace_row("ws1", "fsA")   # None until the first target refresh
        return bool(row and row["last_sync_at"])

    deadline = time.time() + 5
    while time.time() < deadline and not synced():
        time.sleep(0.02)
    assert synced()

    s.write("/hinted.md", para("hinted"))
    svc.hint("ws1", "fsA", "write", "/hinted.md")
    t0 = time.time()
    while time.time() - t0 < 2 and files(svc).get("/hinted.md", {}).get("status") != "indexed":
        time.sleep(0.02)
    assert files(svc)["/hinted.md"]["status"] == "indexed"
    svc.scheduler.stop()


def test_never_two_jobs_for_one_filespace(make_service):
    svc = make_service(tick_sec=0.01, concurrency=4)
    s = src(svc)
    for i in range(20):
        s.write(f"/f{i}.md", para(f"t{i}"))
    active = {"now": 0, "max": 0}
    orig = svc.scheduler.run_job

    def tracked(ws, fs, full=None):
        if fs == "fsA":
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            time.sleep(0.05)
        try:
            return orig(ws, fs, full)
        finally:
            if fs == "fsA":
                active["now"] -= 1

    svc.scheduler.run_job = tracked
    svc.scheduler.start()
    for _ in range(10):
        svc.sync("ws1", "fsA")
        svc.hint("ws1", "fsA", "write", "/f1.md")
        time.sleep(0.01)
    time.sleep(0.5)
    svc.scheduler.stop()
    assert active["max"] == 1


def test_job_exception_is_recorded_and_loop_survives(make_service):
    svc = make_service(tick_sec=0.02)
    s = src(svc)
    s.write("/a.md", para("alpha"))
    boom = {"n": 0}

    def broken(*a, **k):
        boom["n"] += 1
        raise RuntimeError("sdk exploded")

    s.list_dir = broken
    svc.scheduler.run_job("ws1", "fsA", full=True)
    st = svc.filespace_status("ws1", "fsA")
    assert st["state"] == "error"
    assert "listing failed" in st["lastError"]
    # The scheduler thread keeps running after errors.
    svc.scheduler.start()
    svc.sync("ws1", "fsA")
    time.sleep(0.3)
    assert svc.scheduler._thread.is_alive()
    svc.scheduler.stop()
