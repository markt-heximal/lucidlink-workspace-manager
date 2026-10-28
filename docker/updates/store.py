"""SQLite state for the update monitor (UPDATES_DB, default /data/updates.db).

/data is the named volume the file service already mounts, so the state
survives redeploys; that is what makes "alert once per release" hold across
restarts. Tables:

  components  latest seen per component, when it was first seen, last error
  releases    every (component, version) ever seen as latest, with its
              classification and the cached LLM summary (LLMs cost money)
  alerts      one row per (component, version, kind) that was delivered, so a
              release is never announced twice; `kind` is "release" or
              "security:<advisory id>"
  meta        last check start/finish/outcome

One connection per call, short transactions: the scheduler thread and the
POST /check handler can both reach here, and SQLite serializes them.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

DEFAULT_DB = "/data/updates.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS components (
    key TEXT PRIMARY KEY,
    latest_version TEXT,
    latest_first_seen_at TEXT,
    latest_released_at TEXT,
    pinned_version TEXT,
    last_checked_at TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS releases (
    component TEXT NOT NULL,
    version TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    released_at TEXT,
    update_type TEXT,
    breaking INTEGER,
    security INTEGER DEFAULT 0,
    notes_url TEXT,
    summary TEXT,
    PRIMARY KEY (component, version)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL,
    version TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'release',
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'default',
    channels TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (component, version, kind)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class UpdateStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.environ.get("UPDATES_DB", DEFAULT_DB)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # A shared in-memory DB would vanish between connections; tests use a tmp file.
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # -- components -------------------------------------------------------
    def get_component(self, key: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM components WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def record_check(self, key: str, *, pinned: Optional[str], latest: Optional[str],
                     released_at: Optional[str], error: Optional[str]) -> dict:
        """Upsert the component row; returns it. `latest_first_seen_at` moves only
        when the latest version changes."""
        ts = now_iso()
        prev = self.get_component(key)
        if latest is None:  # failed check: keep the last known latest
            latest = prev["latest_version"] if prev else None
            first_seen = prev["latest_first_seen_at"] if prev else None
            released_at = prev["latest_released_at"] if prev else None
        elif prev and prev["latest_version"] == latest:
            first_seen = prev["latest_first_seen_at"]
        else:
            first_seen = ts
        with self._conn() as c:
            c.execute(
                "INSERT INTO components (key, latest_version, latest_first_seen_at, latest_released_at,"
                " pinned_version, last_checked_at, last_error) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET latest_version=excluded.latest_version,"
                " latest_first_seen_at=excluded.latest_first_seen_at,"
                " latest_released_at=excluded.latest_released_at,"
                " pinned_version=excluded.pinned_version, last_checked_at=excluded.last_checked_at,"
                " last_error=excluded.last_error",
                (key, latest, first_seen, released_at, pinned, ts, error))
        return self.get_component(key) or {}

    # -- releases ---------------------------------------------------------
    def get_release(self, component: str, version: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM releases WHERE component = ? AND version = ?",
                            (component, version)).fetchone()
        return dict(row) if row else None

    def upsert_release(self, component: str, version: str, *, released_at: Optional[str],
                       update_type: str, breaking: bool, security: bool,
                       notes_url: Optional[str]) -> dict:
        with self._conn() as c:
            c.execute(
                "INSERT INTO releases (component, version, first_seen_at, released_at, update_type,"
                " breaking, security, notes_url) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(component, version) DO UPDATE SET released_at=excluded.released_at,"
                " update_type=excluded.update_type, breaking=excluded.breaking,"
                " security=excluded.security, notes_url=excluded.notes_url",
                (component, version, now_iso(), released_at, update_type, int(breaking),
                 int(security), notes_url))
        return self.get_release(component, version) or {}

    def set_summary(self, component: str, version: str, summary: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE releases SET summary = ? WHERE component = ? AND version = ?",
                      (summary, component, version))

    # -- alerts -----------------------------------------------------------
    def has_alert(self, component: str, version: str, kind: str = "release") -> bool:
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM alerts WHERE component = ? AND version = ? AND kind = ?",
                            (component, version, kind)).fetchone()
        return row is not None

    def add_alert(self, component: str, version: str, kind: str, *, title: str, body: str,
                  priority: str, channels: dict) -> bool:
        """Record a delivered alert. False if it was already recorded (lost a race)."""
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO alerts (component, version, kind, title, body, priority,"
                " channels, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (component, version, kind, title, body, priority, json.dumps(channels), now_iso()))
        return cur.rowcount == 1

    def recent_alerts(self, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["channels"] = json.loads(d["channels"])
            except ValueError:
                d["channels"] = {}
            out.append(d)
        return out

    # -- meta -------------------------------------------------------------
    def set_meta(self, key: str, value) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO meta (key, value) VALUES (?, ?)"
                      " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                      (key, json.dumps(value)))

    def get_meta(self, key: str, default=None):
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default
