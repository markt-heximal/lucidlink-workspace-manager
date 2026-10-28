"""iWork packages in the scanner: one directory = one file, cheap rescans,
change detection from inside, deletion safety, hints, and the profile."""
from __future__ import annotations

import os

from conftest import files, para, run, src
from iwork_fixtures import (iwa, keynote_members, numbers_members, pages_members, storage, write_package,
                            zip_bytes)
from test_idx_block import block, ledger, reads, share  # noqa: F401  (fixtures)
from test_idx_extract import make_pdf

from indexer.block import path_file_id
from indexer.scanner import package_root

PKG = "/Board/Minutes.pages"


def _put_package(s, path, members, mtime=None):
    for rel, data in members.items():
        s.write(f"{path}/{rel}", data, mtime=mtime)


def _chunk_text(svc, path, fs="fsA"):
    fid = files(svc, fs)[path]["file_id"]
    return "\n".join(r["text"] for r in svc.store.query(
        "SELECT text FROM chunks WHERE workspace_id='ws1' AND filespace=? AND file_id=? ORDER BY chunk_index",
        (fs, fid)))


def _recording(s):
    seen: list[str] = []
    orig = s.read_file

    def read(path):
        seen.append(path)
        return orig(path)

    s.read_file = read
    return seen


def test_package_is_one_file_and_its_text_is_searchable(make_service):
    svc = make_service()
    s = src(svc)
    _put_package(s, PKG, pages_members("Quarterly board minutes approved.", "Harbor expansion discussed."))
    s.write("/Board/agenda.md", para("agenda"))
    seen = _recording(s)

    run(svc)

    led = files(svc)
    assert set(led) == {PKG, "/Board/agenda.md"}            # no inner files in the ledger
    row = led[PKG]
    assert row["status"] == "indexed" and row["chunk_count"] >= 1
    assert row["file_id"] == "dir:" + PKG                     # the directory's own get_entry id
    assert row["size"] == sum(len(v) for v in pages_members("Quarterly board minutes approved.",
                                                            "Harbor expansion discussed.").values())
    assert row["mtime"] == max(s.files[p]["mtime"] for p in s.files if p.startswith(PKG + "/"))
    text = _chunk_text(svc, PKG)
    assert "Harbor expansion discussed." in text and "Helvetica" not in text
    # Only the members the extractor needs were read: never previews or Data/.
    assert sorted(p for p in seen if p.startswith(PKG)) == [
        PKG + "/Index/Document.iwa", PKG + "/Index/DocumentStylesheet.iwa", PKG + "/Index/Metadata.iwa"]
    hits = svc.search("ws1", "fsA", "harbor expansion minutes")
    assert hits and hits[0]["path"] == PKG


def test_single_file_zip_forms_index_too(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/Plan.numbers", zip_bytes(numbers_members(["Gross margin", "North America", "Harbor office lease"])))
    s.write("/Deck.key", zip_bytes(keynote_members([("Opening title", "Market overview", "Smile")])))
    run(svc)
    led = files(svc)
    assert led["/Plan.numbers"]["status"] == "indexed"
    assert "Gross margin" in _chunk_text(svc, "/Plan.numbers")
    assert "Market overview" in _chunk_text(svc, "/Deck.key")


def test_unchanged_package_rescan_reads_nothing(make_service):
    svc = make_service()
    s = src(svc)
    _put_package(s, PKG, pages_members("Some minutes text here. The board agreed on the harbor budget"))
    _put_package(s, "/Q3.numbers", numbers_members(["Revenue line for the northern harbor region"]))
    run(svc)
    s.reads = s.stats = 0
    embedded = svc.embedder.texts_embedded

    run(svc)
    run(svc)

    assert s.reads == 0 and s.stats == 0                      # only the bounded metadata walk
    assert svc.embedder.texts_embedded == embedded
    assert files(svc)[PKG]["status"] == "indexed"


def test_editing_an_inner_file_marks_the_package_changed(make_service):
    svc = make_service()
    s = src(svc)
    _put_package(s, PKG, pages_members("Original minutes wording. The board agreed on the harbor budget"))
    run(svc)
    fid = files(svc)[PKG]["file_id"]

    s.write(PKG + "/Index/Document.iwa", iwa([storage("Revised minutes wording with a new resolution.")]))
    run(svc)

    led = files(svc)
    assert set(led) == {PKG} and led[PKG]["file_id"] == fid
    text = _chunk_text(svc, PKG)
    assert "new resolution" in text and "Original" not in text
    assert led[PKG]["change_kind"] == "modified"


def test_touched_package_is_not_reembedded(make_service):
    svc = make_service()
    s = src(svc)
    _put_package(s, PKG, pages_members("Minutes that will only be touched. The board agreed on the harbor budget"))
    run(svc)
    embedded = svc.embedder.texts_embedded
    s.touch(PKG + "/Data/big-image.png")                       # mtime moves, needed members do not
    run(svc)
    row = files(svc)[PKG]
    assert row["status"] == "indexed"
    assert svc.embedder.texts_embedded == embedded
    s.reads = s.stats = 0
    run(svc)
    assert s.reads == 0 and s.stats == 0                       # and the new walk values were recorded


def test_failed_inner_listing_prevents_deletions(make_service):
    svc = make_service()
    s = src(svc)
    _put_package(s, PKG, pages_members("Minutes text kept safe. The board agreed on the harbor budget"))
    s.write("/other.md", para("other"))
    run(svc)

    s.delete("/other.md")
    s.fail_dirs.add(PKG + "/Index")
    run(svc)
    assert set(files(svc)) == {PKG, "/other.md"}               # incomplete scan: nothing deleted

    s.fail_dirs.clear()
    run(svc)
    assert set(files(svc)) == {PKG}


def test_deleted_package_is_tombstoned(make_service):
    svc = make_service()
    s = src(svc)
    _put_package(s, PKG, pages_members("Minutes to be deleted. The board agreed on the harbor budget"))
    run(svc)
    s.delete(PKG)
    run(svc)
    assert files(svc) == {}


def test_blank_package_id_falls_back_to_a_path_id(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/keep.md", para("keep"))
    run(svc)
    s.delete("/keep.md")
    _put_package(s, PKG, pages_members("Minutes without an id. The board agreed on the harbor budget"))
    orig = s.stat

    def stat(path):
        e = orig(path)
        if e is not None and e.type == "dir":
            e.file_id = ""                                      # as if get_entry had no id for folders
        return e

    s.stat = stat
    run(svc)
    # A source that gives folders no id must not stall deletions: the package
    # gets a path-based id, it is indexed, and the deleted file is pruned.
    rows = files(svc)
    assert set(rows) == {PKG}
    assert rows[PKG]["file_id"].startswith("pkg:") and rows[PKG]["status"] == "indexed"
    assert svc.store.filespace_row("ws1", "fsA")["last_error"] is None


def test_hints_inside_a_package_resolve_to_the_package(make_service):
    svc = make_service()
    s = src(svc)
    _put_package(s, PKG, pages_members("First hinted wording. The board agreed on the harbor budget"))
    run(svc)
    lists = s.lists

    s.write(PKG + "/Index/Document.iwa", iwa([storage("Second hinted wording. The board agreed on the harbor budget")]))
    svc.hint(None, "fsA", "write", PKG + "/Index/Document.iwa")
    svc.scheduler.run_job("ws1", "fsA", full=None)
    assert set(files(svc)) == {PKG}
    assert "Second hinted wording. The board agreed on the harbor budget" in _chunk_text(svc, PKG)
    assert s.lists - lists < 10                                 # targeted: the package only

    svc.hint(None, "fsA", "delete", PKG + "/Index/Metadata.iwa")   # package still there: kept
    svc.scheduler.run_job("ws1", "fsA", full=None)
    assert set(files(svc)) == {PKG}

    s.delete(PKG)
    svc.hint(None, "fsA", "delete", PKG + "/Index/Document.iwa")   # whole package gone
    svc.scheduler.run_job("ws1", "fsA", full=None)
    assert files(svc) == {}


def test_package_root():
    assert package_root("/a/X.pages/Index/Document.iwa") == "/a/X.pages"
    assert package_root("/a/X.pages") is None
    assert package_root("/a/b/c.md") is None
    assert package_root("/Deck.KEY/Data/x.jpg") == "/Deck.KEY"


def test_package_too_large_and_preview_only_when_it_fits(make_service):
    svc = make_service(max_file_bytes=3000)
    s = src(svc)
    _put_package(s, "/Big.pages", {"Index/Document.iwa": iwa([storage("x " * 4000)]) + bytes(4000)})
    _put_package(s, "/Shot.key", {"Index/Document.iwa": iwa([storage("Fits within the limit The board agreed on the harbor budget")]),
                                  "QuickLook/Preview.pdf": make_pdf("Preview words") + bytes(4000)})
    _put_package(s, "/Old.key", {"QuickLook/Preview.pdf": make_pdf("Preview fallback words The board agreed on the harbor budget"),
                                 "Data/huge.jpg": bytes(100_000)})
    seen = _recording(s)
    run(svc)
    led = files(svc)
    assert led["/Big.pages"]["status"] == "skipped" and "too large" in led["/Big.pages"]["reason"]
    assert led["/Shot.key"]["status"] == "indexed"
    assert "/Shot.key/QuickLook/Preview.pdf" not in seen
    assert led["/Old.key"]["status"] == "indexed"                # size includes Data/, but only the PDF is read
    assert "Preview fallback words The board agreed on the harbor budget" in _chunk_text(svc, "/Old.key")


def test_profile_classifies_iwork(make_service):
    svc = make_service()
    s = src(svc)
    _put_package(s, PKG, pages_members("Minutes for the profile. The board agreed on the harbor budget"))
    s.write("/Plan.numbers", zip_bytes(numbers_members(["Gross margin"])))
    _put_package(s, "/Deck.key", keynote_members([("Title words", "Body words", "Note words")]))
    run(svc)
    by_type = {t["type"]: t["files"] for t in svc.profile("ws1", "fsA")["byType"]}
    assert by_type == {"document": 1, "spreadsheet": 1, "presentation": 1}


# ------------------------------------------------------------ block storage (LocalSource)
def test_local_package_and_zip(block, share, reads):  # noqa: F811
    pkg = share / "Board" / "Minutes.pages"
    write_package(pkg, pages_members("Local package minutes text.", "Second local paragraph."))
    (share / "Board" / "Budget.numbers").write_bytes(zip_bytes(numbers_members(["Local budget line for the harbor office"])))
    svc = block()
    run(svc, "NAS")

    led = ledger(svc)
    assert set(led) == {"/Board/Minutes.pages", "/Board/Budget.numbers"}
    row = led["/Board/Minutes.pages"]
    assert row["status"] == "indexed" and row["file_id"] == path_file_id("Board/Minutes.pages")
    assert row["mtime"] == max(os.stat(p).st_mtime_ns for p in pkg.rglob("*"))
    assert "Second local paragraph." in _chunk_text(svc, "/Board/Minutes.pages", fs="NAS")
    assert "Local budget line for the harbor office" in _chunk_text(svc, "/Board/Budget.numbers", fs="NAS")
    assert not any("preview" in r or "/Data/" in r for r in reads)

    reads.clear()
    run(svc, "NAS")
    assert reads == []

    (pkg / "Index" / "Document.iwa").write_bytes(iwa([storage("Edited local minutes. The board agreed on the harbor budget")]))
    os.utime(pkg / "Index" / "Document.iwa", ns=(row["mtime"] + 10**9, row["mtime"] + 10**9))
    run(svc, "NAS")
    assert "Edited local minutes. The board agreed on the harbor budget" in _chunk_text(svc, "/Board/Minutes.pages", fs="NAS")
