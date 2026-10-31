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
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional
from urllib.parse import quote

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
    created_at TEXT NOT NULL,
    download_online_only INTEGER NOT NULL DEFAULT 0
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
-- Small key/value state. fts_lo/fts_hi: chunk point ids in (lo, hi] are not yet
-- in chunks_fts (the one-time backfill of a database that predates it).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Related files: one meaning vector per indexed file (the normalised mean of its
-- chunk vectors), built in the background from the embeddings cache. `seq` grows
-- with every write, so the in-memory file index loads only what changed. A row
-- whose file's chunks changed is dropped to vector NULL (a new seq) and rebuilt.
CREATE TABLE IF NOT EXISTS file_vectors (
    workspace_id TEXT NOT NULL,
    filespace    TEXT NOT NULL,
    file_id      TEXT NOT NULL,
    model        TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    chunks       INTEGER NOT NULL DEFAULT 0,
    vector       BLOB,               -- float32, L2-normalised; NULL = dropped
    PRIMARY KEY (workspace_id, filespace, file_id, model)
);
CREATE INDEX IF NOT EXISTS file_vectors_seq ON file_vectors (model, seq);
-- Discovered collections: the locations the owner switched on, and the groups
-- the engine proposed there. A group's `keys` (its files, JSON) let the next run
-- recognise it, so a dismissed group stays dismissed and a name is kept.
CREATE TABLE IF NOT EXISTS discover_scopes (
    workspace_id TEXT NOT NULL,
    filespace    TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    requested_at TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    fv_seq       INTEGER NOT NULL DEFAULT 0,   -- file vectors as of the last run
    error        TEXT,
    stats        TEXT,                         -- JSON
    PRIMARY KEY (workspace_id, filespace)
);
CREATE TABLE IF NOT EXISTS discover_groups (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    filespace    TEXT NOT NULL,
    status       TEXT NOT NULL,     -- proposed | kept | dismissed | merged
    name         TEXT NOT NULL,
    named_by     TEXT NOT NULL,     -- auto | ai | you
    summary      TEXT,
    score        REAL NOT NULL DEFAULT 0,
    size         INTEGER NOT NULL,
    keys         TEXT NOT NULL,     -- JSON [file_id]
    data         TEXT NOT NULL,     -- JSON: files, names, folders, dates
    merged_into  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS discover_groups_scope ON discover_groups (workspace_id, filespace, status, score);
"""

# Keyword index over chunk text (docs: Search quality). External content, so the
# text is stored once (in chunks); triggers keep it in sync with every chunk
# insert/delete/text update. A row is in the index unless its point id is in the
# backfill hole (fts_lo, fts_hi], which only exists while an old database is
# being backfilled; ids are AUTOINCREMENT, so rows written after the table was
# created are always above the hole.
FTS_HOLE = ("NOT ({id} > COALESCE((SELECT CAST(value AS INTEGER) FROM meta WHERE key='fts_lo'), 0) "
            "AND {id} <= COALESCE((SELECT CAST(value AS INTEGER) FROM meta WHERE key='fts_hi'), 0))")
FTS_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='point_id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts_vocab USING fts5vocab(chunks_fts, row);
CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.point_id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks WHEN {FTS_HOLE.format(id='old.point_id')} BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.point_id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE OF text ON chunks WHEN {FTS_HOLE.format(id='old.point_id')} BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.point_id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.point_id, new.text);
END;
"""
FTS_BACKFILL_BATCH = 2000

# A file name or path folded for name matching: case, spaces, hyphens,
# underscores and dots ignored, so "ThinkTank", "Think Tank" and "think_tank"
# all become "thinktank". SQLite's lower() folds ASCII only; fold_name() matches.
def _norm_sql(expr: str) -> str:
    return (f"replace(replace(replace(replace(replace(lower({expr}), ' ', ''), '-', ''), '_', ''), '.', ''), "
            f"char(9), '')")


# The last path segment: strip everything up to the last '/'.
NAME_SQL = "replace(path, rtrim(path, replace(path, '/', '')), '')"
NAME_NORM_SQL = _norm_sql(NAME_SQL)
PATH_NORM_SQL = _norm_sql("path")


FOLD_TRIGGERS = f"""
CREATE TRIGGER IF NOT EXISTS files_fold_ai AFTER INSERT ON files BEGIN
    UPDATE files SET name_fold={NAME_NORM_SQL.replace('path', 'new.path')},
                     path_fold={PATH_NORM_SQL.replace('path', 'new.path')} WHERE rowid=new.rowid;
END;
CREATE TRIGGER IF NOT EXISTS files_fold_au AFTER UPDATE OF path ON files BEGIN
    UPDATE files SET name_fold={NAME_NORM_SQL.replace('path', 'new.path')},
                     path_fold={PATH_NORM_SQL.replace('path', 'new.path')} WHERE rowid=new.rowid;
END;
"""


def fold_name(text: str) -> str:
    """Python twin of the SQL name folding (ASCII-only lowercase, like SQLite)."""
    out = []
    for ch in text:
        if ch in " -_.\t":
            continue
        out.append(ch.lower() if ch.isascii() else ch)
    return "".join(out)

LIVE_STATUSES = ("pending", "indexed", "skipped", "failed")

# GET /index/files order per status. `pending` matches pending_files (the queue).
FILE_ORDER = {
    "pending": "mtime DESC, path",
    "failed": "mtime DESC, path",
    "skipped": "mtime DESC, path",
    "indexed": "indexed_at DESC, path",
}
BY_REASON_MAX = 20


def _split_sql(script: str) -> list[str]:
    """Split FTS_SCHEMA into statements (triggers contain ';' inside BEGIN…END)."""
    out, buf = [], ""
    for line in script.strip().splitlines():
        buf += line + "\n"
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    return out


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
        self.path = path
        self._lock = threading.RLock()
        self._local = threading.local()    # read_view: a thread's private read connection
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._fts_done = False
        self.fts_available = self._init_fts()
        self._generation = 0

    def _migrate(self) -> None:
        bf_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(block_folders)").fetchall()}
        if "download_online_only" not in bf_cols:
            self._conn.execute("ALTER TABLE block_folders ADD COLUMN download_online_only INTEGER NOT NULL DEFAULT 0")
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
        if "name_fold" not in cols:
            # Folded name/path for find (fold_name). Stored, and kept by triggers on
            # every path write, because folding on the fly costs ~3x the scan.
            # One-time fill: ~1 s per 100k files.
            self._conn.execute("ALTER TABLE files ADD COLUMN name_fold TEXT")
            self._conn.execute("ALTER TABLE files ADD COLUMN path_fold TEXT")
            self._conn.execute(f"UPDATE files SET name_fold={NAME_NORM_SQL}, path_fold={PATH_NORM_SQL}")
        for stmt in _split_sql(FOLD_TRIGGERS):
            self._conn.execute(stmt)
        # Related files: "this file names report.pdf" looks names up exactly.
        self._conn.execute("CREATE INDEX IF NOT EXISTS files_name_fold ON files (name_fold)")

    def _init_fts(self) -> bool:
        """Create the keyword index if this SQLite has FTS5; False disables keyword search."""
        try:
            self._conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.fts5_probe USING fts5(x)")
            self._conn.execute("DROP TABLE IF EXISTS temp.fts5_probe")
        except sqlite3.OperationalError as e:
            logger.warning("SQLite has no FTS5 (%s): keyword search disabled, vector search only", e)
            return False
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                fresh = self._conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='chunks_fts'").fetchone() is None
                if fresh:
                    # Rows already in chunks (an older database) form the hole the
                    # background backfill fills; an empty database has none.
                    hi = self._conn.execute("SELECT COALESCE(MAX(point_id), 0) FROM chunks").fetchone()[0]
                    self._conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                                           (("fts_lo", "0"), ("fts_hi", str(hi))))
                for stmt in _split_sql(FTS_SCHEMA):
                    self._conn.execute(stmt)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return True

    def fts_hole(self) -> tuple[int, int]:
        r = {k: int(v) for k, v in self._conn.execute(
            "SELECT key, value FROM meta WHERE key IN ('fts_lo', 'fts_hi')").fetchall()}
        return r.get("fts_lo", 0), r.get("fts_hi", 0)

    @property
    def fts_ready(self) -> bool:
        """Keyword search usable: FTS5 present and every chunk is in the index."""
        if not self.fts_available:
            return False
        if self._fts_done:                 # once built it stays built (fts_rebuild resets this)
            return True
        with self._lock:
            lo, hi = self.fts_hole()
        self._fts_done = lo >= hi
        return self._fts_done

    def fts_backfill_step(self, batch: int = FTS_BACKFILL_BATCH) -> bool:
        """Index the next `batch` pre-existing chunks; True once none are left.

        Short transactions, so the indexer and API keep working while an old,
        large ledger is backfilled (one `'rebuild'` would hold the write lock,
        and our shared connection, for minutes).
        """
        if not self.fts_available:
            return True
        with self.tx() as c:
            lo, hi = self.fts_hole()
            if lo >= hi:
                return True
            top = c.execute("SELECT MAX(point_id) FROM (SELECT point_id FROM chunks WHERE point_id > ? "
                            "AND point_id <= ? ORDER BY point_id LIMIT ?)", (lo, hi, batch)).fetchone()[0]
            new_lo = hi if top is None else top
            c.execute("INSERT INTO chunks_fts(rowid, text) SELECT point_id, text FROM chunks "
                      "WHERE point_id > ? AND point_id <= ?", (lo, new_lo))
            if new_lo >= hi:
                new_lo = hi = 0
                c.execute("UPDATE meta SET value='0' WHERE key='fts_hi'")
            c.execute("UPDATE meta SET value=? WHERE key='fts_lo'", (str(new_lo),))
        if new_lo >= hi:
            self.bump()
            return True
        return False

    def fts_backfill(self, batch: int = FTS_BACKFILL_BATCH, pause: float = 0.0,
                     stop: Optional[threading.Event] = None) -> bool:
        """Run fts_backfill_step until done (or `stop` is set); True when done."""
        while not (stop is not None and stop.is_set()):
            if self.fts_backfill_step(batch):
                return True
            if pause:
                (stop.wait(pause) if stop is not None else threading.Event().wait(pause))
        return False

    def fts_rebuild(self) -> None:
        """Drop and re-create the keyword index from chunks (repair tool; tests).

        Leaves a hole over every existing chunk, so the next backfill fills it.
        """
        if not self.fts_available:
            return
        with self.tx() as c:
            hi = c.execute("SELECT COALESCE(MAX(point_id), 0) FROM chunks").fetchone()[0]
            self._fts_done = False
            c.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('delete-all')")
            c.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (("fts_lo", "0"), ("fts_hi", str(hi))))
        self.bump()

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

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        """A read-only connection for long scans, so they never hold the shared
        lock (WAL readers do not block the writer). An in-memory database has
        no second connection: its scans run on the shared one, under the lock."""
        if self.path == ":memory:":
            with self._lock:
                yield self._conn
            return
        conn = sqlite3.connect(f"file:{quote(os.path.abspath(self.path))}?mode=ro", uri=True,
                               check_same_thread=False)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def read_view(self) -> Iterator[None]:
        """Run this thread's query/one/scalar on a private read-only connection
        until the block ends: long read-only work (Related files) then neither
        waits behind indexing for the shared lock nor holds it up."""
        if self.path == ":memory:" or getattr(self._local, "conn", None) is not None:
            yield
            return
        with self.reader() as conn:
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
            try:
                yield
            finally:
                self._local.conn = None

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Connection]:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            yield conn
        else:
            with self._lock:
                yield self._conn

    def query(self, sql: str, args: Iterable = ()) -> list[dict]:
        with self._cursor() as c:
            return [dict(r) for r in c.execute(sql, tuple(args)).fetchall()]

    def one(self, sql: str, args: Iterable = ()) -> Optional[dict]:
        with self._cursor() as c:
            r = c.execute(sql, tuple(args)).fetchone()
            return dict(r) if r else None

    def scalar(self, sql: str, args: Iterable = ()):
        with self._cursor() as c:
            r = c.execute(sql, tuple(args)).fetchone()
            return r[0] if r else None

    def execute(self, sql: str, args: Iterable = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, tuple(args)).rowcount

    # ------------------------------------------------------------ block folders
    def block_folders(self) -> list[dict]:
        return self.query("SELECT name, root, folder, created_at, download_online_only FROM block_folders "
                          "ORDER BY created_at, name")

    def set_block_download(self, name: str, on: bool) -> bool:
        return self.execute("UPDATE block_folders SET download_online_only=? WHERE name=?", (int(on), name)) > 0

    def requeue_reason(self, ws: str, fs: str, reason: str) -> int:
        """Files skipped for `reason` go back to pending (their content is read again)."""
        n = self.execute("UPDATE files SET status='pending', reason=NULL, content_hash=NULL "
                         "WHERE workspace_id=? AND filespace=? AND status='skipped' AND reason=?", (ws, fs, reason))
        return n

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
        new workspace in their payload. When `target` already has a copy, the one
        with more files (then the more recently scanned) is kept and the other purged.
        """
        moved: list[str] = []
        rows = self.query("SELECT workspace_id, filespace FROM filespaces WHERE kind='block' AND workspace_id != ?",
                          (target,))
        for r in rows:
            ws, fs = r["workspace_id"], r["filespace"]
            if fs not in names:
                continue
            # Both copies exist (an older engine filed block sources under "local" as a fallback):
            # keep the one with more files, else the more recently scanned, and forget the other.
            there = self.one("SELECT last_scan_at FROM filespaces WHERE workspace_id=? AND filespace=?", (target, fs))
            if there is not None:
                n_here = self.scalar("SELECT count(*) FROM files WHERE workspace_id=? AND filespace=?", (ws, fs)) or 0
                n_there = self.scalar("SELECT count(*) FROM files WHERE workspace_id=? AND filespace=?", (target, fs)) or 0
                here_scan = self.scalar("SELECT last_scan_at FROM filespaces WHERE workspace_id=? AND filespace=?",
                                        (ws, fs)) or ""
                if (n_there, there["last_scan_at"] or "") >= (n_here, here_scan):
                    self.purge_source(ws, fs)       # the target's copy wins; drop this one
                    moved.append(fs)
                    continue
                self.purge_source(target, fs)       # this copy wins; make room for it
            with self.tx() as c:
                # File vectors are rebuilt under the new workspace (cheap: no re-embedding).
                self._fv_drop(c, "workspace_id=? AND filespace=?", (ws, fs))
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
            c.execute("DELETE FROM discover_groups WHERE workspace_id=? AND filespace=?", (ws, fs))
            c.execute("DELETE FROM discover_scopes WHERE workspace_id=? AND filespace=?", (ws, fs))
            self._fv_drop(c, "workspace_id=? AND filespace=?", (ws, fs))
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

    # A row's GET /index/recent timestamp in integer microseconds: when the indexer
    # saw a deletion or rename (changed_at), else the file's own mtime in whichever
    # unit it is stored (see service.mtime_seconds). Integer arithmetic, so a
    # cursor round-trips exactly.
    _MTIME_US = ("(CASE WHEN mtime > 1e17 THEN mtime / 1000 WHEN mtime > 1e14 THEN mtime "
                 "WHEN mtime > 1e11 THEN mtime * 1000 ELSE mtime * 1000000 END)")
    _BY_CHANGE = "((status = 'deleted' OR change_kind = 'renamed') AND changed_at IS NOT NULL)"
    _CHANGED_US = "(CAST(strftime('%s', changed_at) AS INTEGER) * 1000000)"

    def recent_files(self, scopes: Iterable[tuple[str, str]], limit: int,
                     latest: Optional[float] = None, *, since_us: Optional[int] = None,
                     before_us: Optional[int] = None, changes: Optional[Iterable[str]] = None,
                     exts: Optional[Iterable[str]] = None) -> list[dict]:
        """Candidate rows for GET /index/recent, per scope newest first: live files
        by mtime, deletions and renames by changed_at (`when_us`, integer
        microseconds, is added to each row). Each scope gives its newest `limit`
        rows plus every row tied with the last of them, so a page cut at `limit`
        across scopes never splits a tie. The caller merges the scopes.

        Filters apply before the limit: `since_us` ≤ when < `before_us`;
        `changes` keeps those change kinds; `exts` keeps names ending in one of
        them (".pdf"…, ASCII case folded; a name that is only the extension, a
        hidden file, has none).

        `latest` (Unix seconds) leaves out live files dated after it, in whichever
        unit their mtime is stored: a file stamped in the future by a wrong clock is
        not a recent change, and would otherwise crowd out every real one."""
        when = (f"(CASE WHEN {self._BY_CHANGE} THEN COALESCE({self._CHANGED_US}, {self._MTIME_US}) "
                f"ELSE {self._MTIME_US} END)")
        where = ["workspace_id=?", "filespace=?", "(status != 'deleted' OR changed_at IS NOT NULL)"]
        args: list = []
        if latest is not None:
            where.append(f"NOT (NOT {self._BY_CHANGE} AND {self._MTIME_US} > ?)")
            args.append(int(latest * 1e6))
        if changes is not None:
            changes = sorted(set(changes))
            if not changes:
                return []
            where.append(f"change_kind IN ({','.join('?' * len(changes))})")
            args += changes
        if exts is not None:
            exts = sorted(set(exts))
            if not exts:
                return []
            where.append("(" + " OR ".join("(path LIKE ? ESCAPE '\\' AND path NOT LIKE ? ESCAPE '\\' AND path != ?)"
                                           for _ in exts) + ")")
            for e in exts:
                args += ["%" + like_escape(e), "%/" + like_escape(e), e]
        # `w` is computed in the inner select; the time window filters on it outside.
        outer, wargs = [], []
        if since_us is not None:
            outer.append("w >= ?")
            wargs.append(int(since_us))
        if before_us is not None:
            outer.append("w < ?")
            wargs.append(int(before_us))
        sql = (f"WITH c AS (SELECT * FROM (SELECT *, {when} AS w FROM files WHERE {' AND '.join(where)})"
               f"{' WHERE ' + ' AND '.join(outer) if outer else ''}) "
               "SELECT * FROM c WHERE w >= COALESCE((SELECT w FROM c ORDER BY w DESC LIMIT 1 OFFSET ?), w) "
               "ORDER BY w DESC, path")
        out: list[dict] = []
        for ws, fs in scopes:
            for r in self.query(sql, (ws, fs, *args, *wargs, max(1, int(limit)) - 1)):
                r["when_us"] = r.pop("w")
                out.append(r)
        return out

    def find_files(self, scopes: Iterable[tuple[str, str]], compact: str, words: list[str],
                   limit: int) -> list[dict]:
        """Live files whose folded path contains `compact` (the whole folded query)
        or every folded word, best name tier first, then newest, per scope.

        Folding (fold_name) ignores case, spaces, hyphens, underscores and dots.
        Substring match cannot use an index, so this scans the scope's ledger;
        the folded name/path are stored columns (kept by triggers on every path
        write) because folding inside the query made the scan ~3x slower.
        tier: 0 name starts with compact, 1 name contains it, 2 name contains
        every word, 3 only the path matches.
        """
        if not compact:
            return []
        words = [w for w in words if w] or [compact]
        all_in = " AND ".join("instr(path_fold, ?) > 0" for _ in words)
        name_all = " AND ".join("instr(name_fold, ?) > 0" for _ in words)
        sql = (f"SELECT workspace_id, filespace, path, size, mtime, status, reason, name_fold AS nn, "
               f"CASE WHEN substr(name_fold, 1, ?) = ? THEN 0 WHEN instr(name_fold, ?) > 0 THEN 1 "
               f"WHEN {name_all} THEN 2 ELSE 3 END AS tier FROM files "
               f"WHERE workspace_id=? AND filespace=? AND status != 'deleted' AND {all_in} "
               f"ORDER BY tier, mtime DESC LIMIT ?")
        out: list[dict] = []
        for ws, fs in scopes:
            out += self.query(sql, (len(compact), compact, compact, *words, ws, fs, *words, limit))
        return out

    # ------------------------------------------------------------ keyword search
    @staticmethod
    def _scope_sql(scopes: list[tuple[str, str]], path_prefix: Optional[str]) -> tuple[str, list]:
        where = "(c.workspace_id, c.filespace) IN (VALUES " + ",".join("(?, ?)" for _ in scopes) + ")"
        args: list = [x for sc in scopes for x in sc]
        if path_prefix and path_prefix != "/":
            p = path_prefix.rstrip("/")
            where += " AND (f.path = ? OR f.path LIKE ? ESCAPE '\\')"
            args += [p, like_escape(p) + "/%"]
        return where, args

    @staticmethod
    def _files_sql(files: Optional[Iterable[tuple[str, str, str]]], alias: str = "c") -> tuple[str, list]:
        """` AND (ws, fs, file_id) IN (...)` for a request limited to some files (empty: no limit)."""
        if files is None:
            return "", []
        keys = sorted(set(files))
        if not keys:
            return " AND 0", []
        sql = (f" AND ({alias}.workspace_id, {alias}.filespace, {alias}.file_id) IN (VALUES "
               + ",".join("(?, ?, ?)" for _ in keys) + ")")
        return sql, [x for k in keys for x in k]

    def files_at_paths(self, refs: Iterable[tuple[str, str, str]]) -> list[dict]:
        """Live ledger rows (any status but deleted) at these (workspace, filespace, path)s."""
        refs = list(dict.fromkeys(refs))
        out: list[dict] = []
        for i in range(0, len(refs), 300):
            part = refs[i:i + 300]
            out += self.query(
                "SELECT workspace_id, filespace, file_id, path, size, mtime, status FROM files "
                "WHERE status != 'deleted' AND (workspace_id, filespace, path) IN (VALUES "
                + ",".join("(?, ?, ?)" for _ in part) + ") ORDER BY mtime DESC",
                [x for r in part for x in r])
        return out

    def point_ids_of(self, files: Iterable[tuple[str, str, str]]) -> set[int]:
        """Chunk point ids of these (workspace, filespace, file_id)s."""
        keys = sorted(set(files))
        out: set[int] = set()
        for i in range(0, len(keys), 300):
            part = keys[i:i + 300]
            out |= {r["point_id"] for r in self.query(
                "SELECT point_id FROM chunks WHERE (workspace_id, filespace, file_id) IN (VALUES "
                + ",".join("(?, ?, ?)" for _ in part) + ")", [x for k in part for x in k])}
        return out

    def keyword_search(self, scopes: Iterable[tuple[str, str]], match: str, limit: int,
                       path_prefix: Optional[str] = None, ranked: bool = True,
                       files: Optional[Iterable[tuple[str, str, str]]] = None) -> list[tuple[int, float]]:
        """Chunks matching the FTS5 query `match`, best BM25 first: [(point_id, bm25)].

        bm25() is negative (more negative = better). Empty when the index is not
        ready or the query is rejected. ranked=False (a term in a large share of
        the corpus, where ranking every match would cost ~1 s per million) takes
        the newest matching chunks instead, which stops after `limit` rows.
        `files`: only chunks of these (workspace, filespace, file_id)s.
        """
        scopes = sorted(scopes)
        if not scopes or not match or not self.fts_ready:
            return []
        where, args = self._scope_sql(scopes, path_prefix)
        fsql, fargs = self._files_sql(files)
        where, args = where + fsql, args + fargs
        sql = ("SELECT c.point_id, bm25(chunks_fts) AS r FROM chunks_fts "
               "JOIN chunks c ON c.point_id = chunks_fts.rowid "
               "JOIN files f ON f.workspace_id=c.workspace_id AND f.filespace=c.filespace AND f.file_id=c.file_id "
               f"WHERE chunks_fts MATCH ? AND f.status != 'deleted' AND {where} "
               + ("ORDER BY r LIMIT ?" if ranked else "ORDER BY chunks_fts.rowid DESC LIMIT ?"))
        if not ranked:
            sql = sql.replace("bm25(chunks_fts) AS r", "0.0 AS r")
        try:
            with self._lock:
                rows = self._conn.execute(sql, (match, *args, max(1, int(limit)))).fetchall()
        except sqlite3.OperationalError as e:      # never let a query string break search
            logger.warning("keyword search rejected %r: %s", match, e)
            return []
        return [(int(r[0]), float(r[1])) for r in rows]

    def files_mentioning(self, keys: Iterable[tuple[str, str, str]], match: str) -> set[tuple[str, str, str]]:
        """The (workspace_id, filespace, file_id) of `keys` with a chunk matching `match`."""
        keys = sorted(set(keys))
        if not keys or not match or not self.fts_ready:
            return set()
        out: set[tuple[str, str, str]] = set()
        for i in range(0, len(keys), 300):
            part = keys[i:i + 300]
            sql = ("SELECT DISTINCT c.workspace_id, c.filespace, c.file_id FROM chunks_fts "
                   "JOIN chunks c ON c.point_id = chunks_fts.rowid WHERE chunks_fts MATCH ? AND "
                   "(c.workspace_id, c.filespace, c.file_id) IN (VALUES " + ",".join("(?, ?, ?)" for _ in part) + ")")
            try:
                with self._lock:
                    rows = self._conn.execute(sql, (match, *[x for k in part for x in k])).fetchall()
            except sqlite3.OperationalError as e:
                logger.warning("keyword filter rejected %r: %s", match, e)
                return set()
            out |= {(r[0], r[1], r[2]) for r in rows}
        return out

    def term_docs(self, terms: Iterable[str]) -> dict[str, int]:
        """Corpus document frequency (chunks containing it) of indexed terms."""
        terms = sorted(set(terms))
        if not terms or not self.fts_available:
            return {}
        out = {t: 0 for t in terms}
        for i in range(0, len(terms), 200):
            part = terms[i:i + 200]
            for r in self.query("SELECT term, doc FROM chunks_fts_vocab WHERE term IN (" + ",".join("?" * len(part))
                                + ")", part):
                out[r["term"]] = int(r["doc"])
        return out

    def prefix_docs(self, prefix: str, cap: int = 1000) -> int:
        """Sum of chunk frequencies of up to `cap` indexed terms starting with `prefix` (a lower bound)."""
        if not prefix or not self.fts_available:
            return 0
        r = self.scalar("SELECT COALESCE(SUM(doc), 0) FROM (SELECT doc FROM chunks_fts_vocab WHERE term >= ? "
                        "AND term < ? LIMIT ?)", (prefix, prefix + "\U0010ffff", cap))
        return int(r or 0)

    def has_chunks(self, scopes: Iterable[tuple[str, str]]) -> bool:
        return any(self.scalar("SELECT 1 FROM chunks WHERE workspace_id=? AND filespace=? LIMIT 1", sc)
                   for sc in scopes)

    def sample_texts(self, n: int, seed: int = 0) -> list[str]:
        """About `n` chunk texts at random point ids (corpus statistics for refinements)."""
        import random
        top = self.scalar("SELECT COALESCE(MAX(point_id), 0) FROM chunks") or 0
        if not top:
            return []
        rng = random.Random(seed)
        ids = sorted({rng.randrange(1, top + 1) for _ in range(n)})
        out: list[str] = []
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            out += [r["text"] for r in self.query(
                f"SELECT text FROM chunks WHERE point_id IN ({','.join('?' * len(part))})", part)]
        return out

    def name_chunk_hits(self, scopes: Iterable[tuple[str, str]], compact: str, limit: int,
                        path_prefix: Optional[str] = None,
                        files: Optional[Iterable[tuple[str, str, str]]] = None) -> list[int]:
        """First chunk of indexed files whose folded name contains `compact`, best
        (name starts with it) and newest first: the "file is called that" channel."""
        scopes = sorted(scopes)
        if not scopes or len(compact) < 3:
            return []
        where, args = self._scope_sql(scopes, path_prefix)
        where = where.replace("c.workspace_id", "f.workspace_id").replace("c.filespace", "f.filespace")
        fsql, fargs = self._files_sql(files, "f")
        where, args = where + fsql, args + fargs
        sql = ("SELECT c.point_id FROM files f JOIN chunks c ON c.workspace_id=f.workspace_id "
               "AND c.filespace=f.filespace AND c.file_id=f.file_id AND c.chunk_index=0 "
               f"WHERE f.status = 'indexed' AND instr(f.name_fold, ?) > 0 AND {where} "
               "ORDER BY substr(f.name_fold, 1, ?) = ? DESC, f.mtime DESC LIMIT ?")
        return [r["point_id"] for r in self.query(sql, (compact, *args, len(compact), compact, max(1, int(limit))))]

    def live_totals(self, ws: Optional[str] = None, fs: Optional[str] = None) -> dict[tuple[str, str], dict]:
        """{(ws, fs): {"files", "bytes"}} of live files: one GROUP BY over the
        ledger for every source, or just (ws, fs) when given."""
        where, args = "status != 'deleted'", ()
        if ws is not None:
            where, args = "workspace_id=? AND filespace=? AND status != 'deleted'", (ws, fs)
        return {(r["workspace_id"], r["filespace"]): {"files": r["files"], "bytes": r["bytes"]}
                for r in self.query("SELECT workspace_id, filespace, COUNT(*) AS files, "
                                    f"COALESCE(SUM(size), 0) AS bytes FROM files WHERE {where} "
                                    "GROUP BY workspace_id, filespace", args)}

    def files_with_extensions(self, scopes: Iterable[tuple[str, str]], exts: Iterable[str]) -> list[dict]:
        """Live files whose name ends in one of `exts` (".pdf"…; LIKE folds ASCII
        case), per scope. Candidates for GET /index/bykind: the caller classifies
        each name exactly and orders them (mtime units vary)."""
        exts = sorted(set(exts))
        if not exts:
            return []
        like = " OR ".join("path LIKE ? ESCAPE '\\'" for _ in exts)
        pats = ["%" + like_escape(e) for e in exts]
        out: list[dict] = []
        for ws, fs in scopes:
            out += self.query("SELECT workspace_id, filespace, path, size, mtime, status FROM files "
                              f"WHERE workspace_id=? AND filespace=? AND status != 'deleted' AND ({like})",
                              (ws, fs, *pats))
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
                self._fv_drop(c, "workspace_id=? AND filespace=? AND file_id=?", (ws, fs, file_id))
        if written:
            self.bump()
        return written

    def delete_file_chunks(self, ws: str, fs: str, file_id: str) -> int:
        with self.tx() as c:
            n = c.execute("DELETE FROM chunks WHERE workspace_id=? AND filespace=? AND file_id=?",
                          (ws, fs, file_id)).rowcount
            if n:
                self._fv_drop(c, "workspace_id=? AND filespace=? AND file_id=?", (ws, fs, file_id))
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
                    f"f.path, f.mtime, f.size FROM chunks c JOIN files f ON f.workspace_id=c.workspace_id "
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

    def root_surveys(self) -> list[dict]:
        """Every stored root-scope ("/") survey, for GET /index/surveys."""
        return self.query("SELECT * FROM surveys WHERE scope='/'")

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
            self._fv_drop(c, "workspace_id=? AND filespace=? AND file_id=?", (ws, fs, old_id))

    def mirror_forget_file(self, ws: str, fs: str, file_id: str) -> None:
        """Make a file's live points look unmirrored so metadata is re-upserted."""
        self.execute("DELETE FROM mirror_points WHERE point_id IN (SELECT point_id FROM chunks "
                     "WHERE workspace_id=? AND filespace=? AND file_id=?)", (ws, fs, file_id))
        self.bump()   # the mirror now has work to do; a clean-sync shortcut must not skip it

    def mirror_counts(self, collection: str) -> tuple[int, int]:
        """(live mirrored points, dead mirrored points)."""
        total = self.scalar("SELECT COUNT(*) FROM mirror_points WHERE collection=?", (collection,)) or 0
        dead = self.scalar("SELECT COUNT(*) FROM mirror_points m WHERE m.collection=? AND NOT EXISTS "
                           "(SELECT 1 FROM chunks c WHERE c.point_id=m.point_id)", (collection,)) or 0
        return total - dead, dead

    def live_point_ids(self, point_ids: list[int]) -> set[int]:
        return set(self.chunk_rows(point_ids).keys())

    # ------------------------------------------------------------ vector scans
    def chunk_total(self, ttl: float = 900.0) -> int:
        """Chunk count, cached for `ttl` seconds (COUNT(*) takes 1–4 s on a million
        chunks); a small count, cheap to take, is re-taken on any change. The
        file-vector builder refreshes it every few minutes (ttl=240), so a
        request rarely pays for the count."""
        now = time.monotonic()
        cached = getattr(self, "_chunk_total", None)
        if (cached is None or now - cached[0] > ttl
                or (cached[2] != self._generation and cached[1] < 100_000)):
            with self.read_view():          # a long count must not hold the shared lock
                n = self.chunk_count()
            cached = (now, n, self._generation)
            self._chunk_total = cached
        return cached[1]

    def iter_chunk_vectors(self, model: str, scopes: Optional[Iterable[tuple[str, str]]] = None,
                           path_prefix: Optional[str] = None, point_ids: Optional[Iterable[int]] = None,
                           batch: int = 20000) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        """(point ids, vectors) of live chunks in batches, on a read connection:
        an exact vector search over any number of chunks in bounded memory.
        `point_ids`: only those chunks (in batches of 500 ids)."""
        base = ("SELECT c.point_id, e.vector FROM chunks c JOIN embeddings e ON e.model=? AND e.text_hash=c.text_hash "
                "JOIN files f ON f.workspace_id=c.workspace_id AND f.filespace=c.filespace AND f.file_id=c.file_id "
                "WHERE f.status != 'deleted'")
        where, args = "", []
        if scopes is not None:
            scopes = sorted(set(scopes))
            if not scopes:
                return
            where, args = self._scope_sql(scopes, path_prefix)
            where = " AND " + where
        elif path_prefix and path_prefix != "/":
            p = path_prefix.rstrip("/")
            where, args = " AND (f.path = ? OR f.path LIKE ? ESCAPE '\\')", [p, like_escape(p) + "/%"]
        if point_ids is not None:
            ids = sorted(set(int(p) for p in point_ids))
            parts = [(f" AND c.point_id IN ({','.join('?' * len(ids[i:i + 500]))})", ids[i:i + 500])
                     for i in range(0, len(ids), 500)]
        else:
            parts = [("", [])]
        with self.reader() as conn:
            for psql, pargs in parts:
                cur = conn.execute(base + where + psql, (model, *args, *pargs))
                while True:
                    rows = cur.fetchmany(batch)
                    if not rows:
                        break
                    dims = {len(r[1]) for r in rows}
                    dim = max(dims, key=[len(r[1]) for r in rows].count) if len(dims) > 1 else dims.pop()
                    rows = [r for r in rows if len(r[1]) == dim]
                    yield (np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows)),
                           np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1))

    # ------------------------------------------------------------ file vectors
    def _fv_drop(self, c: sqlite3.Connection, where: str, args: Iterable) -> None:
        """Drop file vectors matching `where` (vector NULL under a new seq), so the
        file index forgets them and the builder makes them again."""
        args = tuple(args)
        if c.execute(f"SELECT 1 FROM file_vectors WHERE vector IS NOT NULL AND {where} LIMIT 1", args).fetchone():
            c.execute(f"UPDATE file_vectors SET vector=NULL, chunks=0, seq=? WHERE vector IS NOT NULL AND {where}",
                      (self._fv_next_seq(c), *args))

    @staticmethod
    def _fv_next_seq(c: sqlite3.Connection) -> int:
        c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('fv_seq', '0')")
        c.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='fv_seq'")
        return int(c.execute("SELECT value FROM meta WHERE key='fv_seq'").fetchone()[0])

    def file_vector_todo(self, ws: str, fs: str, model: str, limit: int = 200) -> list[str]:
        """file_ids of indexed files (with chunks) in a source that have no file vector."""
        return [r["file_id"] for r in self.query(
            "SELECT f.file_id FROM files f WHERE f.workspace_id=? AND f.filespace=? AND f.status='indexed' "
            "AND f.chunk_count > 0 AND NOT EXISTS (SELECT 1 FROM file_vectors v WHERE v.workspace_id=f.workspace_id "
            "AND v.filespace=f.filespace AND v.file_id=f.file_id AND v.model=? AND v.vector IS NOT NULL) LIMIT ?",
            (ws, fs, model, int(limit)))]

    def file_mean_vector(self, ws: str, fs: str, file_id: str, model: str) -> Optional[tuple[np.ndarray, int]]:
        """(normalised mean of the file's chunk vectors, chunks used), None when it has none."""
        rows = self.query("SELECT e.vector FROM chunks c JOIN embeddings e ON e.model=? AND e.text_hash=c.text_hash "
                          "WHERE c.workspace_id=? AND c.filespace=? AND c.file_id=?", (model, ws, fs, file_id))
        vecs = [blob_to_vec(r["vector"]) for r in rows]
        if not vecs:
            return None
        dim = max(set(len(v) for v in vecs), key=[len(v) for v in vecs].count)
        mean = np.mean([v for v in vecs if len(v) == dim], axis=0)
        norm = float(np.linalg.norm(mean))
        if norm == 0.0:
            return None
        return (mean / norm).astype(np.float32), len(vecs)

    def file_mean_vectors(self, ws: str, fs: str, file_ids: list[str],
                          model: str) -> dict[str, tuple[np.ndarray, int]]:
        """file_mean_vector for many files of one source in one query, on a read
        connection (the builder must not queue behind indexing for every file)."""
        if not file_ids:
            return {}
        vecs: dict[str, list[np.ndarray]] = {}
        with self.reader() as conn:
            for i in range(0, len(file_ids), 300):
                part = file_ids[i:i + 300]
                for fid, blob in conn.execute(
                        "SELECT c.file_id, e.vector FROM chunks c JOIN embeddings e ON e.model=? "
                        "AND e.text_hash=c.text_hash WHERE c.workspace_id=? AND c.filespace=? "
                        f"AND c.file_id IN ({','.join('?' * len(part))})", (model, ws, fs, *part)):
                    vecs.setdefault(fid, []).append(blob_to_vec(blob))
        out = {}
        for fid, vs in vecs.items():
            dim = max(set(len(v) for v in vs), key=[len(v) for v in vs].count)
            mean = np.mean([v for v in vs if len(v) == dim], axis=0)
            norm = float(np.linalg.norm(mean))
            if norm:
                out[fid] = ((mean / norm).astype(np.float32), len(vs))
        return out

    def put_file_vectors(self, model: str, items: list[tuple[str, str, str, int, np.ndarray]]) -> None:
        """Write [(ws, fs, file_id, chunks, vector)] under one new seq."""
        if not items:
            return
        with self.tx() as c:
            seq = self._fv_next_seq(c)
            c.executemany("INSERT OR REPLACE INTO file_vectors (workspace_id, filespace, file_id, model, seq, chunks, "
                          "vector) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          [(ws, fs, fid, model, seq, n, vec_to_blob(v)) for ws, fs, fid, n, v in items])

    def get_file_vector(self, ws: str, fs: str, file_id: str, model: str) -> Optional[np.ndarray]:
        r = self.one("SELECT vector FROM file_vectors WHERE workspace_id=? AND filespace=? AND file_id=? AND model=? "
                     "AND vector IS NOT NULL", (ws, fs, file_id, model))
        return blob_to_vec(r["vector"]) if r else None

    def iter_file_vectors(self, model: str, after_seq: int, batch: int = 5000) -> Iterator[list[tuple]]:
        """File vector rows written after `after_seq`, oldest first, in batches of
        (workspace_id, filespace, file_id, seq, vector|None)."""
        with self.reader() as conn:
            cur = conn.execute("SELECT workspace_id, filespace, file_id, seq, vector FROM file_vectors "
                               "WHERE model=? AND seq > ? ORDER BY seq", (model, int(after_seq)))
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    return
                yield [tuple(r) for r in rows]

    # ------------------------------------------------------------ related files
    def live_rows(self, keys: Iterable[tuple[str, str, str]]) -> dict[tuple[str, str, str], dict]:
        """Ledger rows of live files by (workspace_id, filespace, file_id)."""
        keys = sorted(set(keys))
        out: dict[tuple[str, str, str], dict] = {}
        for i in range(0, len(keys), 300):
            part = keys[i:i + 300]
            for r in self.query(
                    "SELECT workspace_id, filespace, file_id, path, size, mtime, status, chunk_count FROM files "
                    "WHERE status != 'deleted' AND (workspace_id, filespace, file_id) IN (VALUES "
                    + ",".join("(?, ?, ?)" for _ in part) + ")", [x for k in part for x in k]):
                out[(r["workspace_id"], r["filespace"], r["file_id"])] = r
        return out

    _ROW_COLS = "workspace_id, filespace, file_id, path, size, mtime, status, chunk_count"

    def files_name_containing(self, scopes: Iterable[tuple[str, str]], needle: str, limit: int) -> list[dict]:
        """Live files in `scopes` whose folded name contains `needle` (version
        candidates), at most `limit`. One scan of the name index (~0.1 s per
        700k files; per-scope table scans took seconds), then those rows."""
        ids = [r["rowid"] for r in self.query(
            "SELECT rowid FROM files INDEXED BY files_name_fold WHERE instr(name_fold, ?) > 0", (needle,))]
        scopes = set(scopes)
        out: list[dict] = []
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            out += [r for r in self.query(f"SELECT {self._ROW_COLS} FROM files WHERE rowid IN "
                                          f"({','.join('?' * len(part))}) AND status != 'deleted'", part)
                    if (r["workspace_id"], r["filespace"]) in scopes]
            if len(out) >= limit:
                break
        return out[:limit]

    def files_named(self, folds: Iterable[str]) -> list[dict]:
        """Live files whose folded name is one of `folds` (any location)."""
        folds = sorted(set(f for f in folds if f))
        out: list[dict] = []
        for i in range(0, len(folds), 300):
            part = folds[i:i + 300]
            out += self.query(f"SELECT {self._ROW_COLS}, name_fold FROM files WHERE status != 'deleted' "
                              f"AND name_fold IN ({','.join('?' * len(part))})", part)
        return out

    def shared_passages(self, ws: str, fs: str, file_id: str, max_spread: int = 20,
                        limit: int = 500) -> tuple[int, dict[tuple[str, str, str], int]]:
        """(this file's distinct passages, {other file: passages it shares}). A
        passage found in more than `max_spread` chunks (a disclaimer, a letterhead)
        says nothing about versions and is ignored."""
        mine = [r["text_hash"] for r in self.query(
            "SELECT DISTINCT text_hash FROM chunks WHERE workspace_id=? AND filespace=? AND file_id=?", (ws, fs, file_id))]
        out: dict[tuple[str, str, str], int] = {}
        for i in range(0, len(mine), 300):
            part = mine[i:i + 300]
            marks = ",".join("?" * len(part))
            rare = [r["text_hash"] for r in self.query(
                f"SELECT text_hash FROM chunks WHERE text_hash IN ({marks}) GROUP BY text_hash "
                f"HAVING COUNT(*) <= ?", (*part, int(max_spread) + 1))]
            if not rare:
                continue
            for r in self.query(
                    "SELECT c.workspace_id, c.filespace, c.file_id, COUNT(DISTINCT c.text_hash) AS n FROM chunks c "
                    f"WHERE c.text_hash IN ({','.join('?' * len(rare))}) AND NOT (c.workspace_id=? AND c.filespace=? "
                    "AND c.file_id=?) GROUP BY c.workspace_id, c.filespace, c.file_id LIMIT ?",
                    (*rare, ws, fs, file_id, int(limit))):
                key = (r["workspace_id"], r["filespace"], r["file_id"])
                out[key] = out.get(key, 0) + r["n"]
        return len(mine), out

    def file_texts(self, ws: str, fs: str, file_id: str, limit: int = 200) -> list[str]:
        return [r["text"] for r in self.query(
            "SELECT text FROM chunks WHERE workspace_id=? AND filespace=? AND file_id=? ORDER BY chunk_index LIMIT ?",
            (ws, fs, file_id, int(limit)))]

    def match_files(self, scopes: Iterable[tuple[str, str]], match: str,
                    limit: int = 300) -> dict[tuple[str, str, str], int]:
        """{live file: its first chunk point id matching the FTS5 query `match`}, over at most `limit` chunks."""
        scopes = sorted(set(scopes))
        if not scopes or not match or not self.fts_ready:
            return {}
        where, args = self._scope_sql(scopes, None)
        sql = ("SELECT c.workspace_id, c.filespace, c.file_id, MIN(c.point_id) AS pid FROM "
               "(SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT ?) m "
               "JOIN chunks c ON c.point_id = m.rowid "
               "JOIN files f ON f.workspace_id=c.workspace_id AND f.filespace=c.filespace AND f.file_id=c.file_id "
               f"WHERE f.status != 'deleted' AND {where} GROUP BY c.workspace_id, c.filespace, c.file_id")
        try:
            with self._cursor() as c:
                rows = c.execute(sql, (match, int(limit), *args)).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning("related match rejected %r: %s", match, e)
            return {}
        return {(r[0], r[1], r[2]): int(r[3]) for r in rows}

    # ------------------------------------------------------------ discovered collections
    def fv_seq(self) -> int:
        """The file-vector write counter: it moves whenever a file's meaning changes."""
        return int(self.scalar("SELECT CAST(value AS INTEGER) FROM meta WHERE key='fv_seq'") or 0)

    def discover_files(self, ws: str, fs: str) -> list[dict]:
        """Indexed files of one source (the files discovery groups)."""
        return self.query(f"SELECT {self._ROW_COLS} FROM files WHERE workspace_id=? AND filespace=? "
                          "AND status='indexed'", (ws, fs))

    def iter_discover_texts(self, ws: str, fs: str, max_chunks: int,
                            batch: int = 2000) -> Iterator[tuple[str, list[str]]]:
        """(file_id, its first `max_chunks` chunk texts) for every file of a source
        with chunks, on a read-only connection."""
        with self.reader() as c:
            cur = c.execute("SELECT file_id, text FROM chunks WHERE workspace_id=? AND filespace=? "
                            "AND chunk_index < ? ORDER BY file_id, chunk_index", (ws, fs, int(max_chunks)))
            fid, texts = None, []
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    break
                for f, t in rows:
                    if f != fid:
                        if fid is not None:
                            yield fid, texts
                        fid, texts = f, []
                    texts.append(t)
            if fid is not None:
                yield fid, texts

    def shared_hash_files(self, ws: str, fs: str, max_spread: int) -> Iterator[list[str]]:
        """For each passage found in 2..max_spread files of a source: those file_ids."""
        with self.reader() as c:
            cur = c.execute(
                "SELECT json_group_array(DISTINCT file_id) FROM chunks WHERE workspace_id=? AND filespace=? "
                "GROUP BY text_hash HAVING COUNT(DISTINCT file_id) BETWEEN 2 AND ?", (ws, fs, int(max_spread)))
            while True:
                rows = cur.fetchmany(5000)
                if not rows:
                    break
                for (ids,) in rows:
                    yield json.loads(ids)

    def distinct_passages(self, ws: str, fs: str) -> dict[str, int]:
        """{file_id: distinct passages} for a source."""
        with self.reader() as c:
            return {f: int(n) for f, n in c.execute(
                "SELECT file_id, COUNT(DISTINCT text_hash) FROM chunks WHERE workspace_id=? AND filespace=? "
                "GROUP BY file_id", (ws, fs))}

    def discover_scopes(self) -> list[dict]:
        return self.query("SELECT * FROM discover_scopes ORDER BY workspace_id, filespace")

    def discover_scope(self, ws: str, fs: str) -> Optional[dict]:
        return self.one("SELECT * FROM discover_scopes WHERE workspace_id=? AND filespace=?", (ws, fs))

    def discover_request(self, ws: str, fs: str) -> None:
        """Switch discovery on for a source and ask for a run."""
        self.execute("INSERT INTO discover_scopes (workspace_id, filespace, enabled, requested_at) VALUES (?, ?, 1, ?) "
                     "ON CONFLICT (workspace_id, filespace) DO UPDATE SET enabled=1, requested_at=excluded.requested_at",
                     (ws, fs, now_iso()))

    def discover_disable(self, ws: str, fs: str) -> bool:
        return self.execute("UPDATE discover_scopes SET enabled=0, requested_at=NULL "
                            "WHERE workspace_id=? AND filespace=?", (ws, fs)) > 0

    def discover_mark(self, ws: str, fs: str, **fields) -> None:
        """Update run fields (started_at, finished_at, fv_seq, error, stats, requested_at)."""
        if "stats" in fields and fields["stats"] is not None:
            fields["stats"] = json.dumps(fields["stats"])
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE discover_scopes SET {cols} WHERE workspace_id=? AND filespace=?",
                     (*fields.values(), ws, fs))

    def discover_group_rows(self, ws: Optional[str] = None, fs: Optional[str] = None,
                            statuses: Optional[Iterable[str]] = None) -> list[dict]:
        where, args = ["1=1"], []
        if ws is not None:
            where.append("workspace_id=?")
            args.append(ws)
        if fs is not None:
            where.append("filespace=?")
            args.append(fs)
        if statuses is not None:
            st = list(statuses)
            where.append(f"status IN ({','.join('?' * len(st))})")
            args += st
        return self.query(f"SELECT * FROM discover_groups WHERE {' AND '.join(where)} "
                          "ORDER BY score DESC, id", args)

    def discover_group_row(self, gid: str) -> Optional[dict]:
        return self.one("SELECT * FROM discover_groups WHERE id=?", (gid,))

    _GROUP_COLS = ("id", "workspace_id", "filespace", "status", "name", "named_by", "summary", "score", "size",
                   "keys", "data", "merged_into", "created_at", "updated_at")

    def discover_save(self, ws: str, fs: str, groups: list[dict], drop: Iterable[str] = ()) -> None:
        """Write (insert or replace) groups and delete `drop`, in one transaction."""
        with self.tx() as c:
            drop = list(drop)
            for i in range(0, len(drop), 500):
                part = drop[i:i + 500]
                c.execute(f"DELETE FROM discover_groups WHERE id IN ({','.join('?' * len(part))})", part)
            c.executemany(
                f"INSERT OR REPLACE INTO discover_groups ({', '.join(self._GROUP_COLS)}) "
                f"VALUES ({', '.join('?' * len(self._GROUP_COLS))})",
                [tuple(g.get(k) for k in self._GROUP_COLS) for g in groups])

    def discover_update(self, gid: str, **fields) -> bool:
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        return self.execute(f"UPDATE discover_groups SET {cols} WHERE id=?", (*fields.values(), gid)) > 0
