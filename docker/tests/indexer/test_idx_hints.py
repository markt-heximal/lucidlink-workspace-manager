"""Hint robustness: forged or bogus hints cost bounded work and memory."""
from __future__ import annotations

from conftest import files, para, run, src

from indexer.worker import MAX_QUEUED_HINTS


def test_hint_on_missing_path_does_not_full_scan(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/docs/a.md", para("alpha"))
    s.write("/docs/deep/b.md", para("beta"))
    run(svc)
    lists, stats = s.lists, s.stats

    for op in ("mkdir", "write", "delete"):
        assert svc.hint(None, "fsA", op, "/does-not-exist") is True
    svc.hint(None, "fsA", "move", "/nope/x.md", src="/nope/y.md")
    svc.scheduler.run_job("ws1", "fsA", full=None)   # periodic scan not yet due

    assert s.lists == lists                     # nothing listed at all
    assert s.stats - stats <= 6                 # a handful of get_entry probes
    assert set(files(svc)) == {"/docs/a.md", "/docs/deep/b.md"}


def test_hints_for_unknown_filespaces_are_dropped(make_service):
    svc = make_service()
    for i in range(1000):
        assert svc.hint(f"attacker-ws-{i}", "anything", "write", "/x") is False
    assert svc.hint("ws1", "not-configured", "write", "/x") is False
    assert svc.scheduler._runtime == {}
    assert svc.sync("ws1", "not-configured") == []
    assert svc.scheduler._runtime == {}
    svc.status()
    assert svc.scheduler._runtime == {}


def test_hint_flood_collapses_into_one_full_scan(make_service):
    svc = make_service()
    for i in range(MAX_QUEUED_HINTS * 4):
        svc.hint("ws1", "fsA", "write", f"/f{i}.md")
    rt = svc.scheduler.runtime("ws1", "fsA")
    assert len(rt.hints) <= MAX_QUEUED_HINTS
    assert rt.full_requested


def test_delete_hint_tombstones_only_confirmed_missing_paths(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/a.md", para("alpha"))
    s.write("/dir/b.md", para("beta"))
    s.write("/dir/c.md", para("gamma"))
    run(svc)
    lists = s.lists

    svc.hint("ws1", "fsA", "delete", "/a.md")      # still exists: forged, ignored
    run(svc, full=False)
    assert "/a.md" in files(svc)

    s.delete("/dir")
    svc.hint("ws1", "fsA", "delete", "/dir")
    run(svc, full=False)
    assert set(files(svc)) == {"/a.md"}
    assert s.lists == lists                        # settled without listing


def test_malformed_paths_are_ignored(make_service):
    svc = make_service()
    s = src(svc)
    s.write("/a.md", para("alpha"))
    run(svc)
    lists, stats = s.lists, s.stats
    for p in ("/../etc/passwd", "relative.md", "/a/./b", ""):
        svc.hint("ws1", "fsA", "write", p)
    svc.hint("ws1", "fsA", "delete", "/")
    run(svc, full=False)
    assert (s.lists, s.stats) == (lists, stats)
    assert set(files(svc)) == {"/a.md"}
