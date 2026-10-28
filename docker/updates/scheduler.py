"""Background schedule for update checks.

A daemon thread rather than an asyncio task: the check is blocking I/O
(httpx.Client, SQLite) and the rest of the engine is sync FastAPI routes, so a
thread keeps it off the event loop without an executor dance. `stop_scheduler`
wakes the thread through an Event, so shutdown never waits out the interval.

    start_scheduler()            # UPDATE_CHECK_INTERVAL_SEC, default 21600 (6h)
    stop_scheduler()

An interval <= 0 disables the schedule (POST /engine/updates/check still
works). The first check runs UPDATE_CHECK_INITIAL_DELAY_SEC (default 60) after
start, so a restart loop does not turn into a registry-polling loop and boot
is not slowed down.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

logger = logging.getLogger("lucidlink_api.updates")

DEFAULT_INTERVAL_SEC = 21600
DEFAULT_INITIAL_DELAY_SEC = 60.0

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_interval: Optional[float] = None


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        logger.warning("%s is not a number; using %s", name, default)
        return default


def _loop(interval: float, initial_delay: float) -> None:
    from .monitor import CheckInProgress, get_monitor

    delay = initial_delay
    while not _stop.wait(delay):
        try:
            get_monitor().check(trigger="schedule")
        except CheckInProgress:
            logger.info("scheduled update check skipped: one is already running")
        except Exception:  # keep the schedule alive whatever one run does
            logger.exception("scheduled update check failed")
        delay = interval


def start_scheduler(interval_sec: Optional[float] = None,
                    initial_delay_sec: Optional[float] = None) -> bool:
    """Start the periodic check. Idempotent. Returns True if a thread is running."""
    global _thread, _interval
    interval = interval_sec if interval_sec is not None else _env_float(
        "UPDATE_CHECK_INTERVAL_SEC", DEFAULT_INTERVAL_SEC)
    initial = initial_delay_sec if initial_delay_sec is not None else _env_float(
        "UPDATE_CHECK_INITIAL_DELAY_SEC", DEFAULT_INITIAL_DELAY_SEC)
    if interval <= 0:
        logger.info("update checks: schedule disabled (interval %s)", interval)
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop.clear()
    _interval = interval
    _thread = threading.Thread(target=_loop, args=(interval, max(0.0, initial)),
                               name="update-monitor", daemon=True)
    _thread.start()
    logger.info("update checks: every %ss (first in %ss)", int(interval), int(initial))
    return True


def stop_scheduler(timeout: float = 5.0) -> None:
    """Stop the periodic check; waits up to `timeout` for an in-flight check."""
    global _thread, _interval
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=timeout)
        if _thread.is_alive():
            logger.warning("update monitor thread still running after %ss (daemon; will not block exit)",
                           timeout)
    _thread = None
    _interval = None


def scheduler_info() -> dict:
    running = _thread is not None and _thread.is_alive()
    return {"running": running, "interval_sec": _interval if running else None}
