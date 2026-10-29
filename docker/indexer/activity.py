"""The engine console's feed: recent log lines kept in memory (docs: Engine activity feed).

A logging handler on the root logger keeps the indexer's own lines (`indexer.*`)
and warnings or errors from anything else in a ring; `GET /index/activity` reads
it after a sequence number. Request logs and HTTP client chatter are left out,
and anything that looks like a credential is masked before it is stored.
"""
from __future__ import annotations

import logging
import re
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Optional

LIMIT_MAX = 500
_SECRET = re.compile(r"(?i)(bearer\s+|sa_live:|(?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;\"'&]+")


def mask(text: str) -> str:
    return _SECRET.sub(lambda m: m.group(1) + "••••", text)


class ActivityLog(logging.Handler):
    def __init__(self, size: int = 1000):
        super().__init__(level=logging.INFO)
        self._lines: deque[dict] = deque(maxlen=max(10, size))
        self._seq = 0
        self._lock2 = threading.Lock()

    def keep(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.access":
            return False
        return record.name.startswith("indexer") or record.levelno >= logging.WARNING

    def emit(self, record: logging.LogRecord) -> None:
        if not self.keep(record):
            return
        try:
            text = record.getMessage()
            if record.exc_info and record.levelno >= logging.ERROR:
                text += f" ({record.exc_info[0].__name__}: {record.exc_info[1]})"
        except Exception:
            return
        level = "error" if record.levelno >= logging.ERROR else "warning" if record.levelno >= logging.WARNING else "info"
        source = record.name[len("indexer."):] if record.name.startswith("indexer.") else record.name
        ts = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self._lock2:
            self._seq += 1
            self._lines.append({"seq": self._seq, "ts": ts, "level": level, "source": source,
                                "text": mask(text)[:2000]})

    def read(self, after: Optional[int] = None, limit: int = 200) -> dict:
        limit = max(1, min(int(limit), LIMIT_MAX))
        with self._lock2:
            lines = list(self._lines)
            last = self._seq
        oldest = lines[0]["seq"] if lines else last + 1
        if after is None:
            picked = lines[-limit:]
            dropped = False
        else:
            newer = [e for e in lines if e["seq"] > after]
            picked = newer[:limit]
            dropped = after + 1 < oldest and bool(lines)
        return {"events": picked, "last": picked[-1]["seq"] if picked else (after if after is not None else last),
                "dropped": dropped}


_installed: Optional[ActivityLog] = None
_install_lock = threading.Lock()


def install(size: int = 1000) -> ActivityLog:
    """Attach the ring to the root logger once per process; later calls return it."""
    global _installed
    with _install_lock:
        if _installed is None:
            _installed = ActivityLog(size)
            logging.getLogger().addHandler(_installed)
        return _installed
