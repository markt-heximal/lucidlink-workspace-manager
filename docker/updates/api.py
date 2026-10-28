"""HTTP surface of the update monitor.

  GET  /engine/updates        pinned vs latest per component, last check, recent alerts
  POST /engine/updates/check  run a check now (synchronous; a few seconds)

No auth is applied here, on purpose: the app decides, at include time. On this
deployment the file service is the public catch-all behind Tailscale Funnel, so
include it owner-only:

    app.include_router(updates_router, dependencies=[Depends(owner_auth.require_owner)])

Defence in depth regardless: a manual check is refused (429) within
UPDATES_MIN_MANUAL_INTERVAL_SEC (default 60) of the previous one, so the route
cannot be used to hammer PyPI/Docker Hub or burn LLM tokens.
"""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, HTTPException

from .monitor import CheckInProgress, get_monitor
from .scheduler import scheduler_info

router = APIRouter(prefix="/engine/updates", tags=["updates"])


def _min_manual_interval() -> float:
    try:
        return max(0.0, float(os.environ.get("UPDATES_MIN_MANUAL_INTERVAL_SEC", "60")))
    except ValueError:
        return 60.0


@router.get("")
def get_updates(alerts: int = 20) -> dict:
    monitor = get_monitor()
    return {**monitor.state(alerts_limit=max(0, min(alerts, 200))), "scheduler": scheduler_info()}


@router.post("/check")
def check_updates() -> dict:
    # Plain `def`: FastAPI runs it in the threadpool, so the blocking registry
    # calls never sit on the event loop.
    monitor = get_monitor()
    wait = _min_manual_interval() - (time.monotonic() - monitor.last_manual_check)
    if monitor.last_manual_check and wait > 0:
        raise HTTPException(status_code=429, detail=f"Checked recently; retry in {int(wait) + 1}s",
                            headers={"Retry-After": str(int(wait) + 1)})
    monitor.last_manual_check = time.monotonic()
    try:
        result = monitor.check(trigger="manual")
    except CheckInProgress as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"check": result, **monitor.state(), "scheduler": scheduler_info()}
