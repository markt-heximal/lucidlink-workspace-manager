"""Identity through the real SDK 1.0 shapes.

SDK 1.0's `read_dir()` returns every entry's id (`FsEntry.id`) and
`modified_at`, the same values `get_entry()` reports, so a scan keys the
ledger on the listed id and needs no `get_entry` per file. (SDK 0.14 returned
`file_id=""` from `read_dir`; every file then shared the id "" unless the
scanner called `get_entry`.) Entries here come from the SDK's own
`lucidlink.FileSystem` over the in-memory native fake (tests/sdk_fake.py) and
are served through the real `SdkFilespaceSource`, so the scanner sees exactly
what production sees. The id-less (0.14-shaped) listing is kept as the safety
fallback: such an entry costs one `get_entry`, and a row is never written
without an id.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from lucidlink import FileSystem

from conftest import files, para
from sdk_fake import FakeNativeFs

from indexer.source import SdkFilespaceSource, SourceProvider


class Native(FakeNativeFs):
    """The native fake with switches for the 0.14-shaped and broken cases."""

    def __init__(self):
        super().__init__()
        self.blank_list_ids = False      # read_dir without ids (SDK 0.14)
        self.blank_entry_ids = False     # get_entry without ids too (defensive case)

    def read_dir(self, path):
        recs = super().read_dir(path)
        if self.blank_list_ids:
            for r in recs:
                r["entry_id"] = ""
        return recs

    def get_entry(self, path):
        rec = super().get_entry(path)
        if self.blank_entry_ids:
            rec["entry_id"] = ""
        return rec

    def put(self, path, text, mtime_s=None):
        node = self.nodes.get(path)
        data = text.encode()
        if node is None:
            node = self.add_file(path, data)
        else:
            node["data"][:] = data
            self.clock += 10**9
            node["mtime"] = self.clock
        if mtime_s is not None:
            node["mtime"] = mtime_s * 10**9
        return node


class CountingSource(SdkFilespaceSource):
    def __init__(self, fs, counters):
        super().__init__(fs)
        self.c = counters

    def stat(self, path):
        self.c["stat"] += 1
        if self.c.get("raise_stat"):
            raise OSError("transient")
        return super().stat(path)

    def read_file(self, path):
        self.c["read"] += 1
        return super().read_file(path)


class Provider(SourceProvider):
    def __init__(self, fs, counters):
        self.fs, self.counters = fs, counters

    @contextmanager
    def lease(self, ws, fs):
        yield CountingSource(self.fs, self.counters)


@pytest.fixture
def native(make_service):
    svc = make_service()
    nat = Native()
    counters = {"stat": 0, "read": 0}
    svc.sources = Provider(FileSystem(nat), counters)
    return svc, nat, counters


def _scan(svc):
    svc.scheduler.run_job("ws1", "fsA", full=True)


def test_entries_keep_their_listed_ids_without_get_entry(native):
    svc, nat, c = native
    ids = {f"/doc{i}.md": nat.put(f"/doc{i}.md", para(f"topic{i}"))["id"] for i in range(5)}
    _scan(svc)
    led = files(svc)
    assert {p: r["file_id"] for p, r in led.items()} == ids
    assert all(r["status"] == "indexed" for r in led.values())
    assert c["stat"] == 0                                   # identity came from read_dir
    assert all(r["mtime"] == r["list_mtime"] == nat.nodes[p]["mtime"] for p, r in led.items())


def test_unchanged_rescan_makes_no_get_entry_or_read_calls(native):
    svc, nat, c = native
    for i in range(5):
        nat.put(f"/doc{i}.md", para(f"topic{i}"))
    _scan(svc)
    c["stat"] = c["read"] = 0
    embedded = svc.embedder.texts_embedded

    _scan(svc)
    _scan(svc)

    assert c == {"stat": 0, "read": 0}
    assert svc.embedder.texts_embedded == embedded
    assert len(files(svc)) == 5


def test_change_rename_and_delete_with_sdk_entries(native):
    svc, nat, c = native
    nat.put("/a.md", para("alpha"))
    nat.put("/b.md", para("beta"))
    _scan(svc)
    fid_a = files(svc)["/a.md"]["file_id"]
    pids = [r["point_id"] for r in svc.store.file_chunks("ws1", "fsA", fid_a)]
    embedded = svc.embedder.texts_embedded

    nat.move("/a.md", "/renamed.md")          # same id, new path
    b = nat.put("/b.md", para("beta revised"))
    _scan(svc)

    led = files(svc)
    assert set(led) == {"/renamed.md", "/b.md"}
    assert led["/renamed.md"]["file_id"] == fid_a
    assert [r["point_id"] for r in svc.store.file_chunks("ws1", "fsA", fid_a)] == pids
    assert led["/b.md"]["mtime"] == led["/b.md"]["list_mtime"] == b["mtime"]
    assert c["stat"] == 0
    assert svc.embedder.texts_embedded > embedded

    nat.delete("/b.md")
    _scan(svc)
    assert set(files(svc)) == {"/renamed.md"}


def test_replaced_file_with_same_size_and_mtime_is_a_new_file(native):
    """The path fast path also checks the listed id: a file deleted and
    recreated at the same path is not mistaken for the old one."""
    svc, nat, c = native
    old = nat.put("/a.md", para("alpha"), mtime_s=1_700_000_000)
    _scan(svc)
    nat.delete("/a.md")
    new = nat.put("/a.md", para("alpha"), mtime_s=1_700_000_000)
    assert new["id"] != old["id"]
    _scan(svc)
    rows = svc.store.query("SELECT file_id, status FROM files ORDER BY file_id")
    assert {r["file_id"]: r["status"] for r in rows} == {old["id"]: "deleted", new["id"]: "indexed"}


def test_id_less_listing_falls_back_to_get_entry(native):
    """SDK 0.14 shape: no id in read_dir. Each new file costs one get_entry and
    still gets its own row."""
    svc, nat, c = native
    nat.blank_list_ids = True
    ids = {nat.put(f"/doc{i}.md", para(f"topic{i}"))["id"] for i in range(5)}
    _scan(svc)
    rows = svc.store.query("SELECT file_id, status FROM files")
    assert {r["file_id"] for r in rows} == ids
    assert all(r["status"] == "indexed" for r in rows)
    assert c["stat"] == 5
    c["stat"] = 0
    _scan(svc)
    assert c["stat"] == 0                                   # path fast path holds


def test_missing_file_id_never_writes_a_row_or_deletes(native):
    svc, nat, c = native
    nat.put("/a.md", para("alpha"))
    _scan(svc)
    nat.put("/new.md", para("novel"))
    nat.delete("/a.md")
    nat.blank_list_ids = nat.blank_entry_ids = True

    _scan(svc)

    rows = svc.store.query("SELECT path, status FROM files")
    assert rows == [{"path": "/a.md", "status": "indexed"}]       # no "" row; deletion deferred
    assert "deletions deferred" in svc.filespace_status("ws1", "fsA")["lastError"]


def test_get_entry_failure_marks_scan_incomplete(native):
    svc, nat, c = native
    nat.put("/a.md", para("alpha"))
    _scan(svc)
    nat.blank_list_ids = True
    nat.put("/b.md", para("beta"))
    nat.delete("/a.md")
    c["raise_stat"] = True
    _scan(svc)
    assert set(files(svc)) == {"/a.md"}


def test_reviewer_repro_get_entry_not_found(native):
    """The reviewer's original scenario: an id-less listing and get_entry
    raising for every path used to leave one collapsed row; now nothing is
    written."""
    svc, nat, c = native
    nat.blank_list_ids = True
    for i in range(5):
        nat.put(f"/doc{i}.md", para(f"topic{i}"))
    real = nat.get_entry
    nat.get_entry = lambda path: (_ for _ in ()).throw(FileNotFoundError(path))
    _scan(svc)
    nat.get_entry = real
    assert svc.store.query("SELECT * FROM files") == []
