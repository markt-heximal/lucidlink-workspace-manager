"""The indexer's composition root and the operations behind each HTTP route.

`IndexerService` wires store, settings, sources, embedder, vectors, LLM and
scheduler together. Every dependency can be injected, which is how the tests
run the whole thing against an in-memory filespace, a fake embedder and
`httpx.MockTransport` for Ruvector and the LLM. app.py stays a thin HTTP layer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from . import ask as ask_mod
from . import survey as survey_mod
from .config import Config
from .embedder import Embedder
from .llm import LLM, LLMUnavailable
from .source import SdkSourceProvider, SettingsProvider, SourceProvider, VaultSettings
from .store import Store
from .vectors import LocalIndex, RuvectorMirror, Scope, VectorService
from .worker import Scheduler, iso

logger = logging.getLogger("indexer.service")

SNIPPET_CHARS = 300


def mtime_iso(v: Optional[int]) -> Optional[str]:
    """SDK mtimes are Unix timestamps of unspecified unit; normalise to ISO."""
    if not v:
        return None
    x = float(v)
    for limit, div in ((1e17, 1e9), (1e14, 1e6), (1e11, 1e3)):
        if x > limit:
            x /= div
            break
    try:
        return datetime.fromtimestamp(x, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


class NotFound(Exception):
    pass


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
        """Search scope: one filespace, or every enabled filespace of the workspace."""
        ws = self.resolve_workspace(workspace_id)
        if ws is None:
            return set()
        if filespace:
            return {(ws, filespace)}
        return {k for k in self.scheduler.targets() if k[0] == ws and self.scheduler.enabled(*k)}

    # ------------------------------------------------------------ status
    def filespace_status(self, ws: str, fs: str) -> dict:
        row = self.store.filespace_row(ws, fs) or {}
        rt = self.scheduler.runtime(ws, fs)
        enabled = bool(row.get("enabled", 1))
        return {
            "workspaceId": ws,
            "filespace": fs,
            "enabled": enabled,
            "state": self.scheduler.state(ws, fs),
            "lastScanAt": row.get("last_scan_at"),
            "lastSyncAt": row.get("last_sync_at"),
            "nextScanAt": (iso(rt.next_scan_at) or (iso(datetime.now(timezone.utc).timestamp())))
            if enabled else None,
            "files": self.store.file_counts(ws, fs),
            "chunks": self.store.chunk_count(ws, fs),
            "lastError": row.get("last_error"),
            "progress": dict(rt.progress),
            "surveyError": rt.survey_error,
        }

    def status(self) -> dict:
        return {
            "filespaces": [self.filespace_status(ws, fs) for ws, fs in self.scheduler.targets()],
            "embedder": self.embedder.status(),
            "vectorStore": self.vectors.status(),
            "settings": {"ok": self.scheduler.settings_error is None, "error": self.scheduler.settings_error},
        }

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
        return self.scheduler.add_hint(ws, filespace, {"op": op, "path": path, "src": src})

    # ------------------------------------------------------------ query
    def search(self, workspace_id: Optional[str], filespace: Optional[str], query: str, k: int = 20,
               path_prefix: Optional[str] = None) -> list[dict]:
        scopes = self.scopes(workspace_id, filespace)
        if not scopes or not any(self.store.chunk_count(ws, fs) for ws, fs in scopes):
            return []
        q = self.embedder.embed_query(query)
        hits = self.vectors.search(q, max(1, min(k, 200)), scopes, path_prefix)
        rows = self.store.chunk_rows([pid for pid, _ in hits])
        out = []
        for pid, score in hits:
            r = rows.get(pid)
            if r is None:
                continue
            text = " ".join(r["text"].split())
            out.append({
                "workspaceId": r["workspace_id"], "filespace": r["filespace"], "path": r["path"],
                "chunkIndex": r["chunk_index"], "score": round(score, 4),
                "snippet": text[:SNIPPET_CHARS] + ("…" if len(text) > SNIPPET_CHARS else ""),
                "mtime": mtime_iso(r["mtime"]),
            })
        return out

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
