"""The indexer's composition root and the operations behind each HTTP route.

`IndexerService` wires store, settings, sources, embedder, vectors, LLM and
scheduler together. Every dependency can be injected, which is how the tests
run the whole thing against an in-memory filespace, a fake embedder and
`httpx.MockTransport` for Ruvector and the LLM. app.py stays a thin HTTP layer.
"""
from __future__ import annotations

import json
import logging
import os
import stat as stat_mod
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import httpx

from . import ask as ask_mod
from . import folders as folders_mod
from . import search as search_mod
from . import survey as survey_mod
from .block import KIND_LUCIDLINK, ONLINE_ONLY, TYPE_EXT, TYPES, LocalSource, PathRefused, RoutingSourceProvider, mounted, type_of
from .block import NotMounted as NotMountedError
from .config import Config
from .embedder import Embedder
from .llm import LLM, LLMUnavailable
from .source import SdkSourceProvider, SettingsProvider, SourceProvider, VaultSettings
from .store import FILE_ORDER, Store, fold_name, now_iso
from . import discover as discover_mod
from . import related as related_mod
from .vectors import FileIndex, LocalIndex, RuvectorMirror, Scope, VectorService
from .worker import Scheduler, iso

logger = logging.getLogger("indexer.service")

SNIPPET_CHARS = 300


RECENT_MAX = 200
RECENT_CHANGES = ("new", "modified", "renamed", "deleted")
BYKIND_MAX = 500
# GET /index/bykind: the app's Kind filter onto the profile's type classes.
KIND_CLASSES = search_mod.KIND_CLASSES
FILES_MAX = 200

DAY = 86400.0
# GET /index/profile age buckets: (key, max age in days), exclusive, oldest last.
AGE_BUCKETS = (("7d", 7), ("30d", 30), ("90d", 90), ("older", None))
LARGEST_FOLDERS = 10
# Related files: seconds between passes of the file-vector builder.
FILE_VECTOR_INTERVAL = 60.0
# Discovered collections: seconds between checks for a run to do, groups named
# per check, and how long to leave the AI alone after it failed.
DISCOVER_INTERVAL = 60.0
DISCOVER_NAMES_PER_PASS = 20
DISCOVER_LLM_BACKOFF = 600.0
DISCOVER_LIMIT_MAX = 200


def mtime_seconds(v: Optional[int]) -> Optional[float]:
    """SDK mtimes are Unix timestamps of unspecified unit (s, ms, us or ns); normalise to seconds."""
    if not v:
        return None
    x = float(v)
    for limit, div in ((1e17, 1e9), (1e14, 1e6), (1e11, 1e3)):
        if x > limit:
            return x / div
    return x


def mtime_iso(v: Optional[int]) -> Optional[str]:
    """SDK mtime → ISO 8601 UTC."""
    x = mtime_seconds(v)
    if x is None:
        return None
    try:
        return datetime.fromtimestamp(x, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _iso_seconds(v: Optional[str]) -> Optional[float]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _iso_micros(v: str, name: str) -> int:
    """ISO 8601 → integer Unix microseconds (no offset = UTC); ValueError names the parameter."""
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} is not an ISO 8601 timestamp: {v!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _EPOCH) // timedelta(microseconds=1)


def _micros_iso(us: int) -> Optional[str]:
    """Integer Unix microseconds → ISO 8601 UTC, fraction only when there is one (round-trips)."""
    try:
        return (_EPOCH + timedelta(microseconds=int(us))).isoformat().replace("+00:00", "Z")
    except OverflowError:
        return None


class NotFound(Exception):
    pass


LIST_MAX = 2000
FIND_MAX = 100
FIND_CANDIDATES = 400   # per scope, before ranking

# lastError -> what the app can say and do about it (docs: Why something is wrong).
_PROBLEMS = (   # first match wins: a network failure reported as "authentication failed" is a network failure
    ("offline", ("not mounted",)),
    ("too_big", ("413", "payload too large", "too large")),
    ("engine", ("embedder", "embedding", "llm ", "llm:", "ruvector", "vector store")),
    ("unreachable", ("host not found", "connection refused", "connection reset", "timed out", "timeout",
                     "network is unreachable", "name or service not known", "temporary failure in name")),
    ("sign_in", ("authentication failed", "lease expired", "unauthorized", "forbidden", "invalid token",
                 "token expired", " 401", " 403")),
)


def problem_of(error: Optional[str]) -> Optional[dict]:
    if not error:
        return None
    low = " " + error.lower()
    for kind, needles in _PROBLEMS:
        if any(n in low for n in needles):
            return {"kind": kind, "detail": error}
    return {"kind": "other", "detail": error}
# Roots whose new top folders are offered: mounted shares and drives, and cloud sync apps.
PROMPT_ROOTS = ("volumes", "cloud")
ALL_WORKSPACES = "*"


def _age_sec(iso_ts: str) -> float:
    try:
        return time.time() - datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class IndexerService:
    def __init__(self, config: Config, store: Optional[Store] = None,
                 settings: Optional[SettingsProvider] = None,
                 sources: Optional[SourceProvider] = None,
                 embedder: Optional[Embedder] = None,
                 llm: Optional[LLM] = None,
                 ruvector_transport: Optional[httpx.BaseTransport] = None):
        self.config = config
        self.store = store or Store(config.db_path)
        self.settings = settings or VaultSettings()
        self.sources = sources or SdkSourceProvider(self.settings, config.max_clients,
                                                    config.max_linked_filespaces)
        self.embedder = embedder or Embedder(config.embed_base_url, config.embed_model, config.embed_api_key,
                                             store=self.store, batch=config.embed_batch,
                                             timeout=config.embed_timeout)
        if self.embedder.store is None:
            self.embedder.store = self.store
        self.llm = llm or LLM(config.llm_base_url, config.llm_model, config.llm_api_key, config.llm_timeout)
        mirror = None
        if config.ruvector_url:
            mirror = RuvectorMirror(config.ruvector_url, config.ruvector_api_key, self.store,
                                    self.embedder.model, lambda: self.embedder.dim,
                                    transport=ruvector_transport)
        self.vectors = VectorService(self.store, LocalIndex(self.store, self.embedder.model,
                                                            config.local_index_max_points), mirror)
        self.file_index = FileIndex(self.store, self.embedder.model)
        # True once a pass of the file-vector builder found nothing left to do.
        self.file_vectors_ready = False
        # Discovered collections: the run in progress {"key", "phase", "done", "total"}.
        self.discover_running: Optional[dict] = None
        self._dc_wake = threading.Event()
        self._dc_llm_retry_at = 0.0
        self.scheduler = Scheduler(self)
        # Block keys get a LocalSource; everything else the filespace provider
        # (looked up per lease, so `self.sources` can be swapped in tests).
        self.router = RoutingSourceProvider(lambda ws, fs: self.scheduler.block_root(ws, fs),
                                            lambda: self.sources,
                                            lambda ws, fs: self.scheduler.skip_online_only(ws, fs))

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.scheduler.start()
        self._start_fts_backfill()
        self._start_file_vectors()
        self._start_discovery()

    def build_file_vectors(self, batch: int = 200, pause: float = 0.0,
                           stop: Optional[threading.Event] = None) -> int:
        """One pass of the file-vector builder (Related files): every indexed file
        without a file vector gets the mean of its chunk vectors, from the
        embeddings cache (nothing is read or embedded again). Returns files done."""
        model = self.embedder.model
        done = 0
        for r in self.store.filespace_rows():
            ws, fs = r["workspace_id"], r["filespace"]
            # Files with no chunk vector yet (embedding pending) stay in the todo
            # list; skip them for the rest of this pass.
            failed: set[str] = set()
            while not (stop is not None and stop.is_set()):
                todo = [fid for fid in self.store.file_vector_todo(ws, fs, model, batch + len(failed))
                        if fid not in failed][:batch]
                if not todo:
                    break
                means = self.store.file_mean_vectors(ws, fs, todo, model)
                failed |= {fid for fid in todo if fid not in means}
                items = [(ws, fs, fid, n, v) for fid, (v, n) in means.items()]
                self.store.put_file_vectors(model, items)
                done += len(items)
                if pause:
                    (stop.wait(pause) if stop is not None else time.sleep(pause))
        if not (stop is not None and stop.is_set()):
            self.file_vectors_ready = done == 0 or self.file_vectors_ready
        return done

    def _start_file_vectors(self) -> None:
        """Keep file vectors current in the background (low priority: short
        batches with pauses; a pass every FILE_VECTOR_INTERVAL seconds)."""
        self._fv_stop = threading.Event()

        def run():
            t0 = time.monotonic()
            first = True
            while not self._fv_stop.is_set():
                try:
                    n = self.build_file_vectors(pause=0.25, stop=self._fv_stop)
                    if first and not self._fv_stop.is_set():
                        logger.info("related files: %d file vectors built (%.0fs)", n, time.monotonic() - t0)
                        first = False
                    self.file_index.refresh()
                    self.store.chunk_total(ttl=240.0)     # keep the count off the request path
                except Exception:
                    logger.exception("related files: file vector pass failed")
                self._fv_stop.wait(FILE_VECTOR_INTERVAL)

        self._fv_thread = threading.Thread(target=run, name="indexer-file-vectors", daemon=True)
        self._fv_thread.start()

    def _start_discovery(self) -> None:
        """Discovered collections in the background, at low priority: a run for
        each location switched on (when asked, and again daily if its files
        changed), then names from the AI, a few per pass."""
        self._dc_stop = threading.Event()

        def run():
            try:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
            except (AttributeError, OSError):
                pass
            while not self._dc_stop.is_set():
                try:
                    self.discover_pass(stop=self._dc_stop, pause=1.0)
                except Exception:
                    logger.exception("discover: pass failed")
                self._dc_wake.wait(DISCOVER_INTERVAL)
                self._dc_wake.clear()

        self._dc_thread = threading.Thread(target=run, name="indexer-discover", daemon=True)
        self._dc_thread.start()

    def discover_pass(self, stop: Optional[threading.Event] = None, pause: float = 0.0) -> None:
        """Runs that are due, then up to DISCOVER_NAMES_PER_PASS AI names.
        Waits while indexing is paused or file vectors are still being built."""
        if self.store.control()["paused"] or not self.file_vectors_ready:
            return
        targets = set(self.scheduler.targets())
        for sc in self.store.discover_scopes():
            key = (sc["workspace_id"], sc["filespace"])
            if stop is not None and stop.is_set():
                return
            if sc["enabled"] and key in targets and self._discover_due(sc):
                self._discover_run(*key, stop=stop, pause=pause)
        if not self.llm.configured or time.monotonic() < self._dc_llm_retry_at:
            return
        rows = [r for r in self.store.discover_group_rows(statuses=["proposed"]) if r["named_by"] == "auto"]
        for r in rows[:DISCOVER_NAMES_PER_PASS]:
            if stop is not None and stop.is_set():
                return
            try:
                discover_mod.name_group(self, r)
            except LLMUnavailable as e:
                logger.warning("discover: AI unavailable, names wait %.0fs: %s", DISCOVER_LLM_BACKOFF, e)
                self._dc_llm_retry_at = time.monotonic() + DISCOVER_LLM_BACKOFF
                return
            except Exception as e:      # LLMError and the like: skip this group this pass
                logger.warning("discover: naming group %s failed: %s", r["id"], e)

    def _discover_due(self, sc: dict) -> bool:
        if sc["requested_at"]:
            return True
        if not sc["finished_at"] or sc["error"]:
            return False
        age = time.time() - datetime.fromisoformat(sc["finished_at"].replace("Z", "+00:00")).timestamp()
        return age >= discover_mod.RERUN_EVERY and self.store.fv_seq() != sc["fv_seq"]

    def _discover_run(self, ws: str, fs: str, stop: Optional[threading.Event] = None, pause: float = 0.0) -> None:
        seq = self.store.fv_seq()
        self.store.discover_mark(ws, fs, started_at=now_iso(), requested_at=None, error=None)
        self.discover_running = {"workspaceId": ws, "filespace": fs, "phase": "starting", "done": 0, "total": 0}

        def progress(phase, done, total):
            self.discover_running = {"workspaceId": ws, "filespace": fs, "phase": phase, "done": done,
                                     "total": total}

        try:
            stats = discover_mod.run(self, ws, fs, stop=stop, progress=progress, pause=pause)
            self.store.discover_mark(ws, fs, finished_at=now_iso(), fv_seq=seq, stats=stats)
        except discover_mod.Stopped:
            self.store.discover_mark(ws, fs, requested_at=now_iso())     # finish after a restart
        except Exception as e:
            logger.exception("discover [%s/%s]: run failed", ws, fs)
            self.store.discover_mark(ws, fs, finished_at=now_iso(), error=f"{type(e).__name__}: {e}")
        finally:
            self.discover_running = None

    def _start_fts_backfill(self) -> None:
        """Build the keyword index for chunks that predate it, in the background
        (short batches; search stays meaning-only until it is done)."""
        if self.store.fts_ready or not self.store.fts_available:
            return
        self._fts_stop = threading.Event()

        def run():
            t0 = time.monotonic()
            lo, hi = self.store.fts_hole()
            logger.info("keyword index: backfilling %d chunk ids in the background", hi - lo)
            try:
                if self.store.fts_backfill(pause=0.02, stop=self._fts_stop):
                    logger.info("keyword index ready (%.1fs)", time.monotonic() - t0)
            except Exception:
                logger.exception("keyword index backfill failed; search stays meaning-only")

        self._fts_thread = threading.Thread(target=run, name="indexer-fts-backfill", daemon=True)
        self._fts_thread.start()

    def stop(self) -> None:
        stop = getattr(self, "_fts_stop", None)
        if stop is not None:
            stop.set()
            self._fts_thread.join(10)
        stop = getattr(self, "_fv_stop", None)
        if stop is not None:
            stop.set()
            self._fv_thread.join(10)
        stop = getattr(self, "_dc_stop", None)
        if stop is not None:
            stop.set()
            self._dc_wake.set()
            self._dc_thread.join(10)
        self.scheduler.stop()
        self.sources.close()
        self.store.close()

    # ------------------------------------------------------------ helpers
    def llm_required(self) -> None:
        if not self.llm.configured:
            raise LLMUnavailable("LLM not configured. Set LLM_BASE_URL and LLM_MODEL.")

    def resolve_workspace(self, workspace_id: Optional[str]) -> Optional[str]:
        if workspace_id:
            return workspace_id
        try:
            active = self.settings.active_workspace_id()
        except Exception as e:  # vault unavailable; fall back to what we know
            logger.warning("cannot read active workspace: %s", e)
            active = None
        if active:
            return active
        targets = self.scheduler.targets()
        return targets[0][0] if targets else None

    def scopes(self, workspace_id: Optional[str], filespace: Optional[str]) -> set[Scope]:
        """Search scope: one filespace, or every enabled filespace of the workspace.

        workspaceId "*" means every workspace, the Mini's drives included ("All drives");
        with a filespace it is that name in whichever workspace has it.
        """
        if workspace_id == ALL_WORKSPACES:
            keys = [k for k in self.scheduler.targets() if self.scheduler.enabled(*k)]
            return {k for k in keys if not filespace or k[1] == filespace}
        ws = self.resolve_workspace(workspace_id)
        if ws is None:
            return set()
        if filespace:
            return {(ws, filespace)}
        return {k for k in self.scheduler.targets() if k[0] == ws and self.scheduler.enabled(*k)}

    def location(self, ws: str, fs: str, kind: Optional[str] = None) -> dict:
        """Where a source lives, for badges: kind, plus root and provider for block sources."""
        if kind is None:
            kind = self.kinds().get((ws, fs), KIND_LUCIDLINK)
        root = provider = None
        if kind != KIND_LUCIDLINK and ws == self.scheduler._block_workspace():
            spec = next((b for b in self.scheduler._block_specs if b.name == fs), None)
            if spec is not None:
                root, provider = spec.root, spec.provider
        return {"kind": kind, "root": root, "provider": provider}

    def locations(self) -> "dict[tuple[str, str], dict]":
        """location() for every known source, computed once per request."""
        kinds = self.kinds()
        return {key: self.location(key[0], key[1], k) for key, k in kinds.items()}

    def find(self, workspace_id: Optional[str], filespace: Optional[str], q: str, limit: int = 20) -> list[dict]:
        """Files whose name or path matches the query, ignoring case, spaces,
        hyphens, underscores, dots (so CamelCase too) (docs: Find by name).

        Rank: exact name (with or without extension) > name starts with > name
        contains > name has every word > only the path matches; then newest.
        """
        if len((q or "").strip()) < 2:
            return []
        compact = fold_name(q)
        words = [w for w in (fold_name(x) for x in q.split()) if w]
        if not compact or not words:
            return []
        limit = max(1, min(int(limit), FIND_MAX))
        scopes = sorted(self.scopes(workspace_id, filespace))
        locs = self.locations()
        hits: list[tuple[tuple, dict]] = []
        for r in self.store.find_files(scopes, compact, words, FIND_CANDIDATES):
            name = r["path"].rsplit("/", 1)[-1]
            stem = name.rsplit(".", 1)[0] if "." in name.lstrip(".") else name
            tier = r["tier"] + 1
            if r["nn"] == compact or fold_name(stem) == compact:
                tier = 0
            ts = mtime_seconds(r["mtime"]) or 0.0
            hits.append(((tier, -ts, r["path"]), {
                "workspaceId": r["workspace_id"], "filespace": r["filespace"],
                "kind": locs.get((r["workspace_id"], r["filespace"]), {}).get("kind", KIND_LUCIDLINK),
                "location": locs.get((r["workspace_id"], r["filespace"]),
                                     {"kind": KIND_LUCIDLINK, "root": None, "provider": None}),
                "path": r["path"], "name": name, "size": r["size"], "mtime": mtime_iso(r["mtime"]),
                "status": r["status"], "reason": r["reason"],
            }))
        hits.sort(key=lambda h: h[0])
        return [h[1] for h in hits[:limit]]

    def kinds(self) -> dict[tuple[str, str], str]:
        """(workspace, filespace) -> "lucidlink" | "block", from the ledger."""
        return self.store.kinds()

    # ------------------------------------------------------------ status
    def filespace_status(self, ws: str, fs: str, ctl: Optional[dict] = None,
                         totals: Optional[dict] = None) -> dict:
        """`totals` is store.live_totals() for every source, when the caller has it."""
        row = self.store.filespace_row(ws, fs) or {}
        if totals is None:
            totals = self.store.live_totals(ws, fs)
        rt = self.scheduler.runtime(ws, fs)
        enabled = bool(row.get("enabled", 1))
        ctl = ctl if ctl is not None else self.store.control()
        priority = next((i for i, p in enumerate(ctl["priority"])
                         if (p.get("workspaceId"), p.get("filespace")) == (ws, fs)), None)
        return {
            "workspaceId": ws,
            "filespace": fs,
            "kind": row.get("kind") or KIND_LUCIDLINK,
            "enabled": enabled,
            "state": self.scheduler.state(ws, fs, ctl["paused"]),
            "priority": priority,
            "lastScanAt": row.get("last_scan_at"),
            "lastSyncAt": row.get("last_sync_at"),
            "nextScanAt": (iso(rt.next_scan_at) or (iso(datetime.now(timezone.utc).timestamp())))
            if enabled else None,
            "files": self.store.file_counts(ws, fs),
            "chunks": self.store.chunk_count(ws, fs),
            "totals": dict(totals.get((ws, fs)) or {"files": 0, "bytes": 0}),
            # A refused block source with this name shows here too (the
            # filespace itself keeps working).
            "lastError": row.get("last_error") or self.scheduler.block_conflict(ws, fs),
            "problem": problem_of(row.get("last_error") or self.scheduler.block_conflict(ws, fs)),
            "location": self.location(ws, fs, row.get("kind")),
            "progress": dict(rt.progress),
            "surveyError": rt.survey_error,
        }

    def status(self) -> dict:
        ctl = self.store.control()
        totals = self.store.live_totals()   # one GROUP BY for every source
        return {
            "paused": ctl["paused"],
            "filespaces": [self.filespace_status(ws, fs, ctl, totals) for ws, fs in self.scheduler.targets()],
            "embedder": self.embedder.status(),
            "vectorStore": self.vectors.status(),
            "relatedFiles": {"fileVectors": self.file_index.count(), "ready": self.file_vectors_ready},
            "discovered": {"locations": sum(1 for sc in self.store.discover_scopes() if sc["enabled"]),
                           "running": dict(self.discover_running) if self.discover_running else None},
            "blockSources": list(self.scheduler.block_sources),
            "engine": self.engine_info(),
            "settings": {"ok": self.scheduler.settings_error is None, "error": self.scheduler.settings_error},
        }

    # ------------------------------------------------------------ folders from the app
    def folders(self) -> dict:
        self.scheduler.refresh_targets(force=True)
        return {"engine": self.engine_info(), "roots": folders_mod.roots(self.config),
                "sources": list(self.scheduler.block_sources)}

    def engine_info(self) -> dict:
        """The engine computer: its name (the label of its drives) and their workspace id."""
        return {"name": self.config.engine_name or None, "workspaceId": self.scheduler._block_workspace()}

    def _connected(self):
        """(root, folder, container path, host path) -> name of the source that is that folder."""
        specs = self.scheduler._block_specs
        by_app = {(b.root, b.folder): b.name for b in specs if b.origin == "app"}
        by_host = {b.host_path.rstrip("/"): b.name for b in specs if b.host_path}
        by_real = {}
        for b in specs:
            try:
                by_real[os.path.realpath(b.path)] = b.name
            except OSError:
                pass

        def find(root: str, folder: str, full: str, host: Optional[str]) -> Optional[str]:
            return (by_app.get((root, folder)) or (by_host.get(host.rstrip("/")) if host else None)
                    or by_real.get(os.path.realpath(full)))
        return find

    def browse_folders(self, root: str, path: str) -> dict:
        return folders_mod.browse(self.config, root, path, self._connected())

    def add_folder(self, root: str, path: str, name: Optional[str]) -> dict:
        folder, full = folders_mod.resolve(self.config, root, path)
        name = folders_mod.check_name(name if name and name.strip() else folders_mod.default_name(root, folder))
        specs = self.scheduler._block_specs
        if any(b.name == name for b in specs):
            raise folders_mod.FolderError(409, f"There's already a folder called {name}. Pick another name.")
        ws = self.scheduler._block_workspace()
        kinds = self.store.kinds()
        if any(k == (ws, name) and kinds.get(k, KIND_LUCIDLINK) == KIND_LUCIDLINK for k in self.scheduler.targets()):
            raise folders_mod.FolderError(409, f"{name} is the name of a LucidLink filespace. Pick another name.")
        host = folders_mod.host_path(self.config, root, folder)
        real = os.path.realpath(full)
        for b in specs:
            rel = folders_mod.overlap(host, b.host_path) if host and b.host_path else None
            if rel is None:
                try:
                    rel = folders_mod.overlap(real, os.path.realpath(b.path))
                except OSError:
                    rel = None
            if rel == "same":
                raise folders_mod.FolderError(409, f"That folder is already connected as {b.name}.")
            if rel == "inside":
                raise folders_mod.FolderError(409, f"That folder is inside {b.name}, which is already connected.")
            if rel == "around":
                raise folders_mod.FolderError(
                    409, f"{b.name} is inside that folder. Remove {b.name} first, or pick a folder inside it.")
        self.store.add_block_folder(name, root, folder, now_iso())
        self.scheduler.refresh_targets(force=True)
        self.scheduler.request_sync([(ws, name)])
        for entry in self.scheduler.block_sources:
            if entry["name"] == name:
                return entry
        raise folders_mod.FolderError(500, "The folder was saved but isn't listed; check the indexer log.")

    def _overlaps_a_source(self, root: str, folder: str, full: str, host: Optional[str]) -> bool:
        for b in self.scheduler._block_specs:
            if host and b.host_path and folders_mod.overlap(host, b.host_path):
                return True
            try:
                if folders_mod.overlap(os.path.realpath(full), os.path.realpath(b.path)):
                    return True
            except OSError:
                pass
        return False

    def folder_prompts(self) -> dict:
        """New drives and cloud accounts, and connected folders that have gone missing (docs: Drive prompts)."""
        now = now_iso()
        new: list[dict] = []
        for root in PROMPT_ROOTS:
            if not folders_mod.root_available(self.config, root):
                continue
            listing = folders_mod.browse(self.config, root, "/", lambda *_: None)
            names = [e["name"] for e in listing["entries"]]
            seen = self.store.drives_seen(root)
            if not seen:
                # The first look: what is already there is known, not new ("" marks the look itself).
                self.store.add_drives_seen(root, names + [""], now, baseline=True)
                continue
            self.store.add_drives_seen(root, [n for n in names if n not in seen], now, baseline=False)
            seen = self.store.drives_seen(root)
            base = folders_mod.root_path(self.config, root)
            for e in listing["entries"]:
                row = seen.get(e["name"])
                if not row or row["baseline"] or row["dismissed"]:
                    continue
                if self._overlaps_a_source(root, e["path"], base + e["path"], e["hostPath"]):
                    continue
                new.append({"root": root, "path": e["path"], "name": e["name"], "hostPath": e["hostPath"],
                            "provider": e.get("provider"),
                            "suggestedName": folders_mod.default_name(root, e["path"]),
                            "firstSeenAt": row["first_seen_at"]})
        missing: list[dict] = []
        grace = self.config.folder_missing_grace_sec
        for b in self.scheduler._block_specs:
            if b.error is not None:
                continue
            if mounted(b.path):
                self.store.clear_missing(b.name)
                continue
            row = self.store.mark_missing(b.name, now)
            if row["dismissed"] or _age_sec(row["since"]) < grace:
                continue
            missing.append({"name": b.name, "origin": b.origin, "hostPath": b.host_path, "root": b.root,
                            "missingSince": row["since"]})
        return {"newDrives": new, "missing": missing}

    def dismiss_prompt(self, kind: str, path: Optional[str], name: Optional[str], root: str = "volumes") -> dict:
        if kind == "new":
            drive = (path or "").strip("/")
            if root not in PROMPT_ROOTS or not drive or "/" in drive or not self.store.dismiss_drive(root, drive):
                raise NotFound(f"no new drive at {path!r}")
        else:
            if not name or not self.store.dismiss_missing(name):
                raise NotFound(f"no missing folder called {name!r}")
        return {"dismissed": True}

    def _block_source(self, name: str) -> "LocalSource":
        spec = next((b for b in self.scheduler._block_specs if b.name == name and b.error is None), None)
        if spec is None:
            raise NotFound(f"no connected folder called {name!r}")
        if not mounted(spec.path):
            raise folders_mod.FolderError(503, f"{name} can't be reached right now.")
        return LocalSource(spec.path)

    def list_folder(self, name: str, path: str) -> dict:
        """One folder of a connected block source, read-only (docs: Browsing and opening)."""
        src = self._block_source(name)
        folder = folders_mod.normalize_folder(path)
        try:
            entries = src.list_dir(folder)
        except PathRefused:
            raise folders_mod.FolderError(400, "That path is outside the folder, or goes through a link.") from None
        except (FileNotFoundError, NotADirectoryError):
            raise NotFound(f"{folder} isn't in {name}") from None
        except NotMountedError:
            raise folders_mod.FolderError(503, f"{name} can't be reached right now.") from None
        dirs = sorted((e for e in entries if e.type == "dir"), key=lambda e: e.name.casefold())
        files = sorted((e for e in entries if e.type == "file"), key=lambda e: e.name.casefold())
        ordered = (dirs + files)[:LIST_MAX]
        ws = self.scheduler._block_workspace()
        ids = [e.file_id for e in ordered if e.type == "file"]
        status: dict[str, tuple[str, Optional[str]]] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in self.store.query(
                    f"SELECT file_id, status, reason FROM files WHERE workspace_id=? AND filespace=? "
                    f"AND file_id IN ({','.join('?' * len(part))})", [ws, name, *part]):
                status[r["file_id"]] = (r["status"], r["reason"])
        base = "" if folder == "/" else folder
        out = [{"name": e.name, "path": f"{base}/{e.name}", "isDir": e.type == "dir",
                "size": e.size if e.type == "file" else None, "mtime": mtime_iso(e.mtime),
                "status": (status.get(e.file_id, (None, None))[0] if e.type == "file" else None),
                "reason": (status.get(e.file_id, (None, None))[1] if e.type == "file" else None)} for e in ordered]
        parent = None if folder == "/" else (folder.rsplit("/", 1)[0] or "/")
        return {"name": name, "path": folder, "parent": parent, "entries": out,
                "truncated": len(dirs) + len(files) > LIST_MAX}

    def folder_file(self, name: str, path: str) -> str:
        """Absolute path of a regular file in a connected block source, for streaming."""
        src = self._block_source(name)
        folder = folders_mod.normalize_folder(path)
        if folder == "/":
            raise folders_mod.FolderError(400, "That's a folder, not a file.")
        try:
            _rel, full = src._resolve(folder)
        except PathRefused:
            raise folders_mod.FolderError(400, "That path is outside the folder, or goes through a link.") from None
        try:
            st = os.lstat(full)
        except (FileNotFoundError, NotADirectoryError):
            raise NotFound(f"{folder} isn't in {name}") from None
        if stat_mod.S_ISDIR(st.st_mode):
            raise folders_mod.FolderError(400, "That's a folder, not a file.")
        if not stat_mod.S_ISREG(st.st_mode):
            raise NotFound(f"{folder} isn't a file")
        return full

    def folder_options(self, name: str, download_online_only: bool) -> dict:
        spec = next((b for b in self.scheduler._block_specs if b.name == name), None)
        if spec is None:
            raise NotFound(f"no connected folder called {name!r}")
        if spec.origin != "app":
            raise folders_mod.FolderError(400, f"{name} was set up on the engine computer; its options can't be changed here.")
        self.store.set_block_download(name, download_online_only)
        ws = self.scheduler._block_workspace()
        self.scheduler.refresh_targets(force=True)
        if download_online_only and self.store.requeue_reason(ws, name, ONLINE_ONLY):
            self.scheduler.request_sync([(ws, name)])
        return next(e for e in self.scheduler.block_sources if e["name"] == name)

    def remove_folder(self, name: str) -> dict:
        spec = next((b for b in self.scheduler._block_specs if b.name == name), None)
        if spec is None:
            raise NotFound(f"no connected folder called {name!r}")
        if spec.origin == "app":
            self.store.remove_block_folder(name)
        else:
            self.store.add_block_removed(name, now_iso())
        ws = self.scheduler._block_workspace()
        self.scheduler.refresh_targets(force=True)
        files, chunks = self.store.purge_source(ws, name)
        logger.info("removed block source %s (%s): %d files, %d chunks forgotten", name, spec.origin, files, chunks)
        return {"removed": name, "files": files, "chunks": chunks}

    # ------------------------------------------------------------ control
    def sync(self, workspace_id: Optional[str], filespace: Optional[str]) -> list[dict]:
        keys = sorted(self.scopes(workspace_id, filespace))
        queued = self.scheduler.request_sync(keys)
        return [{"workspaceId": ws, "filespace": fs} for ws, fs in queued]

    def set_enabled(self, workspace_id: Optional[str], filespace: str, enabled: bool) -> dict:
        ws = self.resolve_workspace(workspace_id)
        if ws is None:
            raise NotFound("no workspace configured")
        self.store.set_filespace_enabled(ws, filespace, enabled)
        self.scheduler.refresh_targets(force=True)
        if enabled:
            self.scheduler.request_sync([(ws, filespace)])
        return self.filespace_status(ws, filespace)

    def hint(self, workspace_id: Optional[str], filespace: str, op: str, path: str,
             src: Optional[str] = None) -> bool:
        ws = self.resolve_workspace(workspace_id)
        if ws is None:
            return False
        return self.scheduler.add_hint(ws, filespace, {"op": op, "path": path, "src": src})  # drops non-targets

    def retry(self, workspace_id: Optional[str], filespace: str, paths: Optional[list[str]] = None,
              all_failed: bool = False) -> int:
        """Move named (or, with `all_failed`, every) failed file back to pending,
        clearing the retry back-off, and ask for a sync of that source. Paths
        that are not failed are ignored; skipped files are never retried."""
        ws = self.resolve_workspace(workspace_id)
        if ws is None:
            return 0
        if all_failed:
            n = self.store.requeue_failed(ws, filespace)
        elif paths:
            n = self.store.requeue_failed(ws, filespace, paths)
        else:
            n = 0
        if n:
            self.scheduler.request_sync([(ws, filespace)])
        return n

    def control(self) -> dict:
        ctl = self.store.control()
        return {"paused": ctl["paused"], "pausedAt": ctl["paused_at"],
                "priority": [{"workspaceId": p.get("workspaceId"), "filespace": p.get("filespace")}
                             for p in ctl["priority"]]}

    def set_control(self, paused: Optional[bool] = None, priority: Optional[list[dict]] = None) -> dict:
        """Pause/resume and set the priority list. Sources that are not indexing
        targets, and repeats, are dropped from `priority`."""
        ctl = self.store.control()
        new_paused, paused_at = ctl["paused"], ctl["paused_at"]
        if paused is not None:
            if paused and not ctl["paused"]:
                paused_at = now_iso()
            elif not paused:
                paused_at = None
            new_paused = bool(paused)
        prio = ctl["priority"]
        if priority is not None:
            targets = set(self.scheduler.targets())
            prio, seen = [], set()
            for p in priority:
                key = (p.get("workspaceId"), p.get("filespace"))
                if key in targets and key not in seen:
                    seen.add(key)
                    prio.append({"workspaceId": key[0], "filespace": key[1]})
        self.store.set_control(new_paused, paused_at, prio)
        self.scheduler.wake()
        return self.control()

    # ------------------------------------------------------------ query
    def files(self, workspace_id: Optional[str], filespace: str, status: str, limit: int = 50,
              offset: int = 0, q: Optional[str] = None) -> dict:
        """One page of a source's files in one status, from the ledger."""
        if status not in FILE_ORDER:
            raise ValueError(f"unknown status {status!r}")
        limit = max(1, min(int(limit), FILES_MAX))
        ws = self.resolve_workspace(workspace_id)
        if ws is None:
            return {"files": [], "total": 0, "byReason": []}
        row = self.store.filespace_row(ws, filespace) or {}
        kind = row.get("kind") or KIND_LUCIDLINK
        rows, total = self.store.list_files(ws, filespace, status, limit, max(0, int(offset)), q or None)
        by_reason = ([{"reason": r["reason"], "files": r["files"]}
                      for r in self.store.reasons(ws, filespace, status)]
                     if status in ("failed", "skipped") else [])
        return {
            "files": [{"path": r["path"], "size": r["size"], "mtime": mtime_iso(r["mtime"]),
                       "status": r["status"], "reason": r["reason"], "indexedAt": r["indexed_at"],
                       "changedAt": r["changed_at"], "kind": kind} for r in rows],
            "total": total,
            "byReason": by_reason,
        }

    def search(self, workspace_id: Optional[str], filespace: Optional[str], query: str, k: int = 20,
               path_prefix: Optional[str] = None) -> list[dict]:
        """Chunk hits, hybrid-ranked (the `results` of search_all)."""
        return self.search_all(workspace_id, filespace, query, k, path_prefix)["results"]

    def search_all(self, workspace_id: Optional[str], filespace: Optional[str], query: str, k: int = 20,
                   path_prefix: Optional[str] = None, mode: str = "all", filters: Optional[dict] = None,
                   files: Optional[list[dict]] = None) -> dict:
        """All search: chunk results, file results, counts and refinements (docs: All search).
        files: [FileRef] limits results to those files (docs: Collections)."""
        return search_mod.search(self, workspace_id, filespace, query, k, path_prefix, mode, filters, files)

    def collection_files(self, query: str, mode: str = "all", filters: Optional[dict] = None,
                         scope: Optional[dict] = None, pinned: Optional[list[dict]] = None,
                         excluded: Optional[list[dict]] = None, k: int = search_mod.COLLECTION_K) -> dict:
        """A collection's current members (docs: Collections)."""
        return search_mod.collection_files(self, query, mode, filters, scope, pinned, excluded, k)

    def related(self, workspace_id: Optional[str], filespace: str, path: str, k: int = related_mod.K,
                scope: Optional[dict] = None) -> dict:
        """Related files for one file (docs: Related files). NotFound when it is not in the ledger."""
        with self.store.read_view():       # many reads; never queue behind indexing
            ws = self.resolve_workspace(workspace_id)
            scopes = self.scopes("*", None) if scope is None else self.scopes(scope.get("workspaceId"),
                                                                              scope.get("filespace") or None)
            rows = self.store.live_files_at_path(ws, filespace, path) if ws else []
            if not rows:
                raise NotFound("file not found in the index")
            row = max(rows, key=lambda r: r["mtime"] or 0)
            return related_mod.related(self, row, scopes | {(row["workspace_id"], row["filespace"])}, k)

    # ------------------------------------------------------------ discovered collections
    def _discover_location(self, workspace_id: Optional[str], filespace: str) -> tuple[str, str]:
        ws = self.resolve_workspace(workspace_id)
        if not ws or (ws, filespace) not in set(self.scheduler.targets()):
            raise NotFound(f"no location called {filespace!r}")
        return ws, filespace

    def _discover_scope_out(self, sc: dict, counts: dict) -> dict:
        key = (sc["workspace_id"], sc["filespace"])
        run = self.discover_running
        running = run is not None and (run["workspaceId"], run["filespace"]) == key
        state = ("running" if running else "error" if sc["error"]
                 else "waiting" if sc["requested_at"] and sc["enabled"] else "done" if sc["finished_at"] else "idle")
        return {"workspaceId": key[0], "filespace": key[1], "location": self.locations().get(key),
                "enabled": bool(sc["enabled"]), "state": state,
                "progress": ({"phase": run["phase"], "done": run["done"], "total": run["total"]} if running else None),
                "lastRunAt": sc["finished_at"], "error": sc["error"],
                "stats": json.loads(sc["stats"]) if sc["stats"] else None,
                "groups": {s: counts.get((key, s), 0) for s in ("proposed", "kept", "dismissed")},
                "unnamed": counts.get((key, "unnamed"), 0)}

    def discover_status(self) -> dict:
        """Locations switched on for discovery, their last run and their groups."""
        counts: dict = defaultdict(int)
        for r in self.store.query("SELECT workspace_id, filespace, status, named_by, COUNT(*) AS n "
                                  "FROM discover_groups GROUP BY 1, 2, 3, 4"):
            key = (r["workspace_id"], r["filespace"])
            counts[(key, r["status"])] += r["n"]
            if r["status"] == "proposed" and r["named_by"] == "auto":
                counts[(key, "unnamed")] += r["n"]
        return {"ai": self.llm.configured, "ready": self.file_vectors_ready,
                "locations": [self._discover_scope_out(sc, counts) for sc in self.store.discover_scopes()]}

    def discover_start(self, workspace_id: Optional[str], filespace: str) -> dict:
        """Switch discovery on for one location and run it soon (docs: Discovered collections)."""
        ws, fs = self._discover_location(workspace_id, filespace)
        self.store.discover_request(ws, fs)
        self._dc_wake.set()
        return next(x for x in self.discover_status()["locations"] if (x["workspaceId"], x["filespace"]) == (ws, fs))

    def discover_stop(self, workspace_id: Optional[str], filespace: str) -> dict:
        """No more runs for this location; its groups stay."""
        ws = self.resolve_workspace(workspace_id)
        if not ws or not self.store.discover_disable(ws, filespace):
            raise NotFound(f"discovery is not on for {filespace!r}")
        return {"workspaceId": ws, "filespace": filespace, "enabled": False}

    def discover_groups(self, workspace_id: Optional[str], filespace: Optional[str], status: str = "proposed",
                        limit: int = 50, offset: int = 0) -> dict:
        """Groups, strongest first. workspaceId "*" (or none with no filespace) = every location."""
        if status not in ("proposed", "kept", "dismissed", "merged"):
            raise ValueError(f"unknown status {status!r}")
        ws = None if workspace_id == ALL_WORKSPACES or (not workspace_id and not filespace) \
            else self.resolve_workspace(workspace_id)
        rows = self.store.discover_group_rows(ws, filespace or None, [status])
        limit = max(1, min(int(limit), DISCOVER_LIMIT_MAX))
        return {"total": len(rows), "groups": [discover_mod.group_out(self, r) for r in rows[offset:offset + limit]]}

    def discover_group(self, gid: str) -> dict:
        row = self.store.discover_group_row(gid)
        if row is None:
            raise NotFound("no such group")
        with self.store.read_view():
            return discover_mod.group_out(self, row, files=True)

    def discover_act(self, gid: str, action: str, name: Optional[str] = None) -> dict:
        """keep | dismiss | restore (back to proposed) | rename. Keep answers with the
        group's files and the collection to save ({name, pinned})."""
        row = self.store.discover_group_row(gid)
        if row is None:
            raise NotFound("no such group")
        if action == "rename":
            name = " ".join((name or "").split())
            if not name or len(name) > discover_mod.NAME_MAX:
                raise ValueError(f"name must be 1–{discover_mod.NAME_MAX} characters")
            self.store.discover_update(gid, name=name, named_by="you")
        elif action in ("keep", "dismiss", "restore"):
            if row["status"] == "merged":
                raise ValueError("this group was merged into another")
            self.store.discover_update(gid, status={"keep": "kept", "dismiss": "dismissed",
                                                    "restore": "proposed"}[action])
        else:
            raise ValueError(f"unknown action {action!r}")
        return self.discover_group(gid)

    def discover_merge(self, ids: list[str], name: Optional[str] = None) -> dict:
        try:
            return discover_mod.merge(self, ids, name)
        except LookupError as e:
            raise NotFound(str(e)) from None

    def collection_suggest(self, pinned: list[dict], excluded: Optional[list[dict]] = None,
                           k: int = discover_mod.SUGGEST_K, since: Optional[str] = None,
                           scope: Optional[dict] = None) -> dict:
        """Files outside a collection that link strongly to it (docs: Discovered
        collections, "Grows on its own"). since: only files changed after it."""
        after = None
        if since:
            after = _iso_micros(since, "since") / 1e6
        with self.store.read_view():
            members = search_mod.resolve_files(self, pinned).rows
            if not members:
                return {"names": [], "files": []}
            scopes = (search_mod.FileSet(members).scopes if scope is None
                      else self.scopes(scope.get("workspaceId"), scope.get("filespace") or None))
            excl = {(r["workspace_id"], r["filespace"], r["file_id"])
                    for r in search_mod.resolve_files(self, excluded or []).rows}
            return discover_mod.suggest(self, members, excl, scopes, max(1, min(int(k), 50)), after)

    def recent(self, workspace_id: Optional[str], filespace: Optional[str], limit: int = 20) -> list[dict]:
        """Most recently changed files, newest first (see recent_page)."""
        return self.recent_page(workspace_id, filespace, limit)["files"]

    def recent_page(self, workspace_id: Optional[str], filespace: Optional[str], limit: int = 20, *,
                    kind: Optional[str] = None, changes: Optional[Iterable[str]] = None,
                    since: Optional[str] = None, before: Optional[str] = None) -> dict:
        """One page of GET /index/recent: {"files", "next"} (docs: What's new).

        Live files are ordered by their own mtime (when the file changed, not when
        the indexer noticed); deletions and renames by when the indexer saw them;
        that timestamp is each item's `when`. `change` is new | modified | renamed |
        deleted, or null when it can't be derived (e.g. files found by a
        filespace's first crawl). Filters (kind, change, since ≤ when < before)
        apply in SQL before the limit. A page never splits files tied on `when`,
        so it can run over `limit`; `next` is the `before` of the following page,
        null when nothing older matches. Raises ValueError on a bad filter.
        """
        limit = max(1, min(int(limit), RECENT_MAX))
        exts = None
        if kind:
            classes = KIND_CLASSES.get(kind)
            if classes is None:
                raise ValueError(f"unknown kind {kind!r}; expected one of {', '.join(KIND_CLASSES)}")
            # The extensions type_of() puts in these classes (the first class listing one wins).
            exts = {e for c in classes for e in TYPE_EXT[c] if type_of("x" + e) in classes}
        if changes is not None:
            changes = {c.strip() for c in changes if c.strip()}
            bad = sorted(changes - set(RECENT_CHANGES))
            if bad or not changes:
                raise ValueError(f"unknown change {', '.join(bad) or '(empty)'}; "
                                 f"expected some of {', '.join(RECENT_CHANGES)}")
        since_us = _iso_micros(since, "since") if since else None
        before_us = _iso_micros(before, "before") if before else None
        scopes = sorted(self.scopes(workspace_id, filespace))
        kinds = self.kinds()
        locs = self.locations()
        # A day of slack for clock skew; anything later is a wrong timestamp, not a recent change.
        latest = time.time() + 86400
        query = lambda n, before_us: self.store.recent_files(
            scopes, n, latest=latest, since_us=since_us, before_us=before_us, changes=changes, exts=exts)
        seen: set[tuple[str, str, str]] = set()
        rows: list[dict] = []
        for r in query(limit, before_us):
            key = (r["workspace_id"], r["filespace"], r["file_id"])
            if key not in seen:
                seen.add(key)
                rows.append(r)
        rows.sort(key=lambda r: (-r["when_us"], r["path"], r["workspace_id"], r["filespace"]))
        nxt = None
        if len(rows) >= limit:
            cut = rows[limit - 1]["when_us"]
            older = any(r["when_us"] < cut for r in rows)
            rows = [r for r in rows if r["when_us"] >= cut]
            if older or query(1, cut):
                nxt = _micros_iso(cut)
        files = []
        for r in rows:
            name = r["path"].rsplit("/", 1)[-1]
            files.append({
                "workspaceId": r["workspace_id"], "filespace": r["filespace"],
                "kind": kinds.get((r["workspace_id"], r["filespace"]), KIND_LUCIDLINK), "path": r["path"],
                "location": locs.get((r["workspace_id"], r["filespace"])),
                "size": r["size"], "mtime": mtime_iso(r["mtime"]), "status": r["status"],
                "indexedAt": r.get("indexed_at"), "change": r.get("change_kind"), "changedAt": r.get("changed_at"),
                "previousPath": r.get("prev_path"), "when": _micros_iso(r["when_us"]), "type": type_of(name),
            })
        return {"files": files, "next": nxt}

    def by_kind(self, workspace_id: Optional[str], filespace: Optional[str], kind: str,
                limit: int = 100, offset: int = 0) -> dict:
        """Live files of one kind across locations, newest first (docs: By kind).

        SQL narrows by the classes' extensions; type_of() then classifies each
        name exactly (an extension listed under two classes belongs to the first).
        Files dated more than a day ahead go last, as in recent().
        """
        classes = KIND_CLASSES.get(kind)
        if classes is None:
            raise ValueError(f"unknown kind {kind!r}; expected one of {', '.join(KIND_CLASSES)}")
        limit = max(1, min(int(limit), BYKIND_MAX))
        offset = max(0, int(offset))
        exts = set().union(*(TYPE_EXT[c] for c in classes))
        scopes = sorted(self.scopes(workspace_id, filespace))
        locs = self.locations()
        latest = time.time() + 86400
        items: list[tuple[tuple, dict]] = []
        for r in self.store.files_with_extensions(scopes, exts):
            name = r["path"].rsplit("/", 1)[-1]
            t = type_of(name)
            if t not in classes:
                continue
            secs = mtime_seconds(r["mtime"]) or 0.0
            key = (r["workspace_id"], r["filespace"])
            items.append(((secs > latest, -secs, r["path"], key), {
                "workspaceId": key[0], "filespace": key[1],
                "location": locs.get(key, {"kind": KIND_LUCIDLINK, "root": None, "provider": None}),
                "path": r["path"], "name": name, "size": r["size"], "mtime": mtime_iso(r["mtime"]),
                "status": r["status"], "type": t,
            }))
        items.sort(key=lambda it: it[0])
        return {"files": [it[1] for it in items[offset:offset + limit]], "total": len(items), "kind": kind}

    def surveys(self, workspace_id: Optional[str], filespace: Optional[str] = None) -> list[dict]:
        """Stored root surveys of every location in scope, newest first; none generated."""
        scopes = self.scopes(workspace_id, filespace)
        locs = self.locations()
        out = []
        for row in self.store.root_surveys():
            key = (row["workspace_id"], row["filespace"])
            if key not in scopes:
                continue
            try:
                topics = json.loads(row["payload"]).get("topics") or []
            except (ValueError, AttributeError):
                topics = []
            out.append({"workspaceId": key[0], "filespace": key[1],
                        "location": locs.get(key, {"kind": KIND_LUCIDLINK, "root": None, "provider": None}),
                        "generatedAt": row["generated_at"], "stale": bool(row["stale"]), "topics": topics})
        out.sort(key=lambda s: (s["generatedAt"] or "", s["workspaceId"], s["filespace"]), reverse=True)
        return out

    def profile(self, workspace_id: Optional[str], filespace: str, now: Optional[float] = None) -> dict:
        """What a source holds, from the ledger only (nothing is read from the source)."""
        ws = self.resolve_workspace(workspace_id)
        row = self.store.filespace_row(ws, filespace) if ws else None
        if row is None:
            raise NotFound(f"'{filespace}' is not indexed")
        now = time.time() if now is None else now
        by_type = {t: {"type": t, "files": 0, "bytes": 0} for t in TYPES}
        ages = {key: 0 for key, _ in AGE_BUCKETS}
        folders: dict[str, dict] = defaultdict(lambda: {"files": 0, "bytes": 0})
        files = total = 0
        for f in self.store.live_file_stats(ws, filespace):
            size = int(f["size"] or 0)
            files += 1
            total += size
            t = by_type[type_of(f["path"].rsplit("/", 1)[-1])]
            t["files"] += 1
            t["bytes"] += size
            secs = mtime_seconds(f["mtime"])
            age = (now - secs) / DAY if secs else float("inf")
            ages[next(k for k, days in AGE_BUCKETS if days is None or age <= days)] += 1
            parts = [p for p in f["path"].split("/") if p][:-1]
            if parts:   # files directly in the root belong to no folder
                top = folders["/" + parts[0]]
                top["files"] += 1
                top["bytes"] += size
        largest = sorted(folders.items(), key=lambda kv: (-kv[1]["bytes"], kv[0]))[:LARGEST_FOLDERS]
        return {
            "workspaceId": ws, "filespace": filespace, "kind": row.get("kind") or KIND_LUCIDLINK,
            "totals": {"files": files, "bytes": total},
            "byType": sorted((t for t in by_type.values() if t["files"]),
                             key=lambda t: (-t["bytes"], TYPES.index(t["type"]))),
            "ages": ages,
            "largestFolders": [{"path": p, **v} for p, v in largest],
            "skipped": [{"reason": r["reason"] or "skipped", "files": r["files"]}
                        for r in self.store.skipped_reasons(ws, filespace)],
            "lastScanAt": row.get("last_scan_at"),
        }

    def ask(self, workspace_id: Optional[str], filespace: Optional[str], question: str, k: int = 12,
            files: Optional[list[dict]] = None) -> dict:
        """files: [FileRef] limits retrieval (and citations) to those files."""
        if files is None:
            return ask_mod.answer(self, self.scopes(workspace_id, filespace), question, k)
        only = search_mod.resolve_files(self, files)
        return ask_mod.answer(self, search_mod.file_scopes(self, workspace_id, filespace, only), question, k, only)

    def get_survey(self, workspace_id: Optional[str], filespace: str, scope: str = "/") -> dict:
        ws = self.resolve_workspace(workspace_id)
        found = survey_mod.stored(self, ws, filespace, scope) if ws else None
        if found is None:
            raise NotFound("survey not generated yet")
        return found

    def regenerate_survey(self, workspace_id: Optional[str], filespace: str, scope: str = "/") -> dict:
        ws = self.resolve_workspace(workspace_id)
        if ws is None:
            raise NotFound("no workspace configured")
        return survey_mod.generate(self, ws, filespace, scope)
