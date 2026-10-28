"""Delta detection and indexing for one filespace.

A scan lists metadata only (`read_dir`) and compares it to the ledger in
index.db, keyed by the SDK's stable `file_id`:

* unknown file_id, or (size, mtime) differs  -> queued (status `pending`)
* same file_id at a new path                 -> path updated in place; chunks and
                                                vectors are kept, only mirror
                                                metadata is refreshed
* same (size, mtime)                          -> nothing read, nothing embedded
* not seen by a scan in which *every*
  directory listed successfully               -> tombstoned, chunks removed

A listing failure anywhere makes the scan incomplete and nothing is deleted:
a transient error must never look like a deletion.

Indexing then drains the queue newest-mtime first. Reading a queued file and
finding the same sha256 as last time (a touch) only refreshes the ledger;
otherwise text is extracted, chunked, and embedded through the cache, so an edit
re-embeds only the chunks whose text changed.
"""
from __future__ import annotations

import hashlib
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .chunker import chunk_text
from .config import Config
from .embedder import Embedder, text_hash
from .extract import ext_of, extract, is_supported
from .source import FilespaceSource
from .store import Store, now_iso

logger = logging.getLogger("indexer.scanner")

# Same list as the web app's walkFilespace (src/features/docs/lib/filespace.ts);
# any name starting with "." is skipped as well.
SKIP_DIRS = {"node_modules", ".git", ".cache", ".trash", ".tmp", "$RECYCLE.BIN",
             ".lucidlink", ".DS_Store", "System Volume Information"}


def skip_dir(name: str) -> bool:
    return not name or name.startswith(".") or name in SKIP_DIRS


def skip_file(name: str) -> bool:
    # Dotfiles (".DS_Store", "._foo" resource forks) are never documents.
    return not name or name.startswith(".")


def join(parent: str, name: str) -> str:
    return (parent.rstrip("/") or "") + "/" + name


def parent_of(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[0] or "/"


def normpath(path: str) -> str:
    path = "/" + "/".join(p for p in (path or "").split("/") if p)
    return path


def hidden_path(path: str) -> bool:
    """True when any directory component of `path` is skipped by scans."""
    parts = [p for p in path.split("/") if p]
    return any(skip_dir(p) for p in parts[:-1]) or (bool(parts) and skip_file(parts[-1]))


@dataclass
class ScanResult:
    scan_id: int
    root: str
    complete: bool = True
    seen: int = 0
    new: int = 0
    changed: int = 0
    renamed: int = 0
    deleted: int = 0
    failed_dirs: list[str] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return self.new + self.changed + self.renamed + self.deleted


@dataclass
class IndexResult:
    indexed: int = 0
    touched: int = 0
    skipped: int = 0
    failed: int = 0
    chunks_written: int = 0

    @property
    def changes(self) -> int:
        return self.indexed + self.skipped + self.chunks_written


ProgressFn = Callable[[int, int, Optional[str]], None]


class FilespaceIndexer:
    def __init__(self, store: Store, source: FilespaceSource, workspace_id: str, filespace: str,
                 embedder: Embedder, config: Config):
        self.store = store
        self.source = source
        self.ws = workspace_id
        self.fs = filespace
        self.embedder = embedder
        self.config = config

    # ------------------------------------------------------------ ledger
    def _changed(self) -> None:
        self.store.mark_surveys_stale(self.ws, self.fs)

    def _tombstone(self, row: dict) -> None:
        self.store.delete_file_chunks(self.ws, self.fs, row["file_id"])
        self.store.update_file(self.ws, self.fs, row["file_id"], status="deleted", chunk_count=0)
        self._changed()

    def _retry_due(self, row: dict) -> bool:
        if not row.get("indexed_at"):
            return True
        try:
            at = datetime.fromisoformat(row["indexed_at"].replace("Z", "+00:00"))
        except ValueError:
            return True
        return datetime.now(timezone.utc) - at >= timedelta(seconds=self.config.failed_retry_sec)

    def observe(self, path: str, size: int, mtime: int, file_id: str, scan_id: int, res: ScanResult) -> None:
        """Reconcile one listed file with the ledger."""
        name = path.rsplit("/", 1)[-1]
        supported = is_supported(name)
        unsupported_reason = f"unsupported type (.{ext_of(name)})" if ext_of(name) else "unsupported type"
        row = self.store.get_file(self.ws, self.fs, file_id)
        res.seen += 1
        if row is None:
            self.store.insert_file(self.ws, self.fs, file_id, path, size, mtime, scan_id,
                                   status="pending" if supported else "skipped",
                                   reason=None if supported else unsupported_reason)
            res.new += 1
            return

        cols: dict = {"scan_id": scan_id}
        if row["path"] != path:
            # Rename/move: same content, same chunks and vectors. Re-upsert
            # mirror metadata (it carries the path) and refresh search caches.
            cols["path"] = path
            self.store.mirror_forget_file(self.ws, self.fs, file_id)
            self.store.bump()
            self._changed()
            res.renamed += 1
        if not supported:
            if row["status"] != "skipped" or row["reason"] != unsupported_reason:
                self.store.delete_file_chunks(self.ws, self.fs, file_id)
                cols.update(status="skipped", reason=unsupported_reason, chunk_count=0, content_hash=None)
            cols.update(size=size, mtime=mtime)
        elif row["status"] == "deleted" or (row["status"] == "skipped" and
                                            (row["reason"] or "").startswith("unsupported")):
            cols.update(status="pending", reason=None, size=size, mtime=mtime)
            res.new += 1
        elif (row["size"], row["mtime"]) != (size, mtime):
            # `reason` and `content_hash` are kept: the indexer uses them to
            # recognise a touch (same bytes) without re-extracting.
            cols.update(status="pending", size=size, mtime=mtime)
            res.changed += 1
        elif row["status"] == "failed" and self._retry_due(row):
            cols.update(status="pending")
        self.store.update_file(self.ws, self.fs, file_id, **cols)

    # ------------------------------------------------------------ scan
    def scan(self, root: str = "/", prune: bool = True) -> ScanResult:
        """Breadth-first metadata walk of `root`; tombstone unseen files under it
        only if every directory listed."""
        root = normpath(root)
        scan_id = self.store.next_scan_id(self.ws, self.fs)
        res = ScanResult(scan_id=scan_id, root=root)
        if root != "/" and hidden_path(root + "/x"):
            return res
        queue = deque([root])
        while queue:
            d = queue.popleft()
            try:
                entries = self.source.list_dir(d)
            except Exception as e:
                logger.warning("[%s/%s] listing %s failed: %s", self.ws, self.fs, d, e)
                res.failed_dirs.append(d)
                res.complete = False
                continue
            for e in entries:
                path = join(d, e.name)
                if e.type == "dir":
                    if not skip_dir(e.name):
                        queue.append(path)
                elif e.type == "file" and not skip_file(e.name):
                    self.observe(path, e.size, e.mtime, e.file_id, scan_id, res)
        if prune and res.complete:
            res.deleted = self._prune(root, scan_id)
        if root == "/":
            self.store.update_filespace(self.ws, self.fs, last_scan_at=now_iso())
        return res

    def _prune(self, root: str, scan_id: int) -> int:
        args: list = [self.ws, self.fs, scan_id]
        where = "workspace_id=? AND filespace=? AND status != 'deleted' AND scan_id != ?"
        if root != "/":
            prefix = root.rstrip("/") + "/"
            where += " AND (path = ? OR substr(path, 1, ?) = ?)"
            args += [root, len(prefix), prefix]
        rows = self.store.query(f"SELECT * FROM files WHERE {where}", args)
        for row in rows:
            self._tombstone(row)
        return len(rows)

    # ------------------------------------------------------------ hints
    def hint(self, op: str, path: str, src: Optional[str] = None) -> bool:
        """Apply a file-service change hint. Returns False when the targeted
        listing failed and a full scan is needed to be sure."""
        path = normpath(path)
        if op == "write":
            if hidden_path(path):
                return True
            entry = self.source.stat(path)
            if entry is None:
                return self.scan(parent_of(path)).complete
            if entry.type == "dir":
                return self.scan(path).complete
            res = ScanResult(scan_id=self.store.next_scan_id(self.ws, self.fs), root=path)
            self.observe(path, entry.size, entry.mtime, entry.file_id, res.scan_id, res)
            # A write that replaced the file (new file_id) leaves the old row behind.
            for row in self.store.live_files_at_path(self.ws, self.fs, path):
                if row["file_id"] != entry.file_id:
                    self._tombstone(row)
            return True
        if op == "mkdir":
            return self.scan(path).complete
        if op == "delete":
            return self.scan(parent_of(path)).complete
        if op == "move":
            # Destination first, so a moved file is re-pathed (keeping its
            # vectors) before the source side would tombstone it.
            ok = self.scan(parent_of(path)).complete
            if src:
                ok = self.scan(parent_of(normpath(src))).complete and ok
            return ok
        raise ValueError(f"unknown hint op '{op}'")

    # ------------------------------------------------------------ index
    def index_pending(self, progress: Optional[ProgressFn] = None,
                      should_stop: Optional[Callable[[], bool]] = None) -> IndexResult:
        """Drain the queue, newest first. EmbedError propagates (the embedder is
        down: stop, leave files pending); per-file problems are recorded."""
        out = IndexResult()
        queue = self.store.pending_files(self.ws, self.fs)
        for done, row in enumerate(queue):
            if should_stop and should_stop():
                break
            if progress:
                progress(done, len(queue), row["path"])
            outcome = self._index_file(row, out)
            setattr(out, outcome, getattr(out, outcome) + 1)
        if progress:
            progress(len(queue), len(queue), None)
        return out

    def _set(self, row: dict, **cols) -> None:
        self.store.update_file(self.ws, self.fs, row["file_id"], **cols)

    def _skip(self, row: dict, reason: str, content_hash: Optional[str] = None) -> str:
        if self.store.delete_file_chunks(self.ws, self.fs, row["file_id"]):
            self._changed()
        self._set(row, status="skipped", reason=reason, chunk_count=0, content_hash=content_hash,
                  indexed_at=now_iso())
        return "skipped"

    def _index_file(self, row: dict, out: IndexResult) -> str:
        path = row["path"]
        name = path.rsplit("/", 1)[-1]
        limit = self.config.max_file_bytes
        if not is_supported(name):
            return self._skip(row, f"unsupported type (.{ext_of(name)})")
        if row["size"] > limit:
            return self._skip(row, f"too large ({row['size']} bytes > {limit})")
        try:
            data = self.source.read_file(path)
        except Exception as e:
            self._set(row, status="failed", reason=f"read failed: {type(e).__name__}: {e}"[:300],
                      indexed_at=now_iso())
            return "failed"
        if len(data) > limit:
            return self._skip(row, f"too large ({len(data)} bytes > {limit})")

        digest = hashlib.sha256(data).hexdigest()
        if row["content_hash"] and digest == row["content_hash"]:
            # Touched, not changed: the previous outcome still holds.
            self._set(row, status="skipped" if row["reason"] else "indexed", size=len(data))
            return "touched"

        ext = extract(name, data)
        if ext.skip_reason:
            return self._skip(row, ext.skip_reason, digest)
        pieces = chunk_text(ext.text or "")
        if not pieces:
            return self._skip(row, "too little text", digest)

        self.embedder.embed_texts(pieces)  # fills the cache; raises EmbedError
        written = self.store.replace_chunks(self.ws, self.fs, row["file_id"],
                                            [(text_hash(p), p) for p in pieces])
        if written:
            self._changed()
        out.chunks_written += written
        self._set(row, status="indexed", reason=None, content_hash=digest, chunk_count=len(pieces),
                  size=len(data), indexed_at=now_iso())
        return "indexed"

    def backfill_embeddings(self, batch: int = 256) -> int:
        """Embed chunks that have no vector for the current model (model change)."""
        total = 0
        while True:
            rows = self.store.chunks_missing_embeddings(self.embedder.model, batch)
            if not rows:
                return total
            self.embedder.embed_texts([r["text"] for r in rows])
            total += len(rows)
