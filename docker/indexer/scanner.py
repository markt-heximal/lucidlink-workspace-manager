"""Delta detection and indexing for one filespace.

A scan lists metadata only (`read_dir`) and compares it to the ledger in
index.db. SDK 0.14's `read_dir` does not expose `file_id` (every entry has
""), and its mtime may be in other units than `get_entry`'s, so there are two
steps per file:

1. Fast path, by path: a live ledger row at this path whose stored listing
   values (`list_size`, `list_mtime`) equal what read_dir reports -> unchanged,
   no further call. An unchanged rescan is therefore read_dir only.
2. Otherwise `get_entry(path)` supplies the stable `file_id` and authoritative
   (size, mtime), and the ledger is reconciled by file_id:

* unknown file_id, or (size, mtime) differs  -> queued (status `pending`)
* same file_id at a new path                 -> path updated in place; chunks and
                                                vectors are kept, only mirror
                                                metadata is refreshed
* same (size, mtime)                          -> nothing read, nothing embedded
* not seen by a scan in which *every*
  directory listed successfully               -> tombstoned, chunks removed

A listing failure anywhere — or a get_entry that fails or returns no file_id —
makes the scan incomplete and nothing is deleted: a transient error must never
look like a deletion, and a row is never written without a file_id.

iWork packages (a directory named X.pages / X.numbers / X.key) are one file:
never recursed into, their (size, mtime) are the sum of the contained file
sizes and the newest contained mtime from a bounded walk (listings only), and
their id is the directory's own get_entry id. Reading one for extraction
builds an in-memory zip of just the members the extractor needs. A path
inside a package (from a hint) stands for the package.

Indexing then drains the queue newest-mtime first. Reading a queued file and
finding the same sha256 as last time (a touch) only refreshes the ledger;
otherwise text is extracted, chunked, and embedded through the cache, so an edit
re-embeds only the chunks whose text changed.
"""
from __future__ import annotations

import hashlib
import io
import logging
import zipfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .chunker import chunk_text
from .config import Config
from .embedder import Embedder, text_hash
from .extract import ext_of, extract, is_supported
from .iwork import is_iwork_name, package_member_wanted
from .source import FilespaceSource
from .store import Store, now_iso

logger = logging.getLogger("indexer.scanner")

# Same list as the web app's walkFilespace (src/features/docs/lib/filespace.ts);
# any name starting with "." is skipped as well.
SKIP_DIRS = {"node_modules", ".git", ".cache", ".trash", ".tmp", "$RECYCLE.BIN",
             ".lucidlink", ".DS_Store", "System Volume Information"}


# Bounds on walking inside one iWork package (metadata walk and member read).
PACKAGE_MAX_ENTRIES = 5000
PACKAGE_MAX_DEPTH = 8


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


def is_package(name: str) -> bool:
    """A directory with this name is an iWork package: indexed as one file."""
    return is_iwork_name(name) and not skip_dir(name)


def package_id(path: str, entry_id: str) -> str:
    """A package's ledger id: the folder's own id, or a path-based one when the
    source gives folders no id. Without the fallback, an id-less package would
    mark every scan incomplete and silently stop deletions for the filespace."""
    return entry_id or "pkg:" + hashlib.sha1(path.encode("utf-8")).hexdigest()


def package_root(path: str) -> Optional[str]:
    """The package containing `path` (strictly inside it), or None."""
    parts = [p for p in (path or "").split("/") if p]
    for i, part in enumerate(parts[:-1]):
        if is_package(part):
            return "/" + "/".join(parts[:i + 1])
    return None


class PackageTooLarge(Exception):
    """The members needed to extract a package exceed INDEX_MAX_FILE_BYTES."""


def _zip_members(members: list[tuple[str, bytes]]) -> bytes:
    """A deterministic zip (fixed timestamps, stored), so an untouched package
    always hashes the same."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for rel, data in members:
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
    return buf.getvalue()


def _hintable(path: Optional[str]) -> bool:
    """Absolute, no '.'/'..' components: anything else is not a path we index."""
    if not path or not path.startswith("/"):
        return False
    return not any(p in (".", "..") for p in path.split("/"))


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
    failed_files: list[str] = field(default_factory=list)
    stats: int = 0          # get_entry calls made

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

    def observe(self, path: str, size: int, mtime: int, file_id: str, scan_id: int, res: ScanResult,
                list_size: Optional[int] = None, list_mtime: Optional[int] = None) -> None:
        """Reconcile one file (authoritative get_entry values) with the ledger.

        `list_size` / `list_mtime` are the read_dir values for the fast path;
        None (hints, which only have get_entry) stores "unknown" so the next
        scan does one get_entry for this path and fills them in.
        """
        if not file_id:
            # Without a stable id we cannot tell new from renamed from deleted.
            logger.warning("[%s/%s] no file_id for %s; scan marked incomplete", self.ws, self.fs, path)
            res.failed_files.append(path)
            res.complete = False
            return
        name = path.rsplit("/", 1)[-1]
        supported = is_supported(name)
        unsupported_reason = f"unsupported type (.{ext_of(name)})" if ext_of(name) else "unsupported type"
        row = self.store.get_file(self.ws, self.fs, file_id)
        res.seen += 1
        if row is None:
            self.store.insert_file(self.ws, self.fs, file_id, path, size, mtime, scan_id,
                                   status="pending" if supported else "skipped",
                                   reason=None if supported else unsupported_reason,
                                   list_size=list_size, list_mtime=list_mtime)
            res.new += 1
            return

        cols: dict = {"scan_id": scan_id, "list_size": list_size, "list_mtime": list_mtime}
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
        if root != "/" and (hidden_path(root + "/x") or package_root(root + "/x")):
            return res   # hidden, or a package (handled as a file, never walked)
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
                    if is_package(e.name):
                        self._listed_package(path, scan_id, res)
                    elif not skip_dir(e.name):
                        queue.append(path)
                elif e.type == "file" and not skip_file(e.name):
                    self._listed_file(path, e.size, e.mtime, scan_id, res)
        if prune and res.complete:
            res.deleted = self._prune(root, scan_id)
        if root == "/":
            self.store.update_filespace(self.ws, self.fs, last_scan_at=now_iso())
        return res

    def _package_meta(self, path: str) -> tuple[int, int]:
        """(sum of contained file sizes, newest contained mtime) of a package,
        from listings only; bounded by PACKAGE_MAX_ENTRIES / _DEPTH, walked in
        name order so a truncated walk is still deterministic. Listing errors
        propagate."""
        size = mtime = entries = 0
        queue = deque([(path, 0)])
        while queue:
            d, depth = queue.popleft()
            for e in sorted(self.source.list_dir(d), key=lambda e: e.name):
                entries += 1
                if entries > PACKAGE_MAX_ENTRIES:
                    return size, mtime
                if e.type == "dir" and not skip_dir(e.name):
                    mtime = max(mtime, e.mtime)
                    if depth + 1 < PACKAGE_MAX_DEPTH:
                        queue.append((join(d, e.name), depth + 1))
                elif e.type == "file" and not skip_file(e.name):
                    size += e.size
                    mtime = max(mtime, e.mtime)
        return size, mtime

    def _listed_package(self, path: str, scan_id: int, res: ScanResult) -> None:
        try:
            size, mtime = self._package_meta(path)
        except Exception as e:
            # Unknown contents: like a directory that failed to list, nothing
            # may be deleted on the strength of this scan.
            logger.warning("[%s/%s] listing package %s failed: %s", self.ws, self.fs, path, e)
            res.failed_dirs.append(path)
            res.complete = False
            return
        self._listed_file(path, size, mtime, scan_id, res, package=True)

    def _listed_file(self, path: str, list_size: int, list_mtime: int, scan_id: int, res: ScanResult,
                     package: bool = False) -> None:
        """One file from read_dir (or a package from its metadata walk): fast
        path by path, else get_entry + observe."""
        for row in self.store.live_files_at_path(self.ws, self.fs, path):
            if row["list_size"] == list_size and row["list_mtime"] == list_mtime:
                res.seen += 1
                cols: dict = {"scan_id": scan_id}
                if row["status"] == "failed" and self._retry_due(row):
                    cols["status"] = "pending"
                self.store.update_file(self.ws, self.fs, row["file_id"], **cols)
                return
        res.stats += 1
        try:
            entry = self.source.stat(path)
        except Exception as e:
            logger.warning("[%s/%s] get_entry %s failed: %s", self.ws, self.fs, path, e)
            res.failed_files.append(path)
            res.complete = False
            return
        if entry is None or entry.type != ("dir" if package else "file"):
            return  # vanished (or changed kind) since the listing; next scan settles it
        if package:
            # A package's (size, mtime) only exist as walk values; get_entry
            # supplies its id. Packages use the same walk everywhere, so units agree.
            self.observe(path, list_size, list_mtime, package_id(path, entry.file_id), scan_id, res,
                         list_size=list_size, list_mtime=list_mtime)
            return
        self.observe(path, entry.size, entry.mtime, entry.file_id, scan_id, res,
                     list_size=list_size, list_mtime=list_mtime)

    def _tombstone_under(self, path: str) -> int:
        """Tombstone ledger rows at `path` or below it (a confirmed deletion)."""
        prefix = path.rstrip("/") + "/"
        rows = self.store.query(
            "SELECT * FROM files WHERE workspace_id=? AND filespace=? AND status != 'deleted' "
            "AND (path = ? OR substr(path, 1, ?) = ?)", (self.ws, self.fs, path, len(prefix), prefix))
        for row in rows:
            self._tombstone(row)
        return len(rows)

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
        """Apply a file-service change hint with targeted calls only.

        Hints are advisory and never escalate to a full scan: a path that does
        not exist (or is hidden, or malformed) is ignored, and anything missed
        is caught by the next periodic scan. Returns False when a targeted
        listing was incomplete.
        """
        if not _hintable(path) or (src is not None and not _hintable(src)):
            return True
        path = normpath(path)
        if op not in ("write", "mkdir", "delete", "move"):
            raise ValueError(f"unknown hint op '{op}'")
        # A path inside an iWork package stands for the package (one file).
        pkg = package_root(path)
        if op == "move":
            s = normpath(src) if src else None
            spkg = package_root(s) if s else None
            ok = self._hint_settle(pkg) if pkg else self._hint_present(path)
            if spkg:
                ok = (spkg == pkg or self._hint_settle(spkg)) and ok
            elif s:
                ok = self._hint_absent(s) and ok
            return ok
        if pkg:
            return self._hint_settle(pkg)
        if op in ("write", "mkdir"):
            return self._hint_present(path)
        if op == "delete":
            return self._hint_absent(path)
        if op == "move":
            # Destination first, so a moved file is re-pathed by file_id
            # (keeping its vectors) before the source side is settled.
            ok = self._hint_present(path)
            if src:
                ok = self._hint_absent(normpath(src)) and ok
            return ok
        raise AssertionError(op)   # validated above

    def _hint_settle(self, path: str) -> bool:
        """Present or absent, whichever `path` now is (a package touched from inside)."""
        if path == "/" or hidden_path(path):
            return True
        return self._hint_present(path) if self.source.stat(path) is not None else self._hint_absent(path)

    def _hint_present(self, path: str) -> bool:
        if path == "/" or hidden_path(path):
            return True
        entry = self.source.stat(path)
        if entry is None:
            return True
        package = entry.type == "dir" and is_package(path.rsplit("/", 1)[-1])
        if entry.type == "dir" and not package:
            return self.scan(path).complete
        if entry.type not in ("file", "dir"):
            return True
        res = ScanResult(scan_id=self.store.next_scan_id(self.ws, self.fs), root=path)
        if package:
            try:
                size, mtime = self._package_meta(path)
            except Exception as e:
                logger.warning("[%s/%s] listing package %s failed: %s", self.ws, self.fs, path, e)
                return False
            fid = package_id(path, entry.file_id)
            self.observe(path, size, mtime, fid, res.scan_id, res, list_size=size, list_mtime=mtime)
        else:
            fid = entry.file_id
            self.observe(path, entry.size, entry.mtime, fid, res.scan_id, res)
        if fid:
            # A write that replaced the file (new file_id) leaves the old row behind.
            for row in self.store.live_files_at_path(self.ws, self.fs, path):
                if row["file_id"] != fid:
                    self._tombstone(row)
        return res.complete

    def _hint_absent(self, path: str) -> bool:
        """A delete (or move source): tombstone the subtree once confirmed gone."""
        if path == "/" or self.source.stat(path) is not None:
            return True
        self._tombstone_under(path)
        return True

    # ------------------------------------------------------------ index
    def index_pending(self, progress: Optional[ProgressFn] = None,
                      should_stop: Optional[Callable[[], bool]] = None,
                      limit: Optional[int] = None) -> IndexResult:
        """Work through the queue, newest first, up to `limit` files or until
        `should_stop`. EmbedError propagates (the embedder is down: stop, leave
        files pending); per-file problems are recorded."""
        out = IndexResult()
        queue = self.store.pending_files(self.ws, self.fs, limit)
        # Progress is against the whole backlog, not just this job's slice.
        if not queue:
            total = 0
        elif limit is None:
            total = len(queue)
        else:
            total = self.store.file_counts(self.ws, self.fs)["pending"]
        done = 0
        for row in queue:
            if should_stop and should_stop():
                break
            if progress:
                progress(done, total, row["path"])
            outcome = self._index_file(row, out)
            setattr(out, outcome, getattr(out, outcome) + 1)
            done += 1
        if progress:
            progress(done, total, None)
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
        package = False
        try:
            if is_iwork_name(name):
                entry = self.source.stat(path)
                package = entry is not None and entry.type == "dir"
            if not package and row["size"] > limit:
                return self._skip(row, f"too large ({row['size']} bytes > {limit})")
            data = self._read_package(path, limit) if package else self.source.read_file(path)
        except PackageTooLarge as e:
            return self._skip(row, str(e))
        except Exception as e:
            self._set(row, status="failed", reason=f"read failed: {type(e).__name__}: {e}"[:300],
                      indexed_at=now_iso())
            return "failed"
        if not package and len(data) > limit:
            return self._skip(row, f"too large ({len(data)} bytes > {limit})")
        # A package's ledger size is its walk total, not the size of the zip built here.
        sized = {} if package else {"size": len(data)}

        digest = hashlib.sha256(data).hexdigest()
        if row["content_hash"] and digest == row["content_hash"]:
            # Touched, not changed: the previous outcome still holds.
            self._set(row, status="skipped" if row["reason"] else "indexed", **sized)
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
                  indexed_at=now_iso(), **sized)
        return "indexed"

    def _read_package(self, path: str, limit: int) -> bytes:
        """The members of package `path` the extractor needs (Index/**,
        index.xml/apxl(.gz), Index.zip, QuickLook/Preview.pdf), as a zip.
        Nothing else (images, previews, Data/) is listed deeply or read."""
        found: list[tuple[str, int]] = []
        entries = 0
        queue = deque([("", 0)])
        while queue and entries <= PACKAGE_MAX_ENTRIES:
            rel_dir, depth = queue.popleft()
            for e in sorted(self.source.list_dir(join(path, rel_dir) if rel_dir else path), key=lambda e: e.name):
                entries += 1
                if entries > PACKAGE_MAX_ENTRIES:
                    break
                rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
                if e.type == "dir":
                    if (not skip_dir(e.name) and depth + 1 < PACKAGE_MAX_DEPTH
                            and (rel in ("Index", "QuickLook") or rel.startswith("Index/"))):
                        queue.append((rel, depth + 1))
                elif e.type == "file" and not skip_file(e.name) and package_member_wanted(rel):
                    found.append((rel, e.size))
        preview = [m for m in found if m[0] == "QuickLook/Preview.pdf"]
        wanted = [m for m in found if m[0] != "QuickLook/Preview.pdf"]
        total = sum(size for _, size in wanted)
        if total > limit:
            raise PackageTooLarge(f"too large ({total} bytes of iWork index > {limit})")
        if preview and total + preview[0][1] <= limit:
            wanted += preview        # only a fallback, so only when it fits
        members: list[tuple[str, bytes]] = []
        read = 0
        for rel, _ in wanted:
            data = self.source.read_file(join(path, rel))
            read += len(data)
            if read > limit:
                raise PackageTooLarge(f"too large ({read} bytes of iWork index > {limit})")
            members.append((rel, data))
        return _zip_members(members)

    def backfill_embeddings(self, batch: int = 256) -> int:
        """Embed chunks that have no vector for the current model (model change)."""
        total = 0
        while True:
            rows = self.store.chunks_missing_embeddings(self.embedder.model, batch)
            if not rows:
                return total
            self.embedder.embed_texts([r["text"] for r in rows])
            total += len(rows)
