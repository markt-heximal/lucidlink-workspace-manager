"""An SDK upgrade that changes the file-id scheme must not re-read the filespace."""
from conftest import files, para, run, src


def _as_if_indexed_by_sdk014(svc, fs="fsA"):
    svc.store.execute("UPDATE filespaces SET id_scheme=NULL WHERE filespace=?", (fs,))


def test_new_id_scheme_rekeys_without_reading_or_embedding(make_service):
    svc = make_service()
    s = src(svc)
    for i in range(4):
        s.write(f"/doc{i}.md", para(f"subject{i}"))
    run(svc)
    before = files(svc)
    chunks = svc.store.chunk_count()
    reads, embedded = s.reads, svc.embedder.texts_embedded
    _as_if_indexed_by_sdk014(svc)

    for f in s.files.values():           # same files, ids from a new scheme
        f["file_id"] = "v1-" + f["file_id"]
    run(svc)

    after = files(svc)
    assert set(after) == set(before)
    assert all(r["file_id"].startswith("v1-") and r["status"] == "indexed" for r in after.values())
    assert s.reads == reads and svc.embedder.texts_embedded == embedded
    assert svc.store.chunk_count() == chunks
    hits = svc.search("ws1", "fsA", "subject2 detail")
    assert hits[0]["path"] == "/doc2.md"

    # One complete scan later the scheme is recorded: a new id at an old path
    # is a new file again, not a re-key.
    for f in s.files.values():
        f["file_id"] = "v2-" + f["file_id"]
    run(svc)
    assert s.reads > reads


def test_changed_file_under_new_id_is_reindexed(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/a.md", para("alpha"))
    run(svc)
    _as_if_indexed_by_sdk014(svc)
    for f in s.files.values():
        f["file_id"] = "v1-" + f["file_id"]
    s.write("/a.md", para("omega"))      # new content too: not a pure re-key
    run(svc)
    hits = svc.search("ws1", "fsA", "omega detail")
    assert hits[0]["path"] == "/a.md"
