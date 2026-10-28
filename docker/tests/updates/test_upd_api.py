"""GET /engine/updates and POST /engine/updates/check, plus the scheduler."""
import threading
import time

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from updates import router, set_monitor, start_scheduler, stop_scheduler
from updates.monitor import UpdateMonitor


@pytest.fixture
def client(make_monitor):
    monitor = make_monitor()
    set_monitor(monitor)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        c.monitor = monitor
        yield c


def test_get_before_any_check(client):
    r = client.get("/engine/updates")
    assert r.status_code == 200
    body = r.json()
    assert body["last_check"] is None and body["alerts"] == []
    assert body["notifiers"] == [] and body["llm"] is False
    assert body["scheduler"] == {"running": False, "interval_sec": None}
    sdk = {c["key"]: c for c in body["components"]}["sdk"]
    assert sdk["pinned"] == "0.14.0" and sdk["latest"] is None and sdk["status"] == "unknown"


def test_post_check_then_get(client):
    r = client.post("/engine/updates/check")
    assert r.status_code == 200
    body = r.json()
    assert body["check"]["new_alerts"] == 2 and body["check"]["trigger"] == "manual"
    got = client.get("/engine/updates").json()
    assert got["last_check"]["ok"] is True and len(got["alerts"]) == 2
    assert {c["key"]: c["status"] for c in got["components"]} == {"sdk": "update_available",
                                                                   "img": "update_available"}
    assert client.get("/engine/updates?alerts=1").json()["alerts"].__len__() == 1


def test_manual_check_is_rate_limited(client):
    assert client.post("/engine/updates/check").status_code == 200
    r = client.post("/engine/updates/check")
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_rate_limit_is_configurable(client, monkeypatch):
    monkeypatch.setenv("UPDATES_MIN_MANUAL_INTERVAL_SEC", "0")
    assert client.post("/engine/updates/check").status_code == 200
    assert client.post("/engine/updates/check").status_code == 200


def test_concurrent_check_gets_409(client, monkeypatch):
    monkeypatch.setenv("UPDATES_MIN_MANUAL_INTERVAL_SEC", "0")
    client.monitor._lock.acquire()
    try:
        assert client.post("/engine/updates/check").status_code == 409
    finally:
        client.monitor._lock.release()


def test_router_can_be_mounted_owner_only(make_monitor):
    set_monitor(make_monitor())

    def require_owner(authorization: str = Header(default="")):
        if authorization != "Bearer owner":
            raise HTTPException(status_code=401)

    app = FastAPI()
    app.include_router(router, dependencies=[Depends(require_owner)])
    c = TestClient(app)
    assert c.get("/engine/updates").status_code == 401
    assert c.post("/engine/updates/check").status_code == 401
    assert c.get("/engine/updates", headers={"Authorization": "Bearer owner"}).status_code == 200


def test_scheduler_disabled_with_zero_interval():
    assert start_scheduler(0) is False


def test_scheduler_runs_checks_and_stops_promptly(make_monitor):
    ran = threading.Event()

    class Probe(UpdateMonitor):
        def check(self, *, trigger="schedule"):
            result = super().check(trigger=trigger)
            ran.set()
            return result

    probe = make_monitor()
    probe.__class__ = Probe
    set_monitor(probe)
    try:
        assert start_scheduler(3600, initial_delay_sec=0) is True
        assert start_scheduler(3600, initial_delay_sec=0) is True  # idempotent
        assert ran.wait(10)
        assert probe.store.get_meta("last_check")["trigger"] == "schedule"
    finally:
        t0 = time.monotonic()
        stop_scheduler()
        assert time.monotonic() - t0 < 5   # does not wait out the hour
