"""SQLite persistence for the indexer: `/data/index.db`, the source of truth.

Everything durable lives here — the per-file ledger that makes delta scans
possible, chunk text, the embedding cache and stored surveys — so a restart, a
lost Ruvector collection or a model change never forces a re-crawl. Ruvector is
only ever a mirror of what is in this file.

Concurrency: one connection in WAL mode shared behind an RLock. The indexer's
writes are small and frequent, reads are short, and at personal scale a single
serialized connection is simpler and safer than a pool. `generation` bumps on
every change that affects search results so the in-memory numpy index knows
when to reload.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional

import numpy as np

logger = logging.getLogger("indexer.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS filespaces (
    workspace_id TEXT NOT NULL,
    filespace    TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    -- 1 when `enabled` was set explicitly via PUT /index/filespaces.
    enabled_override INTEGER NOT NULL DEFAULT 0,
    last_scan_at TEXT,
    last_sync_at TEXT,
    last_error   TEXT,
    last_scan_id INTEGER NOT NULL DEFAULT 0,
    -- "lucidlink" (a filespace, read through the SDK) | "block" (a connected
    -- block storage folder, read in place).
    kind         TEXT NOT NULL DEFAULT 'lucidlink',
    PRIMARY KEY (workspace_id, filespace)
);
CREATE TABLE IF NOT EXISTS files (
    workspace_id TEXT NOT NULL,
    filespace    TEXT NOT NULL,
    file_id      TEXT NOT NULL,
    path         TEXT NOT NULL,
    size         INTEGER NOT NULL DEFAULT 0,   -- authoritative, from get_entry
    mtime        INTEGER NOT NULL DEFAULT 0,   -- authoritative, from get_entry
    -- (size, mtime) as read_dir last reported them for this path. read_dir has
    -- no file_id (SDK 0.14) and its mtime may use other units than get_entry's,
    -- so these only drive the per-path fast path. NULL = unknown (set by hints).
    list_size    INTEGER,
    list_mtime   INTEGER,
    content_hash TEXT,
    status       TEXT NOT NULL,   -- pending | indexed | skipped | failed | deleted
    reason       TEXT,
    chunk_count  INTEGER NOT NULL DEFAULT 0,
    scan_id      INTEGER NOT NULL DEFAULT 0,
    indexed_at   TEXT,
    -- Last user-visible change seen by the indexer (GET /index/recent):
    -- new | modified | renamed | deleted, or NULL when not derivable (e.g. the
    -- first crawl of a filespace, where every file would otherwise look "new").
    change_kind  TEXT,
    changed_at   TEXT,
    prev_path    TEXT,
    PRIMARY KEY (workspace_id, filespace, file_id)
);
CREATE INDEX IF NOT EXISTS files_path ON files (workspace_id, filespace, path);
CREATE INDEX IF NOT EXISTS files_status ON files (workspace_id, filespace, status, mtime);
-- GET /index/files: indexed newest-indexedAt first, and byReason (covering).
CREATE INDEX IF NOT EXISTS files_status_indexed ON files (workspace_id, filespace, status, indexed_at);
CREATE INDEX IF NOT EXISTS files_status_reason ON files (workspace_id, filespace, status, reason);
CREATE TABLE IF NOT EXISTS chunks (
    point_id     INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused: Ruvector ids
    workspace_id TEXT NOT NULL,
    filespace    TEXT NOT NULL,
    file_id      TEXT NOT NULL,
    chunk_index  INTEGER NOT NULL,
    text_hash    TEXT NOT NULL,
    text         TEXT NOT NULL,
    UNIQUE (workspace_id, filespace, file_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS chunks_hash ON chunks (text_hash);
CREATE TABLE IF NOT EXISTS embeddings (
    model     TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    vector    BLOB NOT NULL,      -- float32, L2-normalised
    PRIMARY KEY (model, text_hash)
);
CREATE TABLE IF NOT EXISTS surveys (
    workspace_id TEXT NOT NULL,
    filespace    TEXT NOT NULL,
    scope        TEXT NOT NULL,
    payload      TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    stale        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace_id, filespace, scope)
);
-- Point ids known to be in a Ruvector collection. Rows whose chunk is gone are
-- the "dead" points the stock server cannot delete.
-- Indexing controls (GET/PUT /index/control): one row, id 1. `priority` is a
-- JSON list of {"workspaceId", "filespace"}.
CREATE TABLE IF NOT EXISTS control (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    paused    INTEGER NOT NULL DEFAULT 0,
    paused_at TEXT,
    priority  TEXT NOT NULL DEFAULT '[]'
);
-- Block storage folders connected from the web app (folders.py), and names of
-- .env (BLOCK_SOURCES) sources the owner removed from the app.
CREATE TABLE IF NOT EXISTS block_folders (
    name       TEXT PRIMARY KEY,
    root       TEXT NOT NULL,
    folder     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Drive prompts: top-level folders of a root seen so far (baseline = there at
-- the first look, so never "new"), and block sources whose folder is missing.
CREATE TABLE IF NOT EXISTS drives_seen (
    root          TEXT NOT NULL,
    name          TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    baseline      INTEGER NOT NULL DEFAULT 0,
    dismissed     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (root, name)
);
CREATE TABLE IF NOT EXISTS source_missing (
    name      TEXT PRIMARY KEY,
    since     TEXT NOT NULL,
    dismissed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS block_removed (
    name       TEXT PRIMARY KEY,
    removed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mirror_points (
    collection TEXT NOT NULL,
    point_id   INTEGER NOT NULL,
    PRIMARY KEY (collection, point_id)
);
"""

LIVE_STATUSES = ("pending", "indexed", "skipped", "failed")

# GET /index/files order per status. `pending` matches pending_files (the queue).
FILE_ORDER = {
    "pending": "mtime DESC, path",
    "failed": "mtime DESC, path",
    "skipped": "mtime DESC, path",
    "indexed": "indexed_at DESC, path",
}
BY_REASON_MAX = 20


def like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def vec_to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def blob_to_vec(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._generation = 0

    def _migrate(self) -> None:
        fs_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(filespaces)").fetchall()}
        if "kind" not in fs_cols:
            self._conn.execute("ALTER TABLE filespaces ADD COLUMN kind TEXT NOT NULL DEFAULT 'lucidlink'")
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(files)").fetchall()}
        for col in ("list_size", "list_mtime"):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE files ADD COLUMN {col} INTEGER")
        for col in ("change_kind", "changed_at", "prev_path"):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE files ADD COLUMN {col} TEXT")
        fs_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(filespaces)").fetchall()}
        if "id_scheme" not in fs_cols:
            # NULL: ids were recorded by SDK 0.14 (or the filespace is new).
            self._conn.execute("ALTER TABLE filespaces ADD COLUMN id_scheme TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ primitives
    @property
    def generation(self) -> int:
        return self._generation

    def bump(self) -> None:
        """Signal that search-visible data (chunks, vectors, paths) changed."""
        with self._lock:
            self._generation += 1

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def query(self, sql: str, args: Iterable = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(args)).fetchall()]

    def one(self, sql: str, args: Iterable = ()) -> Optional[dict]:
        with self._lock:
            r = self._conn.execute(sql, tuple(args)).fetchone()
            return dict(r) if r else None

    def scalar(self, sql: str, args: Iterable = ()):
        with self._lock:
            r = self._conn.execute(sql, tuple(args)).fetchone()
            return r[0] if r else None

    def execute(self, sql: str, args: Iterable = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, tuple(args)).rowcount

    # ------------------------------------------------------------ block folders
    def block_folders(self) -> list[dict]:
        return self.query("SELECT name, root, folder, created_at FROM block_folders ORDER BY created_at, name")

    def add_block_folder(self, name: str, root: str, folder: str, now: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO block_folders (name, root, folder, created_at) VALUES (?, ?, ?, ?)",
                      (name, root, folder, now))
            c.execute("DELETE FROM block_removed WHERE name=?", (name,))

    def remove_block_folder(self, name: str) -> bool:
        return self.execute("DELETE FROM block_folders WHERE name=?", (name,)) > 0

    def block_removed(self) -> set[str]:
        return {r["name"] for r in self.query("SELECT name FROM block_removed")}

    def add_block_removed(self, name: str, now: str) -> None:
        self.execute("INSERT OR REPLACE INTO block_removed (name, removed_at) VALUES (?, ?)", (name, now))

    def drives_seen(self, root: str) -> dict[str, dict]:
        return {r["name"]: r for r in self.query("SELECT * FROM drives_seen WHERE root=?", (root,))}

    def add_drives_seen(self, root: str, names: list[str], now: str, baseline: bool) -> None:
        with self.tx() as c:
            c.executemany("INSERT OR IGNORE INTO drives_seen (root, name, first_seen_at, baseline) VALUES (?, ?, ?, ?)",
                          [(root, n, now, int(baseline)) for n in names])

    def dismiss_drive(self, root: str, name: str) -> bool:
        return self.execute("UPDATE drives_seen SET dismissed=1 WHERE root=? AND name=? AND baseline=0",
                            (root, name)) > 0

    def missing_row(self, name: str) -> Optional[dict]:
        return self.one("SELECT * FROM source_missing WHERE name=?", (name,))

    def mark_missing(self, name: str, now: str) -> dict:
        self.execute("INSERT OR IGNORE INTO source_missing (name, since) VALUES (?, ?)", (name, now))
        return self.missing_row(name)

    def clear_missing(self, name: str) -> None:
        self.execute("DELETE FROM source_missing WHERE name=?", (name,))

    def dismiss_missing(self, name: str) -> bool:
        return self.execute("UPDATE source_missing SET dismissed=1 WHERE name=?", (name,)) > 0

    def rekey_block_sources(self, names: set[str], target: str) -> list[str]:
        """Move block sources (kind "block") indexed under another workspace to `target`.

        Files, chunks and surveys keep their point ids and embeddings, so nothing is
        re-read; the Ruvector mirror forgets those points so they are re-sent with the
        new workspace in their payload.
        """
        moved: list[str] = []
        rows = self.query("SELECT workspace_id, filespace FROM filespaces WHERE kind='block' AND workspace_id != ?",
                          (target,))
        for r in rows:
            ws, fs = r["workspace_id"], r["filespace"]
            if fs not in names:
                continue
            with self.tx() as c:
                if c.execute("SELECT 1 FROM filespaces WHERE workspace_id=? AND filespace=?", (target, fs)).fetchone():
                    continue   # already there (a source of that name exists in the target): leave the old one
                for table in ("files", "chunks", "surveys"):
                    c.execute(f"UPDATE {table} SET workspace_id=? WHERE workspace_id=? AND filespace=?", (target, ws, fs))
                c.execute("UPDATE filespaces SET workspace_id=? WHERE workspace_id=? AND filespace=?", (target, ws, fs))
                c.execute("DELETE FROM mirror_points WHERE point_id IN "
                          "(SELECT point_id FROM chunks WHERE workspace_id=? AND filespace=?)", (target, fs))
                ctl = c.execute("SELECT priority FROM control WHERE id=1").fetchone()
                if ctl:
                    prio = json.loads(ctl[0] or "[]")
                    changed = False
                    for item in prio:
                        if item.get("workspaceId") == ws and item.get("filespace") == fs:
                            item["workspaceId"] = target
                            changed = True
                    if changed:
                        c.execute("UPDATE control SET priority=? WHERE id=1", (json.dumps(prio),))
            moved.append(fs)
        if moved:
            self.bump()
        return moved

    def purge_source(self, ws: str, fs: str) -> tuple[int, int]:
        """Forget everything indexed for one source: (files, chunks) deleted."""
        with self.tx() as c:
            files = c.execute("DELETE FROM files WHERE workspace_id=? AND filespace=?", (ws, fs)).rowcount
            chunks = c.execute("DELETE FROM chunks WHERE workspace_id=? AND filespace=?", (ws, fs)).rowcount
            c.execute("DELETE FROM surveys WHERE workspace_id=? AND filespace=?", (ws, fs))
            c.execute("DELETE FROM filespaces WHERE workspace_id=? AND filespace=?", (ws, fs))
        self.bump()
        return files, chunks

    # ------------------------------------------------------------ filespaces
    def ensure_filespace(self, ws: str, fs: str) -> None:
        self.execute("INSERT OR IGNORE INTO filespaces (workspace_id, filespace) VALUES (?, ?)", (ws, fs))

    def filespace_row(self, ws: str, fs: str) -> Optional[dict]:
        return self.one("SELECT * FROM filespaces WHERE workspace_id=? AND filespace=?", (ws, fs))

    def filespace_rows(self) -> list[dict]:
        return self.query("SELECT * FROM filespaces ORDER BY workspace_id, filespace")

    def set_filespace_enabled(self, ws: str, fs: str, enabled: bool) -> None:
        self.ensure_filespace(ws, fs)
        self.execute("UPDATE filespaces SET enabled=?, enabled_override=1 "
                     "WHERE workspace_id=? AND filespace=?", (int(enabled), ws, fs))

    def set_filespace_kind(self, ws: str, fs: str, kind: str) -> None:
        self.ensure_filespace(ws, fs)
        self.execute("UPDATE filespaces SET kind=? WHERE workspace_id=? AND filespace=? AND kind != ?",
                     (kind, ws, fs, kind))

    def kinds(self) -> dict[tuple[str, str], str]:
        return {(r["workspace_id"], r["filespace"]): r["kind"]
                for r in self.query("SELECT workspace_id, filespace, kind FROM filespaces")}

    def update_filespace(self, ws: str, fs: str, **cols) -> None:
        if not cols:
            return
        self.ensure_filespace(ws, fs)
        sets = ", ".join(f"{k}=?" for k in cols)
        self.execute(f"UPDATE filespaces SET {sets} WHERE workspace_id=? AND filespace=?",
                     (*cols.values(), ws, fs))

    def next_scan_id(self, ws: str, fs: str) -> int:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO filespaces (workspace_id, filespace) VALUES (?, ?)", (ws, fs))
            c.execute("UPDATE filespaces SET last_scan_id = last_scan_id + 1 "
                      "WHERE workspace_id=? AND filespace=?", (ws, fs))
            return c.execute("SELECT last_scan_id FROM filespaces WHERE workspace_id=? AND filespace=?",
                             (ws, fs)).fetchone()[0]

    def file_counts(self, ws: str, fs: str) -> dict:
        counts = {"indexed": 0, "pending": 0, "failed": 0, "skipped": 0}
        for r in self.query("SELECT status, COUNT(*) AS n FROM files WHERE workspace_id=? AND filespace=? "
                            "GROUP BY status", (ws, fs)):
            if r["status"] in counts:
                counts[r["status"]] = r["n"]
        return counts

    def chunk_count(self, ws: Optional[str] = None, fs: Optional[str] = None) -> int:
        if ws is None:
            return self.scalar("SELECT COUNT(*) FROM chunks") or 0
        return self.scalar("SELECT COUNT(*) FROM chunks WHERE workspace_id=? AND filespace=?", (ws, fs)) or 0

    # ------------------------------------------------------------ files
    def get_file(self, ws: str, fs: str, file_id: str) -> Optional[dict]:
        return self.one("SELECT * FROM files WHERE workspace_id=? AND filespace=? AND file_id=?",
                        (ws, fs, file_id))

    def live_files_at_path(self, ws: str, fs: str, path: str) -> list[dict]:
        return self.query("SELECT * FROM files WHERE workspace_id=? AND filespace=? AND path=? "
                          "AND status != 'deleted'", (ws, fs, path))

    def insert_file(self, ws: str, fs: str, file_id: str, path: str, size: int, mtime: int,
                    scan_id: int, status: str = "pending", reason: Optional[str] = None,
                    list_size: Optional[int] = None, list_mtime: Optional[int] = None) -> None:
        if not file_id:
            raise ValueError("refusing to write a ledger row without a file_id")
        # A file is "new" only once the filespace has been synced before; during
        # the first crawl everything is unseen, which says nothing to the user.
        synced_before = bool(self.scalar("SELECT last_sync_at FROM filespaces WHERE workspace_id=? "
                                         "AND filespace=?", (ws, fs)))
        kind, at = ("new", now_iso()) if synced_before else (None, None)
        self.execute(
            "INSERT OR REPLACE INTO files (workspace_id, filespace, file_id, path, size, mtime, "
            "list_size, list_mtime, content_hash, status, reason, chunk_count, scan_id, indexed_at, "
            "change_kind, changed_at, prev_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, ?, NULL, ?, ?, NULL)",
            (ws, fs, file_id, path, size, mtime, list_size, list_mtime, status, reason, scan_id, kind, at))

    @staticmethod
    def change_of(old: dict, cols: dict) -> tuple[Optional[str], Optional[str]]:
        """(change_kind, prev_path) implied by applying `cols` to ledger row `old`, or (None, None)."""
        new_status = cols.get("status", old["status"])
        if new_status == "deleted":
            return ("deleted", None) if old["status"] != "deleted" else (None, None)
        if old["status"] == "deleted":
            return "new", None                       # reappeared after a deletion
        moved = "path" in cols and cols["path"] != old["path"]
        modified = "mtime" in cols and cols["mtime"] != old["mtime"]
        if modified:
            return "modified", old["path"] if moved else None
        if moved:
            return "renamed", old["path"]
        return None, None

    def update_file(self, ws: str, fs: str, file_id: str, **cols) -> None:
        if {"status", "path", "mtime"} & cols.keys():
            old = self.get_file(ws, fs, file_id)
            if old is not None:
                kind, prev = self.change_of(old, cols)
                if kind:
                    cols = {**cols, "change_kind": kind, "changed_at": now_iso(), "prev_path": prev}
        sets = ", ".join(f"{k}=?" for k in cols)
        self.execute(f"UPDATE files SET {sets} WHERE workspace_id=? AND filespace=? AND file_id=?",
                     (*cols.values(), ws, fs, file_id))

    def pending_files(self, ws: str, fs: str, limit: Optional[int] = None) -> list[dict]:
        """The work queue: newest mtime first, so a fresh filespace is useful early."""
        sql = ("SELECT * FROM files WHERE workspace_id=? AND filespace=? AND status='pending' "
               "ORDER BY mtime DESC, path")
        if limit is not None:
            return self.query(sql + " LIMIT ?", (ws, fs, int(limit)))
        return self.query(sql, (ws, fs))

    def recent_files(self, scopes: Iterable[tuple[str, str]], limit: int) -> list[dict]:
        """Candidate rows for GET /index/recent: per scope, the newest live files by
        mtime and the latest deletions/renames by changed_at. The caller merges
        and orders them (mtime units vary, see service.mtime_seconds)."""
        out: list[dict] = []
        for ws, fs in scopes:
            out += self.query("SELECT * FROM files WHERE workspace_id=? AND filespace=? AND status != 'deleted' "
                              "ORDER BY mtime DESC, path LIMIT ?", (ws, fs, limit))
            out += self.query("SELECT * FROM files WHERE workspace_id=? AND filespace=? AND changed_at IS NOT NULL "
                              "AND (status = 'deleted' OR change_kind = 'renamed') "
                              "ORDER BY changed_at DESC LIMIT ?", (ws, fs, limit))
        return out

    def live_file_stats(self, ws: str, fs: str) -> list[dict]:
        """(path, size, mtime) of every live file, for GET /index/profile."""
        return self.query("SELECT path, size, mtime FROM files WHERE workspace_id=? AND filespace=? "
                          "AND status != 'deleted'", (ws, fs))

    def skipped_reasons(self, ws: str, fs: str) -> list[dict]:
        return self.query("SELECT reason, COUNT(*) AS files FROM files WHERE workspace_id=? AND filespace=? "
                          "AND status='skipped' GROUP BY reason ORDER BY files DESC, reason", (ws, fs))

    def list_files(self, ws: str, fs: str, status: str, limit: int, offset: int = 0,
                   q: Optional[str] = None) -> tuple[list[dict], int]:
        """One page of a status for GET /index/files, and the (filtered) total.
        `q` is a case-insensitive substring of the path (LIKE: ASCII folding)."""
        where = "workspace_id=? AND filespace=? AND status=?"
        args: list = [ws, fs, status]
        if q:
            where += " AND path LIKE ? ESCAPE '\\'"
            args.append(f"%{like_escape(q)}%")
        total = self.scalar(f"SELECT COUNT(*) FROM files WHERE {where}", args) or 0
        rows = self.query(f"SELECT path, size, mtime, status, reason, indexed_at, changed_at FROM files "
                          f"WHERE {where} ORDER BY {FILE_ORDER[status]} LIMIT ? OFFSET ?",
                          (*args, int(limit), int(offset)))
        return rows, total

    def reasons(self, ws: str, fs: str, status: str, limit: int = BY_REASON_MAX) -> list[dict]:
        """Files per reason over the whole status, largest first."""
        return self.query("SELECT reason, COUNT(*) AS files FROM files WHERE workspace_id=? AND filespace=? "
                          "AND status=? GROUP BY reason ORDER BY files DESC, reason LIMIT ?",
                          (ws, fs, status, int(limit)))

    def requeue_failed(self, ws: str, fs: str, paths: Optional[list[str]] = None) -> int:
        """failed -> pending for the given paths (None = every failed file).

        Clears the retry back-off (`indexed_at` holds the failure time) and the
        failure `reason`/`content_hash`, so the file is read and extracted afresh
        rather than being taken for a touch of its last good content."""
        sql = ("UPDATE files SET status='pending', reason=NULL, content_hash=NULL, indexed_at=NULL "
               "WHERE workspace_id=? AND filespace=? AND status='failed'")
        if paths is None:
            return self.execute(sql, (ws, fs))
        n = 0
        uniq = list(dict.fromkeys(paths))
        with self.tx() as c:
            for i in range(0, len(uniq), 500):
                part = uniq[i:i + 500]
                n += c.execute(sql + f" AND path IN ({','.join('?' * len(part))})", (ws, fs, *part)).rowcount
        return n

    # ------------------------------------------------------------ control
    def control(self) -> dict:
        """{"paused": bool, "paused_at": ISO|None, "priority": [{"workspaceId", "filespace"}]}"""
        r = self.one("SELECT paused, paused_at, priority FROM control WHERE id=1")
        if r is None:
            return {"paused": False, "paused_at": None, "priority": []}
        try:
            prio = json.loads(r["priority"] or "[]")
        except ValueError:
            prio = []
        return {"paused": bool(r["paused"]), "paused_at": r["paused_at"],
                "priority": [p for p in prio if isinstance(p, dict)]}

    def set_control(self, paused: bool, paused_at: Optional[str], priority: list[dict]) -> None:
        self.execute("INSERT OR REPLACE INTO control (id, paused, paused_at, priority) VALUES (1, ?, ?, ?)",
                     (int(paused), paused_at, json.dumps(priority)))

    # ------------------------------------------------------------ chunks
    def file_chunks(self, ws: str, fs: str, file_id: str) -> list[dict]:
        return self.query("SELECT point_id, chunk_index, text_hash FROM chunks WHERE workspace_id=? "
                          "AND filespace=? AND file_id=? ORDER BY chunk_index", (ws, fs, file_id))

    def replace_chunks(self, ws: str, fs: str, file_id: str,
                       chunks: list[tuple[str, str]]) -> int:
        """Set a file's chunks to `[(text_hash, text), ...]`, reusing unchanged rows.

        A chunk whose hash is unchanged at the same index keeps its point id (so
        Ruvector needs nothing); a changed one gets a fresh id, leaving the old
        one dead in the mirror. Returns the number of rows written.
        """
        written = 0
        with self.tx() as c:
            existing = {r["chunk_index"]: r for r in c.execute(
                "SELECT point_id, chunk_index, text_hash FROM chunks WHERE workspace_id=? AND filespace=? "
                "AND file_id=?", (ws, fs, file_id)).fetchall()}
            for idx, (text_hash, text) in enumerate(chunks):
                old = existing.pop(idx, None)
                if old is not None and old["text_hash"] == text_hash:
                    continue
                if old is not None:
                    c.execute("DELETE FROM chunks WHERE point_id=?", (old["point_id"],))
                c.execute("INSERT INTO chunks (workspace_id, filespace, file_id, chunk_index, text_hash, text) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (ws, fs, file_id, idx, text_hash, text))
                written += 1
            for old in existing.values():
                c.execute("DELETE FROM chunks WHERE point_id=?", (old["point_id"],))
                written += 1
        if written:
            self.bump()
        return written

    def delete_file_chunks(self, ws: str, fs: str, file_id: str) -> int:
        n = self.execute("DELETE FROM chunks WHERE workspace_id=? AND filespace=? AND file_id=?",
                         (ws, fs, file_id))
        if n:
            self.bump()
        return n

    def chunk_rows(self, point_ids: list[int]) -> dict[int, dict]:
        if not point_ids:
            return {}
        out: dict[int, dict] = {}
        for i in range(0, len(point_ids), 500):
            part = point_ids[i:i + 500]
            marks = ",".join("?" * len(part))
            for r in self.query(
                    f"SELECT c.point_id, c.workspace_id, c.filespace, c.file_id, c.chunk_index, c.text, "
                    f"f.path, f.mtime FROM chunks c JOIN files f ON f.workspace_id=c.workspace_id "
                    f"AND f.filespace=c.filespace AND f.file_id=c.file_id "
                    f"WHERE c.point_id IN ({marks}) AND f.status != 'deleted'", part):
                out[r["point_id"]] = r
        return out

    # ------------------------------------------------------------ embeddings
    def get_embeddings(self, model: str, hashes: list[str]) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        uniq = list(dict.fromkeys(hashes))
        for i in range(0, len(uniq), 500):
            part = uniq[i:i + 500]
            marks = ",".join("?" * len(part))
            for r in self.query(f"SELECT text_hash, vector FROM embeddings WHERE model=? "
                                f"AND text_hash IN ({marks})", (model, *part)):
                out[r["text_hash"]] = blob_to_vec(r["vector"])
        return out

    def put_embeddings(self, model: str, items: list[tuple[str, np.ndarray]]) -> None:
        if not items:
            return
        with self.tx() as c:
            c.executemany("INSERT OR REPLACE INTO embeddings (model, text_hash, dim, vector) VALUES (?, ?, ?, ?)",
                          [(model, h, int(len(v)), vec_to_blob(v)) for h, v in items])
        self.bump()

    def model_dim(self, model: str) -> Optional[int]:
        return self.scalar("SELECT dim FROM embeddings WHERE model=? LIMIT 1", (model,))

    def chunks_missing_embeddings(self, model: str, limit: int = 256) -> list[dict]:
        """Chunks with no cached vector for `model` (e.g. after a model change)."""
        return self.query(
            "SELECT DISTINCT c.text_hash, c.text FROM chunks c WHERE NOT EXISTS "
            "(SELECT 1 FROM embeddings e WHERE e.model=? AND e.text_hash=c.text_hash) LIMIT ?",
            (model, limit))

    def live_vectors(self, model: str) -> list[dict]:
        """Every searchable chunk: its scope, path and vector for `model`."""
        return self.query(
            "SELECT c.point_id, c.workspace_id, c.filespace, f.path, e.vector FROM chunks c "
            "JOIN embeddings e ON e.model=? AND e.text_hash=c.text_hash "
            "JOIN files f ON f.workspace_id=c.workspace_id AND f.filespace=c.filespace "
            "AND f.file_id=c.file_id WHERE f.status != 'deleted'", (model,))

    # ------------------------------------------------------------ surveys
    def get_survey(self, ws: str, fs: str, scope: str) -> Optional[dict]:
        return self.one("SELECT * FROM surveys WHERE workspace_id=? AND filespace=? AND scope=?", (ws, fs, scope))

    def put_survey(self, ws: str, fs: str, scope: str, payload: str, generated_at: str) -> None:
        self.execute("INSERT OR REPLACE INTO surveys (workspace_id, filespace, scope, payload, generated_at, stale) "
                     "VALUES (?, ?, ?, ?, ?, 0)", (ws, fs, scope, payload, generated_at))

    def mark_surveys_stale(self, ws: str, fs: str) -> None:
        self.execute("UPDATE surveys SET stale=1 WHERE workspace_id=? AND filespace=?", (ws, fs))

    # ------------------------------------------------------------ ruvector mirror
    def mirror_missing(self, collection: str, model: str, limit: int = 256) -> list[dict]:
        return self.query(
            "SELECT c.point_id, c.workspace_id, c.filespace, c.file_id, c.chunk_index, f.path, e.vector "
            "FROM chunks c JOIN embeddings e ON e.model=? AND e.text_hash=c.text_hash "
            "JOIN files f ON f.workspace_id=c.workspace_id AND f.filespace=c.filespace AND f.file_id=c.file_id "
            "WHERE f.status != 'deleted' AND NOT EXISTS "
            "(SELECT 1 FROM mirror_points m WHERE m.collection=? AND m.point_id=c.point_id) "
            "ORDER BY c.point_id LIMIT ?", (model, collection, limit))

    def mirror_add(self, collection: str, point_ids: list[int]) -> None:
        with self.tx() as c:
            c.executemany("INSERT OR IGNORE INTO mirror_points (collection, point_id) VALUES (?, ?)",
                          [(collection, p) for p in point_ids])

    def mirror_sample(self, collection: str) -> Optional[int]:
        """One live point we believe is mirrored, to check the mirror still holds it."""
        return self.scalar("SELECT m.point_id FROM mirror_points m JOIN chunks c ON c.point_id=m.point_id "
                           "WHERE m.collection=? ORDER BY m.point_id DESC LIMIT 1", (collection,))

    def mirror_clear(self, collection: str) -> None:
        self.execute("DELETE FROM mirror_points WHERE collection=?", (collection,))

    def rekey_file(self, ws: str, fs: str, old_id: str, new_id: str) -> None:
        """Give a file (and its chunks) a new id, keeping its embeddings."""
        with self.tx() as c:
            c.execute("UPDATE files SET file_id=? WHERE workspace_id=? AND filespace=? AND file_id=?",
                      (new_id, ws, fs, old_id))
            c.execute("UPDATE chunks SET file_id=? WHERE workspace_id=? AND filespace=? AND file_id=?",
                      (new_id, ws, fs, old_id))

    def mirror_forget_file(self, ws: str, fs: str, file_id: str) -> None:
        """Make a file's live points look unmirrored so metadata is re-upserted."""
        self.execute("DELETE FROM mirror_points WHERE point_id IN (SELECT point_id FROM chunks "
                     "WHERE workspace_id=? AND filespace=? AND file_id=?)", (ws, fs, file_id))

    def mirror_counts(self, collection: str) -> tuple[int, int]:
        """(live mirrored points, dead mirrored points)."""
        total = self.scalar("SELECT COUNT(*) FROM mirror_points WHERE collection=?", (collection,)) or 0
        dead = self.scalar("SELECT COUNT(*) FROM mirror_points m WHERE m.collection=? AND NOT EXISTS "
                           "(SELECT 1 FROM chunks c WHERE c.point_id=m.point_id)", (collection,)) or 0
        return total - dead, dead

    def live_point_ids(self, point_ids: list[int]) -> set[int]:
        return set(self.chunk_rows(point_ids).keys())
