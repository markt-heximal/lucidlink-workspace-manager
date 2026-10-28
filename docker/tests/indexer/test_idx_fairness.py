"""One huge filespace must not hold a job slot until its whole backlog is done."""
from conftest import files, para, run, src


def test_job_stops_at_budget_and_reschedules(make_service):
    svc = make_service(job_max_files=3)
    s = src(svc, "fsA")
    for i in range(10):
        s.write(f"/big{i}.md", para(f"bulk{i}"))

    run(svc, "fsA")
    rt = svc.scheduler.runtime("ws1", "fsA")
    assert sum(1 for f in files(svc, "fsA").values() if f["status"] == "indexed") == 3
    assert rt.more_pending is True            # dispatch will offer it a slot again

    run(svc, "fsA", full=False)
    run(svc, "fsA", full=False)
    run(svc, "fsA", full=False)
    assert all(f["status"] == "indexed" for f in files(svc, "fsA").values())
    assert rt.more_pending is False


def test_small_filespace_is_not_starved(make_service):
    svc = make_service(job_max_files=2)
    big, small = src(svc, "fsA"), src(svc, "fsB")
    for i in range(12):
        big.write(f"/big{i}.md", para(f"bulk{i}"))
    small.write("/note.md", para("urgent"))

    # Least recently run goes first: after fsA's slice, fsB is offered next.
    run(svc, "fsA")
    sched = svc.scheduler
    keys = sorted([("ws1", "fsA"), ("ws1", "fsB")],
                  key=lambda k: sched._runtime[k].last_run_end if k in sched._runtime else 0.0)
    assert keys[0] == ("ws1", "fsB")
    run(svc, "fsB")
    assert files(svc, "fsB")["/note.md"]["status"] == "indexed"
    assert any(f["status"] == "pending" for f in files(svc, "fsA").values())


def test_errors_do_not_spin(make_service):
    svc = make_service(job_max_files=1)
    s = src(svc, "fsA")
    for i in range(3):
        s.write(f"/f{i}.md", para(f"t{i}"))
    from indexer.embedder import EmbedError

    def down(texts):
        raise EmbedError("embedder down")
    svc.embedder._request = down
    run(svc, "fsA")
    assert svc.scheduler.runtime("ws1", "fsA").more_pending is False
