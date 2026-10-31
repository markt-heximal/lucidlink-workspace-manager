"""Search quality: keyword index (FTS5), hybrid ranking, name folding, All search."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from conftest import KEY, para, run, src

from indexer.app import create_app
from indexer.search import QueryPlan
from indexer.store import Store

H = {"X-Internal-Key": KEY}

# FakeEmbedder hashes words into 64 buckets; "tau" shares a bucket with
# "thinktank", so documents full of "tau" are the vague semantic neighbours that
# pure vector search ranks above the one document that literally says ThinkTank.
LITERAL = "/Notes/offsite recap.md"


@contextmanager
def client(svc):
    with TestClient(create_app(lambda: svc, start_scheduler=False)) as c:
        yield c


def corpus(svc, fs="fsA"):
    s = src(svc, fs)
    s.write(LITERAL, para("garden roses pruning schedule") + " We met the ThinkTank team after lunch. "
            + para("compost watering mulch"))
    for i in range(8):
        s.write(f"/Strategy/forum{i}.md", para(f"tau tau strategy forum {i}"))
    run(svc, fs)


def paths(rows):
    return [r["path"] for r in rows]


# ---------------------------------------------------------------- ranking
def test_literal_term_ranks_first_where_vectors_alone_miss_it(make_service):
    svc = make_service()
    corpus(svc)
    q = svc.embedder.embed_query("ThinkTank")
    vec_only = svc.vectors.search(q, 3, {("ws1", "fsA")})
    rows = svc.store.chunk_rows([p for p, _ in vec_only])
    assert LITERAL not in {rows[p]["path"] for p, _ in vec_only}       # the bug: vectors miss it

    hits = svc.search("ws1", "fsA", "ThinkTank")
    assert hits[0]["path"] == LITERAL
    assert "ThinkTank" in hits[0]["snippet"]                           # snippet starts near the mention
    assert all(0 <= h["score"] <= 1 for h in hits) and hits == sorted(hits, key=lambda h: -h["score"])
    body = svc.search_all("ws1", "fsA", "ThinkTank")
    assert body["files"][0]["path"] == LITERAL
    assert body["files"][0]["why"]["keyword"] is True


def test_ask_retrieves_the_literal_doc(make_service):
    calls = []
    svc = make_service(llm_answer="See /Notes/offsite recap.md.", llm_calls=calls)
    corpus(svc)
    res = svc.ask("ws1", "fsA", "What did ThinkTank say?", k=3)
    assert LITERAL in res["sources"]
    prompt = calls[0]["messages"][-1]["content"]
    assert "ThinkTank" in prompt


@pytest.mark.parametrize("query,text", [
    ("ThinkTank", "the Think Tank met"),
    ("ThinkTank", "the think-tank met"),
    ("ThinkTank", "the think_tank met"),
    ("think tank", "the ThinkTank met"),
    ("think tank", "the thinktank met"),
    ("Think-Tank", "the ThinkTank met"),
])
def test_compound_and_camelcase_variants(make_service, query, text):
    svc = make_service()
    s = src(svc)
    s.write("/hit.md", para("garden roses") + f" Note: {text} today. " + para("compost"))
    for i in range(4):
        s.write(f"/d{i}.md", para(f"tau tau forum {i}"))
    run(svc)
    body = svc.search_all("ws1", "fsA", query, mode="keyword")
    assert paths(body["files"]) == ["/hit.md"]
    assert svc.search("ws1", "fsA", query)[0]["path"] == "/hit.md"


@pytest.mark.parametrize("query", ['"', 'think"tank', "a:b", "*", "**ThinkTank*", "NEAR(x y)", "-", "AND OR NOT",
                                   "col:ThinkTank", "^start", "(((", "'", "x AND", "日本語", "ÜBER café"])
def test_fts_syntax_never_breaks_search(make_service, query):
    svc = make_service()
    corpus(svc)
    body = svc.search_all("ws1", "fsA", query)
    assert isinstance(body["results"], list)
    svc.search_all("ws1", "fsA", query, mode="keyword", filters={"include": [query], "exclude": [query]})


def test_query_plan_variants():
    p = QueryPlan("ThinkTank")
    assert p.match == '("thinktank"* OR "think tank")' and p.compact == "thinktank"
    p = QueryPlan("think tank")
    assert p.match == '("think" AND "tank"*) OR "thinktank"'
    assert QueryPlan('":*()').match is None
    assert QueryPlan("ai").match == '"ai"'            # no prefix on very short tokens


def test_diacritics_fold(make_service):
    svc = make_service()
    src(svc).write("/cafe.md", para("garden") + " Meeting at the Café Müller. " + para("roses"))
    run(svc)
    assert paths(svc.search_all("ws1", "fsA", "cafe muller", mode="keyword")["files"]) == ["/cafe.md"]


# ---------------------------------------------------------------- FTS sync
def fts_ids(svc, term):
    return {r["rowid"] for r in svc.store.query("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", (term,))}


def test_fts_follows_updates_deletes_and_rekeys(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/a.md", para("alpha") + " zebracorn " + para("beta"))
    run(svc)
    assert fts_ids(svc, "zebracorn")
    s.write("/a.md", para("alpha") + " unicornfish " + para("beta"))      # content change
    run(svc)
    assert not fts_ids(svc, "zebracorn") and fts_ids(svc, "unicornfish")
    pid = next(iter(fts_ids(svc, "unicornfish")))
    svc.store.execute("UPDATE chunks SET text='narwhal text' WHERE point_id=?", (pid,))
    assert fts_ids(svc, "narwhal") == {pid} and pid not in fts_ids(svc, "unicornfish")
    s.delete("/a.md")
    run(svc)
    assert not fts_ids(svc, "unicornfish") and not fts_ids(svc, "narwhal")
    s.write("/b.md", para("gamma") + " platypus ")
    run(svc)
    svc.store.purge_source("ws1", "fsA")
    assert not fts_ids(svc, "platypus")
    # Integrity of the external-content index after all of that.
    svc.store.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES ('integrity-check', 1)")


def test_existing_db_backfills_and_falls_back_until_ready(make_service, tmp_path):
    db = str(tmp_path / "old.db")
    svc = make_service(db_path=db)
    corpus(svc)
    # Make it look like a database from before the keyword index.
    svc.store.close()
    c = sqlite3.connect(db)
    for t in ("chunks_fts_ai", "chunks_fts_ad", "chunks_fts_au"):
        c.execute(f"DROP TRIGGER {t}")
    c.execute("DROP TABLE chunks_fts_vocab")
    c.execute("DROP TABLE chunks_fts")
    c.execute("DELETE FROM meta")
    c.commit()
    c.close()

    svc2 = make_service(db_path=db)
    st = svc2.store
    assert st.fts_available and not st.fts_ready and st.fts_hole()[1] > 0
    # Before it is ready: meaning only, as before (score = cosine, the literal doc missed).
    before = svc2.search_all("ws1", "fsA", "ThinkTank", k=3)
    assert LITERAL not in paths(before["results"]) and before["keywordReady"] is False
    # Writes during the backfill are indexed right away and deletes of not-yet-indexed rows are safe.
    svc2.sources = svc.sources                      # same files on "disk"
    src(svc2).write("/new.md", para("fresh") + " ThinkTank again ")
    src(svc2).delete("/Strategy/forum0.md")
    run(svc2)
    assert not st.fts_backfill_step(batch=3)        # partial progress
    assert st.fts_backfill()
    assert st.fts_ready and st.fts_hole() == (0, 0)
    st.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES ('integrity-check', 1)")
    after = svc2.search_all("ws1", "fsA", "ThinkTank")
    assert set(paths(after["files"][:2])) == {LITERAL, "/new.md"} and "keywordReady" not in after


def test_backfill_runs_in_background_on_start(make_service, tmp_path):
    db = str(tmp_path / "bg.db")
    svc = make_service(db_path=db)
    corpus(svc)
    svc.store.fts_rebuild()
    assert not svc.store.fts_ready
    svc._start_fts_backfill()
    svc._fts_thread.join(10)
    assert svc.store.fts_ready


def test_no_fts5_disables_keyword_search(make_service, monkeypatch):
    svc = make_service()
    corpus(svc)
    monkeypatch.setattr(svc.store, "fts_available", False)
    hits = svc.search("ws1", "fsA", "ThinkTank", k=3)
    assert hits and LITERAL not in paths(hits)         # meaning only, nothing raised
    assert svc.search_all("ws1", "fsA", "ThinkTank", mode="keyword")["keywordReady"] is False


def test_store_without_fts5_module(tmp_path, monkeypatch):
    monkeypatch.setattr(Store, "_init_fts", lambda self: False)    # what a probe failure returns
    st = Store(str(tmp_path / "x.db"))
    assert not st.fts_available and not st.fts_ready and st.keyword_search([("a", "b")], '"x"', 5) == []
    assert st.fts_backfill()
    st.close()


# ---------------------------------------------------------------- find
def test_find_folds_case_spaces_hyphens_and_camelcase(make_service):
    svc = make_service()
    s = src(svc)
    for p in ("/a/Think Tank.docx", "/b/think-tank notes.md", "/c/ThinkTank", "/d/old_think_tank_v2.pdf",
              "/ThinkTank Archive/readme.md", "/e/thinking about tanks.md", "/f/Report.v2.final.pdf"):
        s.write(p, "%PDF-1.4 x" if p.endswith(".pdf") else para("x"))
    run(svc)
    names = [r["name"] for r in svc.find("ws1", "fsA", "thinktank")]
    # exact (with or without extension) > starts with > contains > path only; newest first within a tier
    assert names[:2] == ["ThinkTank", "Think Tank.docx"]
    assert names[2:] == ["think-tank notes.md", "old_think_tank_v2.pdf", "readme.md"]
    assert [r["name"] for r in svc.find("ws1", "fsA", "Think Tank")][:2] == ["ThinkTank", "Think Tank.docx"]
    assert [r["name"] for r in svc.find("ws1", "fsA", "think_tank")][:2] == ["ThinkTank", "Think Tank.docx"]
    assert [r["name"] for r in svc.find("ws1", "fsA", "report v2")] == ["Report.v2.final.pdf"]
    assert svc.find("ws1", "fsA", "100%_") == []


def test_find_fold_columns_follow_renames_and_migrate(make_service, tmp_path):
    db = str(tmp_path / "f.db")
    svc = make_service(db_path=db)
    s = src(svc)
    s.write("/x/Quarterly Plan.md", para("x"))
    run(svc)
    s.move("/x/Quarterly Plan.md", "/x/Annual-Review.md")
    run(svc)
    assert [r["name"] for r in svc.find("ws1", "fsA", "annualreview")] == ["Annual-Review.md"]
    assert svc.find("ws1", "fsA", "quarterlyplan") == []
    svc.store.close()
    c = sqlite3.connect(db)
    c.execute("DROP TRIGGER files_fold_ai")
    c.execute("DROP TRIGGER files_fold_au")
    c.execute("ALTER TABLE files DROP COLUMN name_fold")
    c.execute("ALTER TABLE files DROP COLUMN path_fold")
    c.commit()
    c.close()
    svc2 = make_service(db_path=db)
    assert [r["name"] for r in svc2.find("ws1", "fsA", "Annual Review")] == ["Annual-Review.md"]


# ---------------------------------------------------------------- scopes
def test_scopes_and_path_prefix_respected(make_service):
    svc = make_service()
    src(svc, "fsA").write("/in/a.md", para("garden") + " ThinkTank ")
    src(svc, "fsA").write("/out/b.md", para("garden") + " ThinkTank ")
    src(svc, "fsB").write("/in/c.md", para("garden") + " ThinkTank ")
    run(svc, "fsA")
    run(svc, "fsB")
    assert set(paths(svc.search_all("ws1", None, "ThinkTank", mode="keyword")["files"])) == {"/in/a.md", "/out/b.md", "/in/c.md"}
    assert set(paths(svc.search_all("ws1", "fsB", "ThinkTank", mode="keyword")["files"])) == {"/in/c.md"}
    pre = svc.search_all("ws1", "fsA", "ThinkTank", path_prefix="/in")
    assert set(paths(pre["files"])) == {"/in/a.md"} and set(paths(pre["results"])) == {"/in/a.md"}
    flt = svc.search_all("ws1", "fsA", "ThinkTank", filters={"pathPrefix": "/out"})
    assert set(paths(flt["files"])) == {"/out/b.md"}
    # path_prefix with LIKE metacharacters
    assert svc.search_all("ws1", "fsA", "ThinkTank", path_prefix="/i%")["files"] == []


# ---------------------------------------------------------------- All search
def all_corpus(svc):
    s = src(svc)
    now = int(time.time())
    old = 1_600_000_000            # 2020-09
    s.write("/Clients/Acme/ThinkTank brief.docx.md", para("client brief budget"), mtime=now - 86400)
    s.write("/Clients/Acme/kickoff.md", para("kickoff agenda budget") + " The ThinkTank joins. ", mtime=now - 2 * 86400)
    s.write("/Clients/Acme/minutes.md", para("minutes budget") + " ThinkTank, ThinkTank and the think tank. ",
            mtime=old)
    s.write("/Archive/2020/deck.pptx.md", para("archive slides") + " ThinkTank ", mtime=old + 100)
    for i in range(3):
        s.write(f"/Strategy/forum{i}.md", para(f"tau tau strategy forum {i}"), mtime=old + 200 + i)
    run(svc)


def test_all_search_files_why_counts_highlights(make_service):
    svc = make_service()
    all_corpus(svc)
    with client(svc) as c:
        body = c.post("/index/search", json={"query": "ThinkTank", "workspaceId": "ws1", "filespace": "fsA"},
                      headers=H).json()
    assert set(body) >= {"results", "files", "total", "counts", "refinements"}
    files = {f["path"]: f for f in body["files"]}
    f0 = body["files"][0]
    assert set(f0) == {"workspaceId", "filespace", "kind", "location", "path", "name", "size", "mtime", "type",
                       "score", "why", "snippets"}
    assert set(f0["why"]) == {"meaning", "keyword", "name", "mentions"}
    brief = files["/Clients/Acme/ThinkTank brief.docx.md"]
    assert brief["why"]["name"] is True
    minutes = files["/Clients/Acme/minutes.md"]
    assert minutes["why"]["keyword"] is True and minutes["why"]["mentions"] >= 3
    sn = minutes["snippets"][0]
    assert sn["keyword"] is True and sn["highlights"]
    for s0, e0 in sn["highlights"]:
        assert sn["text"][s0:e0].lower().replace(" ", "").replace("-", "") == "thinktank"
    forum = [f for p, f in files.items() if p.startswith("/Strategy")]
    assert all(f["why"]["meaning"] and not f["why"]["keyword"] for f in forum)
    c_ = body["counts"]
    assert c_["keyword"] == 3 and c_["name"] == 1 and c_["both"] == sum(
        1 for f in body["files"] if f["why"]["meaning"] and f["why"]["keyword"])
    assert c_["meaning"] == sum(1 for f in body["files"] if f["why"]["meaning"])
    assert body["total"] == len(body["files"])
    # keyword files outrank meaning-only ones
    kinds = [bool(f["why"]["keyword"] or f["why"]["name"]) for f in body["files"]]
    assert kinds == sorted(kinds, reverse=True)
    assert len(f0["snippets"]) <= 3


def test_mode_switching(make_service):
    svc = make_service()
    all_corpus(svc)
    kw = svc.search_all("ws1", "fsA", "ThinkTank", mode="keyword")
    assert {f["path"] for f in kw["files"]} == {"/Clients/Acme/ThinkTank brief.docx.md", "/Clients/Acme/kickoff.md",
                                                 "/Clients/Acme/minutes.md", "/Archive/2020/deck.pptx.md"}
    assert all(not f["why"]["meaning"] for f in kw["files"])
    mean = svc.search_all("ws1", "fsA", "ThinkTank", mode="meaning")
    assert mean["files"] and all(f["why"]["meaning"] for f in mean["files"])
    assert {f["path"] for f in mean["files"]} >= {"/Strategy/forum0.md"}
    # keyword mode needs no embedder at all
    svc.embedder.embed_query = lambda q: (_ for _ in ()).throw(AssertionError("embedded"))
    assert svc.search_all("ws1", "fsA", "ThinkTank", mode="keyword")["files"]


def test_filters(make_service):
    svc = make_service()
    all_corpus(svc)
    get = lambda **f: {x["path"] for x in svc.search_all("ws1", "fsA", "ThinkTank", filters=f)["files"]}  # noqa
    assert get(include=["budget"]) == {"/Clients/Acme/ThinkTank brief.docx.md", "/Clients/Acme/kickoff.md",
                                       "/Clients/Acme/minutes.md"}
    assert get(include=["budget", "minutes"]) == {"/Clients/Acme/minutes.md"}
    assert "/Clients/Acme/minutes.md" not in get(exclude=["minutes"])
    assert get(inName=True) == {"/Clients/Acme/ThinkTank brief.docx.md"}
    assert get(modifiedBefore="2021-01-01T00:00:00Z") >= {"/Clients/Acme/minutes.md", "/Archive/2020/deck.pptx.md"}
    assert not get(modifiedBefore="2021-01-01T00:00:00Z") & {"/Clients/Acme/kickoff.md"}
    assert get(modifiedAfter="2024-01-01T00:00:00Z") == {"/Clients/Acme/ThinkTank brief.docx.md",
                                                         "/Clients/Acme/kickoff.md"}
    assert get(kind="documents") and all(p.endswith(".md") for p in get(kind="documents"))
    # .pptx.md is still "document" by extension; presentations come from real decks
    src(svc).write("/Decks/ThinkTank.pptx", b"PK not really a deck")
    run(svc)
    assert get(kind="presentations") <= {"/Decks/ThinkTank.pptx"}


def test_refinements_offered_only_when_they_narrow(make_service):
    svc = make_service()
    all_corpus(svc)
    body = svc.search_all("ws1", "fsA", "ThinkTank")
    total = body["total"]
    refs = body["refinements"]
    assert 0 < len(refs) <= 10
    for r in refs:
        assert set(r) == {"id", "kind", "label", "count", "apply"} and 0 < r["count"] < total
    by = {r["id"]: r for r in refs}
    kinds = {r["kind"] for r in refs}
    assert {"match", "folder", "date", "term"} <= kinds
    assert by["match:keyword"]["apply"] == {"mode": "keyword"} and by["match:keyword"]["label"] == "Only files that mention “ThinkTank”"
    assert by["match:name"]["apply"] == {"filters": {"inName": True}} and by["match:name"]["count"] == 1
    # Each refinement's count equals what applying it returns.
    for r in refs:
        if r["kind"] in ("match", "folder", "date"):
            a = r["apply"]
            got = svc.search_all("ws1", "fsA", "ThinkTank", mode=a.get("mode", "all"), filters=a.get("filters"))
            assert got["total"] == r["count"], r
    term = next(r for r in refs if r["kind"] == "term")
    assert term["label"].startswith("+ “") and term["apply"]["filters"]["include"]
    assert "thinktank" not in {t.lower() for t in term["apply"]["filters"]["include"]}
    # A set where every file mentions it: the keyword-only refinement would not narrow.
    only = svc.search_all("ws1", "fsA", "ThinkTank", mode="keyword")
    assert not any(r["id"] == "match:keyword" for r in only["refinements"])
    assert all(0 < r["count"] < only["total"] for r in only["refinements"])


def test_refinements_location_type_and_filters_merge(make_service):
    svc = make_service()
    src(svc, "fsA").write("/a.md", para("garden") + " ThinkTank ")
    src(svc, "fsA").write("/deck.pptx", b"PK")
    src(svc, "fsB").write("/b.md", para("garden") + " ThinkTank ")
    src(svc, "fsB").write("/c.md", para("garden") + " ThinkTank ")
    run(svc, "fsA")
    run(svc, "fsB")
    body = svc.search_all("ws1", None, "ThinkTank", mode="keyword", filters={"include": ["garden"]})
    locs = [r for r in body["refinements"] if r["kind"] == "location"]
    assert {r["apply"]["scope"]["filespace"] for r in locs} == {"fsA", "fsB"}
    assert {r["label"]: r["count"] for r in locs} == {"fsA": 1, "fsB": 2}
    # Patches carry the current filters along.
    for r in body["refinements"]:
        if "filters" in r["apply"]:
            assert "garden" in r["apply"]["filters"]["include"]


def test_type_refinement(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/ThinkTank plan.md", para("plan"))
    s.write("/ThinkTank.pdf", "%PDF-1.4 none")
    s.write("/ThinkTank.key", b"PK")
    s.write("/ThinkTank logo.png", b"\x89PNG")
    run(svc)
    find_names = [r["name"] for r in svc.find("ws1", "fsA", "thinktank")]
    assert len(find_names) == 4
    body = svc.search_all("ws1", "fsA", "ThinkTank plan")
    assert body["files"]
    refs = [r for r in body["refinements"] if r["kind"] == "type"]
    for r in refs:
        assert 0 < r["count"] < body["total"]


def test_backward_compatible_request_and_response(make_service):
    svc = make_service()
    all_corpus(svc)
    with client(svc) as c:
        res = c.post("/index/search", json={"query": "ThinkTank"}, headers=H)
        assert res.status_code == 200
        r0 = res.json()["results"][0]
        assert set(r0) == {"workspaceId", "filespace", "path", "chunkIndex", "score", "snippet", "mtime", "kind",
                           "location"}
        assert c.post("/index/search", json={"query": "x", "mode": "fuzzy"}, headers=H).status_code == 422
        assert c.post("/index/search", json={"query": "x", "filters": {"kind": "spreadsheets"}},
                      headers=H).status_code == 422
        ok = c.post("/index/search", json={"query": "ThinkTank", "mode": "keyword", "filters": {
            "modifiedAfter": "2024-01-01T00:00:00Z", "include": ["budget"]}}, headers=H)
        assert ok.status_code == 200
        for r in ok.json()["refinements"]:
            f = r["apply"].get("filters")
            if f:
                assert f["modifiedAfter"].startswith("2024-01-01") or r["kind"] == "date"


# ---------------------------------------------------------------- performance
def test_keyword_query_fast_on_50k_chunks(tmp_path):
    st = Store(str(tmp_path / "perf.db"))
    import random
    rnd = random.Random(7)
    vocab = [f"w{i}" for i in range(5000)]
    with st.tx() as c:
        c.executemany("INSERT INTO files (workspace_id, filespace, file_id, path, size, mtime, status) "
                      "VALUES ('ws', 'fs', ?, ?, 1, ?, 'indexed')",
                      [(str(i), f"/d{i % 40}/File {i}.md", i) for i in range(10000)])
        c.executemany("INSERT INTO chunks (workspace_id, filespace, file_id, chunk_index, text_hash, text) "
                      "VALUES ('ws', 'fs', ?, ?, ?, ?)",
                      [(str(j % 10000), j // 10000, f"h{j}",
                        " ".join(rnd.choices(vocab, k=60)) + (" ThinkTank" if j == 31337 else ""))
                       for j in range(50000)])
    p = QueryPlan("ThinkTank")
    st.keyword_search([("ws", "fs")], p.match, 200)
    t = time.perf_counter()
    hits = st.keyword_search([("ws", "fs")], p.match, 200)
    names = st.name_chunk_hits([("ws", "fs")], "file123", 50)
    found = st.find_files([("ws", "fs")], "file123", ["file123"], 400)
    dt = time.perf_counter() - t
    assert len(hits) == 1 and names and found
    print(f"50k chunks: keyword+name+find {dt * 1000:.1f} ms")
    assert dt < 0.2, dt
    st.close()
