"""Collections step 3: discovered collections, and suggestions for a collection."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from conftest import KEY, para, run, src

from indexer import discover as discover_mod
from indexer.app import create_app

H = {"X-Internal-Key": KEY}


def doc(topic: str, *names: str, extra: str = "") -> str:
    """A paragraph about `topic` that mentions `names` the way prose does."""
    said = " ".join(f"We met with {n} about it." for n in names)
    return " ".join(f"The {topic} plan covers step {i}." for i in range(25)) + " " + said + " " + extra


def files_of(group) -> set[str]:
    return {f["path"] for f in group["items"]}


@pytest.fixture
def archive(make_service, monkeypatch):
    """Two projects (Lantern, Orchid) spread over folders, plus unrelated files."""
    monkeypatch.setattr(discover_mod, "NAME_NODES_MAX", 5)   # "Monday" (6 files) is common

    def build(**kw):
        svc = make_service(**kw)
        a = src(svc, "fsA")
        a.write("/Backup/Projects/Lantern/brief.md", doc("lighting", "Quillfeather", "Lanternix"))   # an older copy
        a.write("/Projects/Lantern/brief.md", doc("lighting", "Quillfeather", "Lanternix"))
        a.write("/Finance/2024/lantern-budget.md", doc("budget", "Quillfeather", "Lanternix"))
        a.write("/Mail/kickoff.md", doc("kickoff", "Quillfeather", "Lanternix", extra="Attached: brief.md for review."))
        a.write("/Mail/followup.md", doc("followup", "Lanternix", "Quillfeather"))
        a.write("/Projects/Orchid/plan.md", doc("greenhouse", "Zanbury", "Orchidra"))
        a.write("/Finance/orchid-costs.md", doc("costs", "Zanbury", "Orchidra"))
        a.write("/Mail/orchid-update.md", doc("update", "Orchidra", "Zanbury"))
        for i, topic in enumerate(["sailing", "pottery", "chess", "baking", "cycling", "astronomy"]):
            a.write(f"/Misc/{topic}.md", para(f"{topic} hobby notes") + " See you Monday.")
        a.write("/Projects/Lantern/brief.md.json", '{"source": "brief.md", "pages": 1}')           # a sidecar
        run(svc, "fsA")
        svc.build_file_vectors()
        svc.build_file_vectors()
        assert svc.file_vectors_ready
        return svc
    return build


def group_with(svc, path, status="proposed"):
    for g in svc.discover_groups("ws1", "fsA", status, limit=200)["groups"]:
        d = svc.discover_group(g["id"])
        if path in files_of(d):
            return d
    return None


# ---------------------------------------------------------------- pieces
def test_copies_and_sidecars_fold_into_one_document():
    rows = [{"workspace_id": "w", "filespace": "f", "file_id": str(i), "path": p, "size": s, "mtime": m,
             "status": "indexed", "chunk_count": 1}
            for i, (p, s, m) in enumerate([("/a/Deck.pptx", 10, 2), ("/b/Deck.pptx", 10, 5),
                                          ("/a/Deck.pptx.json", 3, 2), ("/c/Deck.pptx", 11, 1),
                                          ("/d/data.json", 4, 1)])]
    nodes = discover_mod.fold_nodes(rows)
    by_rep = {n.rep["path"]: n for n in nodes}
    assert set(by_rep) == {"/b/Deck.pptx", "/c/Deck.pptx"}          # data files are not documents
    assert [r["path"] for r in by_rep["/b/Deck.pptx"].files] == ["/b/Deck.pptx", "/a/Deck.pptx", "/a/Deck.pptx.json"]
    assert by_rep["/b/Deck.pptx"].copies == 1


def test_only_documents_outside_system_folders():
    assert discover_mod.wanted("/Projects/Plan.docx") and discover_mod.wanted("/Board/Deck.pdf")
    assert not discover_mod.wanted("/Projects/notes.json")
    assert not discover_mod.wanted("/Documents and Settings/mt/Local Settings/Temporary Internet Files/x.html")
    assert not discover_mod.wanted("/Old Mac/Applications/Calculator.app/Contents/Resources/Help.html")
    assert not discover_mod.wanted("/mt/AppData/Roaming/report.txt")


def test_machine_codes_are_not_names():
    from indexer.related import scan_names
    tf, _, _ = scan_names(["We met DC78AACC and e25bb512f2a8 with Wagner, then W2008 and Q408 shipped."])
    assert {"wagner", "w2008", "q408"} <= set(tf) and not {"dc78aacc", "e25bb512f2a8"} & set(tf)


def test_louvain_separates_two_clusters():
    adj = {}
    def link(a, b, w=1.0):
        adj.setdefault(a, {})[b] = w
        adj.setdefault(b, {})[a] = w
    for grp in ((0, 1, 2, 3), (4, 5, 6, 7)):
        for i in grp:
            for j in grp:
                if i < j:
                    link(i, j)
    link(3, 4, 0.1)
    assert sorted(discover_mod.louvain(adj)) == [[0, 1, 2, 3], [4, 5, 6, 7]]


def test_nearest_matches_brute_force():
    import numpy as np
    rng = np.random.default_rng(3)
    v = rng.normal(size=(300, 16)).astype(np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    old = discover_mod.BLOCK, discover_mod.CHUNK
    discover_mod.BLOCK, discover_mod.CHUNK = 64, 100        # many steps, uneven edges
    try:
        idx, sim = discover_mod.nearest(v.astype(np.float16), 5)
    finally:
        discover_mod.BLOCK, discover_mod.CHUNK = old
    full = v.astype(np.float16).astype(np.float32) @ v.astype(np.float16).astype(np.float32).T
    np.fill_diagonal(full, -np.inf)
    want = np.argsort(-full, axis=1)[:, :5]
    assert (np.sort(idx, axis=1) == np.sort(want, axis=1)).mean() > 0.99
    assert not (idx == np.arange(300)[:, None]).any()


def test_describe():
    folders = [{"folder": "Projects", "files": 9}, {"folder": "Finance", "files": 8}, {"folder": "Mail", "files": 6}]
    assert discover_mod.describe(23, folders, "2023-02-01T00:00:00Z", "2025-06-01T00:00:00Z") == \
        "23 files across Projects, Finance and Mail, 2023–2025"
    assert discover_mod.describe(4, folders[:1], "2024-01-01T00:00:00Z", "2024-03-01T00:00:00Z") == \
        "4 files in Projects, 2024"
    assert discover_mod.describe(9, folders + [{"folder": "Notes", "files": 1}], None, None) == \
        "9 files across Projects, Finance, Mail and 1 more"


def test_parse_name():
    assert discover_mod.parse_name('```json\n{"name": "\\"Lantern programme\\"", "summary": "Plans."}\n```') == \
        ("Lantern programme", "Plans.")
    with pytest.raises(ValueError):
        discover_mod.parse_name('{"summary": "no name"}')


# ---------------------------------------------------------------- runs
def test_discovery_finds_the_projects(archive):
    svc = archive()
    svc.discover_start("ws1", "fsA")
    assert svc.discover_status()["locations"][0]["state"] == "waiting"
    svc.discover_pass()
    status = svc.discover_status()["locations"][0]
    assert status["state"] == "done" and status["enabled"] and status["stats"]["groups"] >= 2
    lantern = group_with(svc, "/Projects/Lantern/brief.md")
    orchid = group_with(svc, "/Projects/Orchid/plan.md")
    assert files_of(lantern) == {"/Projects/Lantern/brief.md", "/Finance/2024/lantern-budget.md",
                                 "/Mail/kickoff.md", "/Mail/followup.md"}
    assert files_of(orchid) == {"/Projects/Orchid/plan.md", "/Finance/orchid-costs.md", "/Mail/orchid-update.md"}
    assert {n["name"] for n in lantern["names"]} == {"Quillfeather", "Lanternix"}
    assert lantern["namedBy"] == "auto" and lantern["name"] in ("Quillfeather · Lanternix", "Lanternix · Quillfeather")
    assert lantern["description"].startswith("4 files across ")
    item = next(f for f in lantern["items"] if f["path"] == "/Projects/Lantern/brief.md")
    assert item["reason"].startswith("Mentions ") and item["copies"] == 1      # the backup copy is folded in
    assert sum(t["files"] for t in lantern["timeline"]) == 4
    assert {p["path"] for p in lantern["collection"]["pinned"]} == files_of(lantern)
    assert lantern["collection"]["name"] == lantern["name"]
    for g in svc.discover_groups("ws1", "fsA")["groups"]:
        assert "/Misc/sailing.md" not in files_of(svc.discover_group(g["id"])) or g["id"] not in (
            lantern["id"], orchid["id"])


def test_rerun_remembers_decisions(archive):
    svc = archive()
    svc.discover_start("ws1", "fsA")
    svc.discover_pass()
    lantern = group_with(svc, "/Projects/Lantern/brief.md")
    orchid = group_with(svc, "/Projects/Orchid/plan.md")
    svc.discover_act(lantern["id"], "rename", "Lantern programme")
    svc.discover_act(orchid["id"], "dismiss")
    # A new Lantern file arrives; the next run keeps the id and the name.
    src(svc, "fsA").write("/Mail/lantern-minutes.md", doc("minutes", "Quillfeather", "Lanternix"))
    run(svc, "fsA")
    svc.build_file_vectors()
    svc.discover_start("ws1", "fsA")
    svc.discover_pass()
    again = group_with(svc, "/Projects/Lantern/brief.md")
    assert again["id"] == lantern["id"] and again["name"] == "Lantern programme" and again["namedBy"] == "you"
    assert "/Mail/lantern-minutes.md" in files_of(again)
    assert group_with(svc, "/Projects/Orchid/plan.md") is None                 # dismissed stays dismissed
    assert group_with(svc, "/Projects/Orchid/plan.md", "dismissed")["id"] == orchid["id"]
    assert svc.discover_status()["locations"][0]["stats"]["still_dismissed"] == 1
    # Restore brings it back; keep answers with the collection to save.
    assert svc.discover_act(orchid["id"], "restore")["status"] == "proposed"
    kept = svc.discover_act(lantern["id"], "keep")
    assert kept["status"] == "kept" and len(kept["collection"]["pinned"]) == 5


def test_ai_names_proposals(archive):
    calls = []
    svc = archive(llm_answer='{"name": "Lantern programme", "summary": "Planning the Lantern lighting work."}',
                  llm_calls=calls)
    svc.discover_start("ws1", "fsA")
    svc.discover_pass()
    lantern = group_with(svc, "/Projects/Lantern/brief.md")
    assert lantern["namedBy"] == "ai" and lantern["name"] == "Lantern programme"
    assert lantern["summary"] == "Planning the Lantern lighting work."
    prompt = json.dumps(calls)
    assert "Quillfeather" in prompt and "/Finance/2024/lantern-budget.md" in prompt
    n = len(calls)
    svc.discover_pass()                                     # nothing left to name
    assert len(calls) == n
    assert svc.discover_status()["locations"][0]["unnamed"] == 0


def test_bad_ai_answer_keeps_the_made_up_name(archive):
    svc = archive(llm_answer="I cannot help with that.")
    svc.discover_start("ws1", "fsA")
    svc.discover_pass()
    assert group_with(svc, "/Projects/Lantern/brief.md")["namedBy"] == "auto"


def test_nothing_runs_while_paused_or_before_file_vectors(archive):
    svc = archive()
    svc.discover_start("ws1", "fsA")
    svc.set_control(paused=True)
    svc.discover_pass()
    assert svc.discover_status()["locations"][0]["state"] == "waiting"
    svc.set_control(paused=False)
    svc.file_vectors_ready = False
    svc.discover_pass()
    assert svc.discover_status()["locations"][0]["state"] == "waiting"


def test_merge(archive):
    svc = archive()
    svc.discover_start("ws1", "fsA")
    svc.discover_pass()
    a = group_with(svc, "/Projects/Lantern/brief.md")
    b = group_with(svc, "/Projects/Orchid/plan.md")
    merged = svc.discover_merge([a["id"], b["id"]], "Gardens and lights")
    assert merged["status"] == "proposed" and merged["namedBy"] == "you"
    assert files_of(merged) == files_of(a) | files_of(b)
    assert svc.discover_group(a["id"])["status"] == "merged"
    assert svc.discover_group(a["id"])["mergedInto"] == merged["id"]
    with pytest.raises(ValueError):
        svc.discover_act(a["id"], "keep")


# ---------------------------------------------------------------- suggestions
def test_suggest_files_for_a_collection(archive):
    svc = archive()
    pins = [{"workspaceId": "ws1", "filespace": "fsA", "path": p}
            for p in ("/Projects/Lantern/brief.md", "/Finance/2024/lantern-budget.md", "/Mail/kickoff.md")]
    body = svc.collection_suggest(pins)
    assert {"Quillfeather", "Lanternix"} <= set(body["names"])
    got = [f["path"] for f in body["files"]]
    assert got[0] == "/Mail/followup.md"
    assert body["files"][0]["reason"].startswith("Mentions ")
    assert "/Projects/Lantern/brief.md" not in got and "/Backup/Projects/Lantern/brief.md" not in got
    assert not any(p.startswith("/Projects/Orchid") for p in got)
    # Excluded files and files older than `since` are left out.
    excl = svc.collection_suggest(pins, excluded=[{"workspaceId": "ws1", "filespace": "fsA",
                                                   "path": "/Mail/followup.md"}])
    assert "/Mail/followup.md" not in [f["path"] for f in excl["files"]]
    assert svc.collection_suggest(pins, since="2999-01-01T00:00:00Z")["files"] == []


# ---------------------------------------------------------------- routes
def test_routes(archive):
    svc = archive()
    with TestClient(create_app(lambda: svc, start_scheduler=False)) as c:
        assert c.post("/index/discover/start", headers=H, json={"workspaceId": "ws1", "filespace": "nope"}
                      ).status_code == 404
        r = c.post("/index/discover/start", headers=H, json={"workspaceId": "ws1", "filespace": "fsA"})
        assert r.status_code == 200 and r.json()["state"] == "waiting"
        svc.discover_pass()
        st = c.get("/index/discover", headers=H).json()
        assert st["locations"][0]["groups"]["proposed"] >= 2
        assert c.get("/index/status", headers=H).json()["discovered"] == {"locations": 1, "running": None}
        groups = c.get("/index/discover/groups", headers=H, params={"workspaceId": "ws1", "filespace": "fsA"}).json()
        assert groups["total"] >= 2 and all("items" not in g for g in groups["groups"])
        gid = groups["groups"][0]["id"]
        one = c.get(f"/index/discover/groups/{gid}", headers=H).json()
        assert {"items", "timeline", "collection", "names", "description"} <= set(one)
        assert c.get("/index/discover/groups/zzz", headers=H).status_code == 404
        assert c.get("/index/discover/groups", headers=H, params={"status": "odd"}).status_code == 400
        assert c.post(f"/index/discover/groups/{gid}", headers=H, json={"action": "rename", "name": " "}
                      ).status_code == 400
        assert c.post(f"/index/discover/groups/{gid}", headers=H, json={"action": "explode"}).status_code == 422
        assert c.post(f"/index/discover/groups/{gid}", headers=H, json={"action": "keep"}).json()["status"] == "kept"
        assert c.post("/index/discover/merge", headers=H, json={"ids": [gid]}).status_code == 422
        assert c.post("/index/discover/merge", headers=H, json={"ids": [gid, "zzz"]}).status_code == 404
        pins = [{"workspaceId": "ws1", "filespace": "fsA", "path": "/Mail/kickoff.md"}]
        s = c.post("/index/collection/suggest", headers=H, json={"pinned": pins, "k": 5})
        assert s.status_code == 200 and len(s.json()["files"]) <= 5
        assert c.post("/index/collection/suggest", headers=H, json={"pinned": pins, "since": "soon"}).status_code == 400
        assert c.post("/index/discover/stop", headers=H, json={"workspaceId": "ws1", "filespace": "fsA"}
                      ).json()["enabled"] is False
        assert c.get("/index/discover", headers=H).json()["locations"][0]["enabled"] is False
        assert c.get("/index/discover", headers={}).status_code == 401
