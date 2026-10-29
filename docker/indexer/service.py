"""The indexer's composition root and the operations behind each HTTP route.

`IndexerService` wires store, settings, sources, embedder, vectors, LLM and
scheduler together. Every dependency can be injected, which is how the tests
run the whole thing against an in-memory filespace, a fake embedder and
`httpx.MockTransport` for Ruvector and the LLM. app.py stays a thin HTTP layer.
"""
from __future__ import annotations

import logging
import os
import stat as stat_mod
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import httpx

from . import ask as ask_mod
from . import folders as folders_mod
from . import survey as survey_mod
from .block import KIND_LUCIDLINK, TYPES, LocalSource, PathRefused, RoutingSourceProvider, mounted, type_of
from .block import NotMounted as NotMountedError
from .config import Config
from .embedder import Embedder
from .llm import LLM, LLMUnavailable
from .source import SdkSourceProvider, SettingsProvider, SourceProvider, VaultSettings
from .store import FILE_ORDER, Store, now_iso
from .vectors import LocalIndex, RuvectorMirror, Scope, VectorService
from .worker import Scheduler, iso

logger = logging.getLogger("indexer.service")

SNIPPET_CHARS = 300


RECENT_MAX = 200
FILES_MAX = 200

DAY = 86400.0
# GET /index/profile age buckets: (key, max age in days), exclusive, oldest last.
AGE_BUCKETS = (("7d", 7), ("30d", 30), ("90d", 90), ("older", None))
LARGEST_FOLDERS = 10


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


class NotFound(Exception):
    pass


LIST_MAX = 2000
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
        self.vectors = VectorService(self.store, LocalIndex(self.store, self.embedder.model), mirror)
        self.scheduler = Scheduler(self)
        # Block keys get a LocalSource; everything else the filespace provider
        # (looked up per lease, so `self.sources` can be swapped in tests).
        self.router = RoutingSourceProvider(lambda ws, fs: self.scheduler.block_root(ws, fs),
                                            lambda: self.sources)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.scheduler.start()

    def stop(self) -> None:
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

    def kinds(self) -> dict[tuple[str, str], str]:
        """(workspace, filespace) -> "lucidlink" | "block", from the ledger."""
        return self.store.kinds()

    # ------------------------------------------------------------ status
    def filespace_status(self, ws: str, fs: str, ctl: Optional[dict] = None) -> dict:
        row = self.store.filespace_row(ws, fs) or {}
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
            # A refused block source with this name shows here too (the
            # filespace itself keeps working).
            "lastError": row.get("last_error") or self.scheduler.block_conflict(ws, fs),
            "progress": dict(rt.progress),
            "surveyError": rt.survey_error,
        }

    def status(self) -> dict:
        ctl = self.store.control()
        return {
            "paused": ctl["paused"],
            "filespaces": [self.filespace_status(ws, fs, ctl) for ws, fs in self.scheduler.targets()],
            "embedder": self.embedder.status(),
            "vectorStore": self.vectors.status(),
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
        """New drives under /Volumes and connected folders that have gone missing (docs: Drive prompts)."""
        now = now_iso()
        new: list[dict] = []
        root = "volumes"
        if folders_mod.root_available(self.config, root):
            listing = folders_mod.browse(self.config, root, "/", lambda *_: None)
            names = [e["name"] for e in listing["entries"]]
            seen = self.store.drives_seen(root)
            if not seen:
                # The first look: what is already mounted is known, not new ("" marks the look itself).
                self.store.add_drives_seen(root, names + [""], now, baseline=True)
            else:
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

    def dismiss_prompt(self, kind: str, path: Optional[str], name: Optional[str]) -> dict:
        if kind == "new":
            drive = (path or "").strip("/")
            if not drive or "/" in drive or not self.store.dismiss_drive("volumes", drive):
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
        status: dict[str, str] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in self.store.query(
                    f"SELECT file_id, status FROM files WHERE workspace_id=? AND filespace=? "
                    f"AND file_id IN ({','.join('?' * len(part))})", [ws, name, *part]):
                status[r["file_id"]] = r["status"]
        base = "" if folder == "/" else folder
        out = [{"name": e.name, "path": f"{base}/{e.name}", "isDir": e.type == "dir",
                "size": e.size if e.type == "file" else None, "mtime": mtime_iso(e.mtime),
                "status": (status.get(e.file_id) if e.type == "file" else None)} for e in ordered]
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
        scopes = self.scopes(workspace_id, filespace)
        if not scopes or not any(self.store.chunk_count(ws, fs) for ws, fs in scopes):
            return []
        q = self.embedder.embed_query(query)
        hits = self.vectors.search(q, max(1, min(k, 200)), scopes, path_prefix)
        rows = self.store.chunk_rows([pid for pid, _ in hits])
        kinds = self.kinds()
        out = []
        for pid, score in hits:
            r = rows.get(pid)
            if r is None:
                continue
            text = " ".join(r["text"].split())
            out.append({
                "workspaceId": r["workspace_id"], "filespace": r["filespace"],
                "kind": kinds.get((r["workspace_id"], r["filespace"]), KIND_LUCIDLINK), "path": r["path"],
                "chunkIndex": r["chunk_index"], "score": round(score, 4),
                "snippet": text[:SNIPPET_CHARS] + ("…" if len(text) > SNIPPET_CHARS else ""),
                "mtime": mtime_iso(r["mtime"]),
            })
        return out

    def recent(self, workspace_id: Optional[str], filespace: Optional[str], limit: int = 20) -> list[dict]:
        """Most recently changed files, newest first, from the ledger.

        Live files are ordered by their own mtime (when the file changed, not when
        the indexer noticed); deletions and renames by when the indexer saw them.
        `change` is new | modified | renamed | deleted, or null when it can't be
        derived (e.g. files found by a filespace's first crawl).
        """
        limit = max(1, min(int(limit), RECENT_MAX))
        scopes = sorted(self.scopes(workspace_id, filespace))
        kinds = self.kinds()
        seen: set[tuple[str, str, str]] = set()
        items: list[tuple[float, dict]] = []
        for r in self.store.recent_files(scopes, limit):
            key = (r["workspace_id"], r["filespace"], r["file_id"])
            if key in seen:
                continue
            seen.add(key)
            kind = r.get("change_kind")
            by_change = r["status"] == "deleted" or kind == "renamed"
            when = (_iso_seconds(r.get("changed_at")) if by_change else None) or mtime_seconds(r["mtime"]) or 0.0
            items.append((when, {
                "workspaceId": r["workspace_id"], "filespace": r["filespace"],
                "kind": kinds.get((r["workspace_id"], r["filespace"]), KIND_LUCIDLINK), "path": r["path"],
                "size": r["size"], "mtime": mtime_iso(r["mtime"]), "status": r["status"],
                "indexedAt": r.get("indexed_at"), "change": kind, "changedAt": r.get("changed_at"),
                "previousPath": r.get("prev_path"),
            }))
        items.sort(key=lambda it: (-it[0], it[1]["path"]))
        return [it[1] for it in items[:limit]]

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

    def ask(self, workspace_id: Optional[str], filespace: Optional[str], question: str, k: int = 12) -> dict:
        return ask_mod.answer(self, self.scopes(workspace_id, filespace), question, k)

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
