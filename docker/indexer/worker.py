"""Background scheduler: when each filespace is scanned and indexed.

One control thread wakes every `tick_sec` (and immediately on a hint or a sync
request), re-reads the vault's filespace list now and then, and dispatches
jobs onto a small pool. Invariants:

* At most one job per filespace at any time — a filespace is "running" from
  dispatch until its job returns, and nothing is dispatched for it meanwhile.
  Requests that arrive during a job are merged into its next run.
* At most `concurrency` jobs overall. Each job pins a linked filespace and its
  ~1 GB SDK cache, so the pool is sized to the link bound rather than giving
  every filespace its own thread.
* A job never takes the loop down: every exception is recorded as the
  filespace's lastError and the next scan is scheduled as usual.

A job: apply queued hints (targeted listings), run a full delta scan when one
is due or requested (or a hint could not be applied with certainty), drain the
pending queue, backfill vectors for a changed model, sync the Ruvector mirror,
and regenerate the stored survey when it is stale and allowed.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from .embedder import EmbedError
from .scanner import FilespaceIndexer
from .source import SourceError, configured_filespaces
from .store import now_iso

if TYPE_CHECKING:
    from .service import IndexerService

logger = logging.getLogger("indexer.worker")

Key = tuple[str, str]

# Queued hints per filespace beyond this collapse into one full-scan request,
# so a burst (or a flood of forged hints) costs one scan and bounded memory.
MAX_QUEUED_HINTS = 50


@dataclass
class Runtime:
    """In-memory state of one filespace (the durable part lives in the store)."""
    running: bool = False
    phase: Optional[str] = None            # "scanning" | "indexing" while running
    full_requested: bool = False
    hints: list[dict] = field(default_factory=list)
    next_scan_at: float = 0.0              # epoch seconds; 0 = scan as soon as possible
    progress: dict = field(default_factory=lambda: {"done": 0, "total": 0, "current": None})
    survey_error: Optional[str] = None
    jobs: int = 0                          # completed jobs, for tests and diagnostics
    more_pending: bool = False             # last job stopped with files still queued
    last_run_end: float = 0.0              # monotonic; least recently run goes first


def iso(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Scheduler:
    def __init__(self, svc: "IndexerService"):
        self.svc = svc
        self.cfg = svc.config
        self._cv = threading.Condition()
        self._runtime: dict[Key, Runtime] = {}
        self._targets: list[Key] = []
        self._targets_at: Optional[float] = None
        self.settings_error: Optional[str] = None
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pool: Optional[ThreadPoolExecutor] = None

    # ------------------------------------------------------------ targets
    def runtime(self, ws: str, fs: str, create: bool = False) -> Runtime:
        """Runtime for a filespace. Reads of unknown keys get a throwaway
        default, so status/search for arbitrary names never grow `_runtime`."""
        with self._cv:
            if create:
                return self._runtime.setdefault((ws, fs), Runtime())
            return self._runtime.get((ws, fs)) or Runtime()

    def is_target(self, ws: str, fs: str) -> bool:
        return (ws, fs) in self.targets()

    def refresh_targets(self, force: bool = False) -> list[Key]:
        """(workspace, filespace) pairs from the vault plus explicit overrides."""
        now = time.monotonic()
        if not force and self._targets_at is not None and now - self._targets_at < self.cfg.targets_refresh_sec:
            return list(self._targets)
        try:
            workspaces = self.svc.settings.workspaces()
            self.settings_error = None
        except Exception as e:  # vault.VaultError: no/invalid VAULT_KEY, corrupt vault
            self.settings_error = f"{type(e).__name__}: {e}"
            logger.warning("cannot read vault settings: %s", self.settings_error)
            self._targets_at = now
            return list(self._targets)
        keys: list[Key] = []
        ws_ids = set()
        for w in workspaces:
            wid = w.get("id")
            if not wid:
                continue
            ws_ids.add(wid)
            for name in configured_filespaces(w):
                keys.append((wid, name))
        # Filespaces enabled by hand via PUT /index/filespaces, in known workspaces.
        for row in self.svc.store.filespace_rows():
            key = (row["workspace_id"], row["filespace"])
            if row["enabled_override"] and row["enabled"] and row["workspace_id"] in ws_ids and key not in keys:
                keys.append(key)
        for ws, fs in keys:
            self.svc.store.ensure_filespace(ws, fs)
        with self._cv:
            self._targets = keys
            self._targets_at = now
        return list(keys)

    def targets(self) -> list[Key]:
        return self.refresh_targets()

    def enabled(self, ws: str, fs: str) -> bool:
        row = self.svc.store.filespace_row(ws, fs)
        return bool(row is None or row["enabled"])

    # ------------------------------------------------------------ requests
    def request_sync(self, keys: list[Key]) -> list[Key]:
        queued = []
        targets = set(self.targets())
        with self._cv:
            for key in keys:
                if key not in targets or not self.enabled(*key):
                    continue
                self._runtime.setdefault(key, Runtime()).full_requested = True
                queued.append(key)
            self._cv.notify_all()
        return queued

    def add_hint(self, ws: str, fs: str, hint: dict) -> bool:
        """Queue a hint for a known, enabled filespace; anything else is dropped."""
        if not self.is_target(ws, fs) or not self.enabled(ws, fs):
            return False
        with self._cv:
            rt = self._runtime.setdefault((ws, fs), Runtime())
            if rt.full_requested:
                pass  # a full scan is already coming; it covers this hint
            elif len(rt.hints) >= MAX_QUEUED_HINTS:
                rt.hints = []
                rt.full_requested = True
            else:
                rt.hints.append(hint)
            self._cv.notify_all()
        return True

    # ------------------------------------------------------------ loop
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping.clear()
        self._pool = ThreadPoolExecutor(max_workers=self.cfg.concurrency, thread_name_prefix="indexer-job")
        self._thread = threading.Thread(target=self._loop, name="indexer-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        self._stopping.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self._dispatch()
            except Exception:
                logger.exception("scheduler dispatch failed")
            with self._cv:
                if not self._stopping.is_set():
                    self._cv.wait(self.cfg.tick_sec)

    def _dispatch(self) -> None:
        keys = self.refresh_targets()
        now = time.time()
        with self._cv:
            running = sum(1 for rt in self._runtime.values() if rt.running)
            # Round-robin: whichever filespace ran least recently is offered a
            # slot first, so time-sliced big jobs take turns with small ones.
            keys = sorted(keys, key=lambda k: self._runtime[k].last_run_end if k in self._runtime else 0.0)
            for key in keys:
                if running >= self.cfg.concurrency or self._stopping.is_set():
                    break
                rt = self._runtime.setdefault(key, Runtime())
                if rt.running or not self.enabled(*key):
                    continue
                if rt.full_requested or rt.hints or rt.more_pending or now >= rt.next_scan_at:
                    rt.running = True
                    running += 1
                    assert self._pool is not None
                    self._pool.submit(self._job_safe, key)

    def _job_safe(self, key: Key) -> None:
        try:
            self.run_job(*key)
        except Exception:  # run_job records its own errors; this is belt and braces
            logger.exception("job crashed for %s", key)
        finally:
            with self._cv:
                self._runtime[key].running = False
                self._cv.notify_all()

    # ------------------------------------------------------------ job
    def run_job(self, ws: str, fs: str, full: Optional[bool] = None) -> None:
        """One unit of work for a filespace. Callable synchronously (tests)."""
        rt = self.runtime(ws, fs, create=True)
        with self._cv:
            hints, rt.hints = rt.hints, []
            if full is None:
                full = rt.full_requested or time.time() >= rt.next_scan_at
            rt.full_requested = False
        store = self.svc.store
        error: Optional[str] = None
        scan_took: Optional[float] = None
        try:
            with self.svc.sources.lease(ws, fs) as source:
                idx = FilespaceIndexer(store, source, ws, fs, self.svc.embedder, self.cfg)
                rt.phase = "scanning"
                # Hints are advisory: a failed or incomplete one is logged and
                # left to the next periodic scan, never escalated to a full scan
                # (a hint names a path, possibly a bogus one).
                for h in hints:
                    try:
                        if not idx.hint(h["op"], h["path"], h.get("src")):
                            logger.info("[%s/%s] hint %s incomplete; next scan will settle it", ws, fs, h)
                    except Exception as e:
                        logger.warning("[%s/%s] hint %s failed: %s", ws, fs, h, e)
                if full:
                    scan_started = time.monotonic()
                    res = idx.scan("/")
                    scan_took = time.monotonic() - scan_started
                    logger.info("[%s/%s] scan %d: seen=%d new=%d changed=%d renamed=%d deleted=%d "
                                "get_entry=%d rekeyed=%d%s", ws, fs, res.scan_id, res.seen, res.new, res.changed,
                                res.renamed, res.deleted, res.stats, res.rekeyed,
                                "" if res.complete else f" INCOMPLETE {(res.failed_dirs + res.failed_files)[:5]}")
                    if not res.complete:
                        failed = res.failed_dirs + res.failed_files
                        error = (f"listing failed for {len(res.failed_dirs)} folder(s) and "
                                 f"{len(res.failed_files)} file(s), deletions deferred: {', '.join(failed[:3])}")
                if self.svc.embedder.configured:
                    rt.phase = "indexing"
                    deadline = time.monotonic() + self.cfg.job_budget_sec
                    idx.index_pending(progress=lambda d, t, c: rt.progress.update(done=d, total=t, current=c),
                                      should_stop=lambda: self._stopping.is_set() or time.monotonic() >= deadline,
                                      limit=self.cfg.job_max_files)
                    rt.more_pending = bool(store.pending_files(ws, fs, 1))
                    idx.backfill_embeddings()
                    self.svc.vectors.sync_mirror()
                elif store.pending_files(ws, fs):
                    error = error or "embedder not configured (EMBED_BASE_URL / EMBED_MODEL); files left pending"
            store.update_filespace(ws, fs, last_sync_at=now_iso())
        except EmbedError as e:
            error = f"embedding failed: {e}"
        except SourceError as e:   # no token, bad token, filespace gone: expected, no traceback
            logger.warning("[%s/%s] %s", ws, fs, e)
            error = f"SourceError: {e}"
        except Exception as e:
            logger.exception("[%s/%s] job failed", ws, fs)
            error = f"{type(e).__name__}: {e}"
        finally:
            rt.phase = None
            rt.progress = {"done": 0, "total": 0, "current": None}
            if error is not None:
                # Don't spin on a broken embedder or source: the backlog waits
                # for the next scheduled scan or a manual sync.
                rt.more_pending = False
            if full:
                interval = self.cfg.sync_interval_sec
                if scan_took is not None:
                    interval = max(interval, self.cfg.scan_interval_factor * scan_took)
                rt.next_scan_at = time.time() + interval
            store.update_filespace(ws, fs, last_error=error)
            rt.jobs += 1
            rt.last_run_end = time.monotonic()
        if error is None:
            self._maybe_survey(ws, fs, rt)

    def _maybe_survey(self, ws: str, fs: str, rt: Runtime) -> None:
        """Regenerate the root survey when stale (or missing), rate-limited."""
        svc = self.svc
        if not (svc.llm.configured and svc.embedder.configured) or not svc.store.chunk_count(ws, fs):
            return
        row = svc.store.get_survey(ws, fs, "/")
        if row is not None:
            if not row["stale"]:
                return
            try:
                age = time.time() - datetime.fromisoformat(row["generated_at"].replace("Z", "+00:00")).timestamp()
            except ValueError:
                age = float("inf")
            if age < self.cfg.survey_min_interval_sec:
                return
        from . import survey
        try:
            survey.generate(svc, ws, fs, "/")
            rt.survey_error = None
        except Exception as e:
            rt.survey_error = f"{type(e).__name__}: {e}"
            logger.warning("[%s/%s] survey failed: %s", ws, fs, rt.survey_error)

    # ------------------------------------------------------------ status
    def state(self, ws: str, fs: str) -> str:
        row = self.svc.store.filespace_row(ws, fs)
        if row is not None and not row["enabled"]:
            return "disabled"
        rt = self.runtime(ws, fs)
        if rt.running and rt.phase:
            return rt.phase
        if rt.running:
            return "scanning"
        return "error" if row is not None and row["last_error"] else "idle"
