"""LocalSource: listing, reading and refusing paths in a real directory tree."""
from __future__ import annotations

import os
import sqlite3

import pytest

from indexer.block import (LocalSource, NotMounted, PathRefused, RoutingSourceProvider, mounted,
                           parse_block_sources, path_file_id, type_of)
from indexer.source import MemorySourceProvider
from indexer.store import Store


def _tree(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir()
    (root / "docs" / "a.md").write_text("alpha")
    (root / "b.txt").write_text("beta beta")
    return root


def test_lists_files_and_dirs_with_path_ids_and_ns_mtimes(tmp_path):
    root = _tree(tmp_path / "share")
    s = LocalSource(str(root))
    top = {e.name: e for e in s.list_dir("/")}
    assert set(top) == {"docs", "b.txt"}
    assert top["docs"].type == "dir"
    b = top["b.txt"]
    assert b.type == "file" and b.size == 9
    assert b.mtime == os.stat(root / "b.txt").st_mtime_ns
    assert b.file_id == path_file_id("b.txt") and b.file_id.startswith("p:")
    inner = s.list_dir("/docs")
    assert [(e.name, e.file_id) for e in inner] == [("a.md", path_file_id("docs/a.md"))]
    st = s.stat("/docs/a.md")
    assert (st.type, st.size, st.file_id) == ("file", 5, path_file_id("docs/a.md"))
    assert st.mtime == inner[0].mtime
    assert s.stat("/docs").type == "dir"
    assert s.stat("/nope.md") is None and s.stat("/b.txt/x") is None
    assert s.read_file("/docs/a.md") == b"alpha"


def test_vanished_file_reads_as_not_found(tmp_path):
    s = LocalSource(str(_tree(tmp_path / "share")))
    with pytest.raises(FileNotFoundError):
        s.read_file("/gone.md")


def test_symlinks_are_never_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret")
    root = _tree(tmp_path / "share")
    os.symlink(outside, root / "link_dir")
    os.symlink(outside / "secret.md", root / "link.md")
    os.symlink(root / "b.txt", root / "inside_link.txt")
    s = LocalSource(str(root))
    assert {e.name for e in s.list_dir("/")} == {"docs", "b.txt"}
    with pytest.raises(PathRefused):
        s.list_dir("/link_dir")
    for p in ("/link_dir/secret.md", "/link.md", "/inside_link.txt"):
        with pytest.raises(PathRefused):
            s.read_file(p)
        with pytest.raises(PathRefused):
            s.stat(p)


def test_path_traversal_refused(tmp_path):
    (tmp_path / "sibling.md").write_text("no")
    s = LocalSource(str(_tree(tmp_path / "share")))
    for bad in ("/../sibling.md", "/docs/../../sibling.md", "/./b.txt", "/docs/\x00a.md"):
        with pytest.raises(PathRefused):
            s.read_file(bad)
        with pytest.raises(PathRefused):
            s.stat(bad)
    with pytest.raises(PathRefused):
        s.list_dir("/..")


def test_system_files_and_folders_skipped(tmp_path):
    root = _tree(tmp_path / "share")
    for d in (".Spotlight-V100", ".Trashes", ".fseventsd", "#recycle", "@eaDir", ".TemporaryItems",
              "node_modules", ".git"):
        (root / d).mkdir()
        (root / d / "x.md").write_text("system")
    for f in (".DS_Store", "._b.txt", ".hidden.md"):
        (root / f).write_text("system")
    os.mkfifo(root / "pipe")
    assert {e.name for e in LocalSource(str(root)).list_dir("/")} == {"docs", "b.txt"}


def test_listing_errors_raise(tmp_path):
    s = LocalSource(str(_tree(tmp_path / "share")))
    with pytest.raises(FileNotFoundError):
        s.list_dir("/missing")
    with pytest.raises(NotADirectoryError):
        s.list_dir("/b.txt")


def test_empty_or_placeholder_root_is_not_mounted(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    placeholder = tmp_path / "block-empty"
    placeholder.mkdir()
    (placeholder / ".gitkeep").write_text("")
    for root in (empty, placeholder):
        assert not mounted(str(root))
        with pytest.raises(NotMounted):
            LocalSource(str(root)).list_dir("/")
    assert not mounted(str(tmp_path / "missing"))
    assert mounted(str(_tree(tmp_path / "share")))


def test_routing_provider(tmp_path):
    root = _tree(tmp_path / "share")
    mem = MemorySourceProvider()
    roots = {("ws1", "NAS"): str(root), ("ws1", "Gone"): str(tmp_path / "missing")}
    router = RoutingSourceProvider(lambda ws, fs: roots.get((ws, fs)), lambda: mem)
    with router.lease("ws1", "NAS") as s:
        assert isinstance(s, LocalSource)
    with router.lease("ws1", "fsA") as s:
        assert s is mem.get("ws1", "fsA")
    with pytest.raises(NotMounted, match="block source path not mounted"):
        with router.lease("ws1", "Gone"):
            pass


def test_parse_block_sources():
    specs = parse_block_sources(" NAS=/block/source1 ; ;Bad;x/y=/p;Rel=block;NAS=/other")
    assert [(s.name, s.path, s.error is None) for s in specs] == [
        ("NAS", "/block/source1", True), ("Bad", "", False), ("x/y", "/p", False), ("Rel", "block", False),
        ("NAS", "/other", False)]
    assert parse_block_sources("") == []


def test_type_of_uses_lab_classes():
    assert [type_of(n) for n in ("a.DOCX", "b.xlsx", "c.key", "d.pdf", "e.jpg", "f.psd", "g.wav", "h.mov",
                                 "i.py", "j.zip", "k.prproj", "l.bin", "noext")] == [
        "document", "spreadsheet", "presentation", "pdf", "image", "design", "audio", "video", "code",
        "archive", "project", "other", "other"]


def test_kind_column_migrates_old_databases(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE filespaces (workspace_id TEXT NOT NULL, filespace TEXT NOT NULL, "
              "enabled INTEGER NOT NULL DEFAULT 1, enabled_override INTEGER NOT NULL DEFAULT 0, "
              "last_scan_at TEXT, last_sync_at TEXT, last_error TEXT, last_scan_id INTEGER NOT NULL DEFAULT 0, "
              "PRIMARY KEY (workspace_id, filespace))")
    c.execute("INSERT INTO filespaces (workspace_id, filespace) VALUES ('ws1', 'fsA')")
    c.commit()
    c.close()
    store = Store(str(db))
    assert store.kinds() == {("ws1", "fsA"): "lucidlink"}
    store.set_filespace_kind("ws1", "NAS", "block")
    assert store.kinds()[("ws1", "NAS")] == "block"
    store.close()
