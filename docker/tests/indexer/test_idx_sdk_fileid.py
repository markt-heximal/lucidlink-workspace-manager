"""Regression: SDK 0.14 `read_dir()` returns file_id="" for every entry; only
`get_entry()` has it (lucidlink_native/_filesystem.py). Entries here are built
with the SDK's own `_to_dir_entry` and served through the real
`SdkFilespaceSource`, so the scanner sees exactly what production sees."""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from lucidlink.filesystem import _to_dir_entry

from conftest import files, para

from indexer.source import SdkFilespaceSource, SourceProvider


class FakeNativeFs:
    """Mimics lucidlink.Filesystem for a flat root: read_dir without ids and
    with a listing mtime (s); get_entry with the id and update_time (ns)."""

    def __init__(self):
        self.files: dict[str, dict] = {}   # path -> {data, id, mtime}
        self.get_entry_calls = 0
        self.read_calls = 0
        self.blank_ids = False             # get_entry also returns "" (defensive case)
        self.raise_get_entry = False
        self._next = 100

    def put(self, path, data, mtime):
        cur = self.files.get(path)
        fid = cur["id"] if cur else f"fid-{self._next}"
        self._next += 1
        self.files[path] = {"data": data.encode(), "id": fid, "mtime": mtime}

    def rename(self, src, dst):
        self.files[dst] = self.files.pop(src)

    def read_dir(self, path):
        assert path == "/"
        return [_to_dir_entry({"name": p[1:], "size": len(f["data"]), "type": "file", "file_id": "",
                               "file_id_external": 0, "creation_time": 0, "update_time": f["mtime"]})
                for p, f in sorted(self.files.items())]

    def get_entry(self, path):
        self.get_entry_calls += 1
        if self.raise_get_entry:
            raise OSError("transient")
        f = self.files.get(path)
        if f is None:
            raise FileNotFoundError(path)
        return _to_dir_entry({"name": path[1:], "size": len(f["data"]), "type": "file",
                              "file_id": "" if self.blank_ids else f["id"], "file_id_external": 7,
                              "creation_time": 1, "update_time": f["mtime"] * 1_000_000_000})

    def read_file(self, path):
        self.read_calls += 1
        return self.files[path]["data"]


class Provider(SourceProvider):
    def __init__(self, fs):
        self.fs = fs

    @contextmanager
    def lease(self, ws, fs):
        yield SdkFilespaceSource(self.fs)


@pytest.fixture
def native(make_service):
    svc = make_service()
    fs = FakeNativeFs()
    svc.sources = Provider(fs)
    return svc, fs


def _scan(svc):
    svc.scheduler.run_job("ws1", "fsA", full=True)


def test_sdk_entries_do_not_collapse_into_one_row(native):
    svc, fs = native
    for i in range(5):
        fs.put(f"/doc{i}.md", para(f"topic{i}"), 1_700_000_000 + i)
    _scan(svc)
    rows = svc.store.query("SELECT file_id, path, status FROM files")
    assert len(rows) == 5
    assert {r["file_id"] for r in rows} == {f"fid-{100 + i}" for i in range(5)}
    assert all(r["status"] == "indexed" for r in rows)
    assert all(r["file_id"] for r in rows)
    assert fs.get_entry_calls == 5


def test_unchanged_rescan_makes_no_get_entry_calls(native):
    svc, fs = native
    for i in range(5):
        fs.put(f"/doc{i}.md", para(f"topic{i}"), 1_700_000_000 + i)
    _scan(svc)
    fs.get_entry_calls = fs.read_calls = 0
    embedded = svc.embedder.texts_embedded

    _scan(svc)
    _scan(svc)

    assert fs.get_entry_calls == 0 and fs.read_calls == 0
    assert svc.embedder.texts_embedded == embedded
    assert len(files(svc)) == 5


def test_change_rename_and_delete_with_sdk_entries(native):
    svc, fs = native
    fs.put("/a.md", para("alpha"), 1_700_000_000)
    fs.put("/b.md", para("beta"), 1_700_000_001)
    _scan(svc)
    fid_a = files(svc)["/a.md"]["file_id"]
    pids = [r["point_id"] for r in svc.store.file_chunks("ws1", "fsA", fid_a)]
    embedded = svc.embedder.texts_embedded
    fs.get_entry_calls = 0

    fs.rename("/a.md", "/renamed.md")          # same id, new path
    fs.put("/b.md", para("beta revised"), 1_700_000_050)
    _scan(svc)

    led = files(svc)
    assert set(led) == {"/renamed.md", "/b.md"}
    assert led["/renamed.md"]["file_id"] == fid_a
    assert [r["point_id"] for r in svc.store.file_chunks("ws1", "fsA", fid_a)] == pids
    assert led["/b.md"]["mtime"] == 1_700_000_050 * 1_000_000_000      # authoritative get_entry value
    assert led["/b.md"]["list_mtime"] == 1_700_000_050                 # read_dir value, fast path only
    assert fs.get_entry_calls == 2                                      # only the two that changed
    assert svc.embedder.texts_embedded > embedded

    del fs.files["/b.md"]
    _scan(svc)
    assert set(files(svc)) == {"/renamed.md"}


def test_missing_file_id_never_writes_a_row_or_deletes(native):
    svc, fs = native
    fs.put("/a.md", para("alpha"), 1_700_000_000)
    _scan(svc)
    fs.put("/new.md", para("novel"), 1_700_000_100)
    del fs.files["/a.md"]
    fs.blank_ids = True

    _scan(svc)

    rows = svc.store.query("SELECT path, status FROM files")
    assert rows == [{"path": "/a.md", "status": "indexed"}]       # no "" row; deletion deferred
    assert "deletions deferred" in svc.filespace_status("ws1", "fsA")["lastError"]


def test_get_entry_failure_marks_scan_incomplete(native):
    svc, fs = native
    fs.put("/a.md", para("alpha"), 1_700_000_000)
    _scan(svc)
    fs.put("/b.md", para("beta"), 1_700_000_001)
    del fs.files["/a.md"]
    fs.raise_get_entry = True
    _scan(svc)
    assert set(files(svc)) == {"/a.md"}


def test_reviewer_repro_get_entry_not_found(native):
    """The reviewer's original scenario: get_entry raising for every path used
    to leave one collapsed row; now nothing is written."""
    svc, fs = native
    for i in range(5):
        fs.put(f"/doc{i}.md", para(f"topic{i}"), 1_700_000_000 + i)
    fs.get_entry = lambda path: (_ for _ in ()).throw(FileNotFoundError(path))
    _scan(svc)
    assert svc.store.query("SELECT * FROM files") == []
