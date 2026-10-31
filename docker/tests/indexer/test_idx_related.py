"""Collections step 2: related files, file vectors, and streaming local search."""
from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import KEY, para, run, src

from indexer import related as related_mod
from indexer.app import create_app
from indexer.related import version_base

H = {"X-Internal-Key": KEY}


def paths(items):
    return [x["path"] for x in items]


def story(topic: str, n: int = 30) -> str:
    """A one-chunk paragraph mentioning `topic` among ordinary words."""
    return " ".join(f"The {topic} team met on day {i} to plan the work." for i in range(n))


# ---------------------------------------------------------------- version names
@pytest.mark.parametrize("a,b", [
    ("Proposal v2.docx", "Proposal FINAL (signed).pdf"),
    ("proposal_v3.1.docx", "Proposal - draft.docx"),
    ("Budget 2024-03-01.xlsx", "Budget (2).xlsx"),
    ("Pitch deck final final.key", "pitch-deck copy 2.pptx"),
    ("T03- Shareholder Value Metrics vFinal.pdf", "T03 - Shareholder Value Metrics.pdf"),
])
def test_version_base_strips_version_words(a, b):
    assert version_base(a) == version_base(b) != ""


def test_version_base_keeps_distinct_names():
    assert version_base("Proposal.docx") != version_base("Proposal budget.docx")
    assert version_base("Chapter 3.docx") != version_base("Chapter 4.docx")


# ---------------------------------------------------------------- versions
def test_versions_by_name_and_by_content(make_service):
    svc = make_service()
    a = src(svc, "fsA")
    a.write("/p/Proposal v1.md", story("Lantern"))
    a.write("/p/old/Proposal FINAL.md", story("Lantern") + " Signed by both parties.")
    a.write("/p/renamed-copy.md", story("Lantern"))          # same text, other name
    a.write("/p/Proposal budget.md", para("costs"))           # another document
    a.write("/p/untitled.md", para("one"))
    a.write("/p/untitled (2).md", para("two"))                # generic names are not versions
    src(svc, "fsB").write("/Proposal.pdf.md", para("export"))  # name only, another location
    run(svc, "fsA")
    run(svc, "fsB")
    body = svc.related("ws1", "fsA", "/p/Proposal v1.md")
    v = {x["path"]: x for x in body["versions"]}
    assert set(v) == {"/p/old/Proposal FINAL.md", "/p/renamed-copy.md"}
    assert v["/p/old/Proposal FINAL.md"]["version"]["byName"] is True
    assert v["/p/renamed-copy.md"]["version"] == {"byName": False, "shared": 1.0}
    assert v["/p/renamed-copy.md"]["reason"].startswith("100% of the text in common")
    assert v["/p/renamed-copy.md"]["near"] == "folder"
    assert svc.related("ws1", "fsA", "/p/untitled.md")["versions"] == []


def test_many_backup_copies_of_a_specific_name_are_versions(make_service):
    svc = make_service()
    a = src(svc, "fsA")
    for i in range(60):
        a.write(f"/backup{i}/T03 - Shareholder Value Metrics vFinal.md", para(f"copy{i}" + "x" * i))
        a.write(f"/drafts{i}/Plan.md", para(f"plan{i}"))
    run(svc, "fsA")
    assert len(svc.related("ws1", "fsA", "/backup0/T03 - Shareholder Value Metrics vFinal.md", k=50)["versions"]) == 50
    # Identical copies (same name and size) are shown once, with a count.
    for i in range(3):
        a.write(f"/mirror{i}/T03 - Shareholder Value Metrics vFinal.md", para("copy0"))
    run(svc, "fsA")
    body = svc.related("ws1", "fsA", "/backup1/T03 - Shareholder Value Metrics vFinal.md", k=50)
    same = [x for x in body["versions"] if x["copies"]]
    assert len(same) == 1 and same[0]["copies"] == 3
    assert "(also in 3 other places)" in same[0]["reason"]
    # Sixty unrelated "Plan"s are not versions of one another.
    assert svc.related("ws1", "fsA", "/drafts0/Plan.md")["versions"] == []


def test_versions_with_punctuation_in_the_name(make_service):
    svc = make_service()
    a = src(svc, "fsA")
    a.write("/d/Market Share & Opportunity [board template] - update.md", para("share"))
    a.write("/e/Market Share & Opportunity [board template] - update 2.md", para("other"))
    a.write("/f/Market Share & Opportunity [board template] - update.md", para("share"))   # a copy
    run(svc, "fsA")
    svc.build_file_vectors()
    body = svc.related("ws1", "fsA", "/d/Market Share & Opportunity [board template] - update.md")
    assert set(paths(body["versions"])) == {"/e/Market Share & Opportunity [board template] - update 2.md",
                                            "/f/Market Share & Opportunity [board template] - update.md"}
    assert body["meaning"] == []


def test_versions_by_name_across_locations_and_types(make_service):
    svc = make_service()
    src(svc, "fsA").write("/deck/Pitch v2.md", para("pitch"))
    src(svc, "fsB").write("/out/Pitch FINAL.txt", para("other words entirely"))
    run(svc, "fsA")
    run(svc, "fsB")
    body = svc.related("ws1", "fsA", "/deck/Pitch v2.md")
    assert [(x["filespace"], x["path"]) for x in body["versions"]] == [("fsB", "/out/Pitch FINAL.txt")]
    assert body["versions"][0]["reason"].startswith("Same name: another version")
    # A scope narrows where related files may live.
    narrowed = svc.related("ws1", "fsA", "/deck/Pitch v2.md", scope={"workspaceId": "ws1", "filespace": "fsA"})
    assert narrowed["versions"] == []


# ---------------------------------------------------------------- names
def test_shared_distinctive_names(make_service, monkeypatch):
    monkeypatch.setattr(related_mod, "NAME_DF_FLOOR", 4)
    svc = make_service()
    a = src(svc, "fsA")
    a.write("/a/kickoff.md", story("ThinkTank") + " It is sponsored by Acme.\nSmallest\tAverages\n"
            "14Shareholder Performance1 ONLINESERVICESDIVISION, up to 2011F at 73x")
    a.write("/a/budget.md", para("budget") + " ThinkTank costs, paid by Acme.\nSmallest\tAverages\n"
            "14Shareholder Performance1 ONLINESERVICESDIVISION, up to 2011F at 73x")
    a.write("/a/notes.md", para("notes") + " ThinkTank is mentioned once.")
    for i in range(6):                                       # "Monday" is everywhere: too common to link
        a.write(f"/z/common{i}.md", para(f"filler{i}") + " Monday.")
    a.write("/a/plain.md", para("garden") + " Monday.")
    run(svc, "fsA")
    body = svc.related("ws1", "fsA", "/a/kickoff.md")
    assert {"ThinkTank", "Acme"} <= set(body["names"]["terms"])
    files = body["names"]["files"]
    assert paths(files)[:2] == ["/a/budget.md", "/a/notes.md"]
    assert set(files[0]["names"]) == {"ThinkTank", "Acme"}
    assert files[0]["reason"].startswith("Both mention ")
    assert files[1]["names"] == ["ThinkTank"]
    assert "Monday" not in body["names"]["terms"]
    assert "/a/plain.md" not in paths(files)
    # Table headings and words fused by PDF extraction are not names.
    assert set(body["names"]["terms"]) == {"ThinkTank", "Acme"}


def test_name_like_occurrences():
    from indexer.related import _name_like
    t = "Present: Bryan Wagner, Jessica Soisson. Report by Mr. Glaub.\nSmallest Averages\nThe ThinkTank met."
    assert _name_like(t, t.index("Jessica"))          # after a comma
    assert _name_like(t, t.index("Glaub"))            # after a title
    assert _name_like(t, t.index("ThinkTank"))        # after "The"
    assert not _name_like(t, t.index("Bryan"))       # first word after a colon
    assert not _name_like(t, t.index("Smallest"))     # start of a line
    assert not _name_like(t, t.index("Averages"))     # a heading row


# ---------------------------------------------------------------- meaning
def test_meaning_uses_file_vectors(make_service):
    svc = make_service()
    a = src(svc, "fsA")
    a.write("/g/roses.md", para("roses garden pruning soil"))
    a.write("/g/tulips.md", para("tulips garden pruning soil"))
    a.write("/f/invoice.md", para("invoice payment overdue account"))
    run(svc, "fsA")
    assert svc.build_file_vectors() == 3
    assert svc.build_file_vectors() == 0 and svc.file_vectors_ready
    body = svc.related("ws1", "fsA", "/g/roses.md")
    assert paths(body["meaning"])[0] == "/g/tulips.md"
    assert body["meaning"][0]["reason"] == "About the same thing, in the same folder"
    assert "/g/roses.md" not in paths(body["meaning"])
    assert "meaningPartial" not in body


def test_file_vectors_follow_changes(make_service, tmp_path):
    svc = make_service()
    a = src(svc, "fsA")
    a.write("/g/roses.md", para("roses garden"))
    a.write("/g/tulips.md", para("tulips garden"))
    run(svc, "fsA")
    svc.build_file_vectors()
    model = svc.embedder.model
    fid = svc.store.live_files_at_path("ws1", "fsA", "/g/roses.md")[0]["file_id"]
    before = svc.store.get_file_vector("ws1", "fsA", fid, model)
    assert svc.file_index.count() == 2
    a.write("/g/roses.md", para("invoice payment"))
    run(svc, "fsA", full=True)
    assert svc.store.get_file_vector("ws1", "fsA", fid, model) is None      # dropped on change
    svc.file_index.refresh()
    assert svc.file_index.count() == 1
    assert svc.build_file_vectors() == 1
    after = svc.store.get_file_vector("ws1", "fsA", fid, model)
    assert not np.allclose(before, after)
    svc.file_index.refresh()
    assert svc.file_index.count() == 2
    a.delete("/g/tulips.md")
    run(svc, "fsA", full=True)
    svc.file_index.refresh()
    assert svc.file_index.count() == 1
    # Mean of the chunk vectors, unit length.
    assert abs(float(np.linalg.norm(after)) - 1.0) < 1e-5


# ---------------------------------------------------------------- references
def test_references_both_ways(make_service):
    svc = make_service()
    a = src(svc, "fsA")
    a.write("/mail/kickoff.md", para("kickoff") + " Attached: Budget 2024.xlsx and the Q3 Plan.pdf for review.")
    a.write("/fin/Budget 2024.xlsx.md", para("figures"))
    a.write("/fin/Budget 2024.xlsx", b"PK binary")
    a.write("/plans/Q3 Plan.pdf", b"%PDF binary")
    a.write("/fin/summary.md", para("summary") + " See kickoff.md in mail.")
    a.write("/_gsdata_/sync.log.md", "Copied " + " ".join(f"file{i}.pdf" for i in range(20)) + " Q3 Plan.pdf")
    run(svc, "fsA")
    body = svc.related("ws1", "fsA", "/mail/kickoff.md")
    mentions = body["references"]["mentions"]
    assert set(paths(mentions)) == {"/fin/Budget 2024.xlsx", "/plans/Q3 Plan.pdf"}
    assert {x["reason"] for x in mentions} == {"This file names Budget 2024.xlsx, edited the same week",
                                               "This file names Q3 Plan.pdf, edited the same week"}
    # The PDF is not indexed, yet it is named; its own page lists who names it.
    pdf = svc.related("ws1", "fsA", "/plans/Q3 Plan.pdf")
    assert pdf["file"]["indexed"] is False
    assert paths(pdf["references"]["mentionedBy"]) == ["/mail/kickoff.md"]
    assert "Q3 Plan" in pdf["references"]["mentionedBy"][0]["snippet"]


# ---------------------------------------------------------------- route + collection
def test_related_route_and_collection_pins(make_service):
    svc = make_service()
    a = src(svc, "fsA")
    a.write("/p/Proposal v1.md", story("Lantern") + " See Notes.txt for details.")
    a.write("/p/Proposal v2.md", para("revised"))
    a.write("/p/Notes.txt", b"plain notes")
    run(svc, "fsA")
    with TestClient(create_app(lambda: svc, start_scheduler=False)) as c:
        r = c.post("/index/related", headers=H, json={"filespace": "fsA", "path": "/p/Proposal v1.md"})
        assert r.status_code == 200
        body = r.json()
        assert set(body) >= {"file", "versions", "names", "meaning", "references", "collection"}
        assert body["file"]["path"] == "/p/Proposal v1.md" and body["file"]["indexed"] is True
        assert body["collection"]["name"] == "Proposal v1"
        assert [p["path"] for p in body["collection"]["pinned"]] == ["/p/Proposal v1.md", "/p/Proposal v2.md",
                                                                     "/p/Notes.txt"]
        assert body["meaningPartial"] is True        # the builder has not run yet
        assert c.post("/index/related", headers=H, json={"filespace": "fsA", "path": "/nope.md"}).status_code == 404
        assert c.post("/index/related", headers=H, json={"filespace": "fsA", "path": "p"}).status_code == 422
        assert c.post("/index/related", headers=H,
                      json={"filespace": "fsA", "path": "/p/Notes.txt", "k": 51}).status_code == 422
        assert c.post("/index/related", json={"filespace": "fsA", "path": "/p/Notes.txt"}).status_code == 401


def test_corpus_sample_draws_many_chunks():
    """The word-frequency sample (refinements, rare names) spans the corpus:
    it once re-seeded per draw and so held a single chunk."""
    from indexer.store import Store
    s = Store(":memory:")
    for i in range(50):
        s.replace_chunks("w", "f", f"id{i}", [(f"h{i}", f"text {i}")])
    assert len(s.sample_texts(2000, seed=5)) == 50


# ---------------------------------------------------------------- streaming local search
def test_local_search_streams_above_max_points(make_service):
    svc = make_service()
    a = src(svc, "fsA")
    for i, t in enumerate(["roses garden", "tulips garden", "invoice payment", "taxes filing", "garden soil"]):
        a.write(f"/d/{i}.md", para(t))
    run(svc, "fsA")
    local = svc.vectors.local
    q = svc.embedder.embed_query("garden")
    scopes = {("ws1", "fsA")}
    in_memory = local.search(q, 3, scopes)
    local.max_points = 0
    assert local.streaming
    streamed = local.search(q, 3, scopes)
    every = dict(local._scan(q, 10, scopes, None, None))
    assert len(every) == 5
    # Same best scores; equal scores may come in either order.
    assert np.allclose([s for _, s in streamed], [s for _, s in in_memory], atol=1e-5)
    assert all(abs(every[p] - s) < 1e-5 for p, s in in_memory + streamed)
    assert local.search(q, 3, scopes, path_prefix="/nowhere") == []


def test_limited_search_never_loads_every_vector(make_service):
    svc = make_service()
    a = src(svc, "fsA")
    a.write("/a.md", para("roses garden"))
    a.write("/b.md", para("tulips garden"))
    run(svc, "fsA")
    local = svc.vectors.local
    body = svc.search_all(None, None, "garden", files=[{"workspaceId": "ws1", "filespace": "fsA", "path": "/b.md"}])
    assert paths(body["files"]) == ["/b.md"]
    assert local._gen == -1          # the full matrix was never loaded
