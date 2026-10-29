"""Indexer configuration, read once from the environment.

Everything is a plain dataclass so tests can build a Config directly instead of
patching os.environ; `Config.from_env()` is what the container uses.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int, floor: int = 0) -> int:
    try:
        return max(floor, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class Config:
    db_path: str = "/data/index.db"
    internal_key: str = ""

    embed_base_url: str = ""
    embed_model: str = ""
    embed_api_key: str = ""
    embed_batch: int = 32
    embed_timeout: float = 120.0

    ruvector_url: str = ""
    ruvector_api_key: str = ""

    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout: float = 180.0

    sync_interval_sec: int = 300
    max_file_bytes: int = 25 * 1024 * 1024
    # Survey regeneration after a changed sync is rate-limited to this.
    survey_min_interval_sec: int = 600
    # Concurrent filespace jobs. Each one pins a linked filespace (~1 GB cache),
    # so this defaults to the link limit rather than to the filespace count.
    concurrency: int = 2
    max_linked_filespaces: int = 2
    max_clients: int = 2
    # Scheduler wake-up granularity; hints are picked up within this.
    tick_sec: float = 0.5
    # How often the vault is re-read for the list of filespaces to index.
    targets_refresh_sec: float = 30.0
    # A file that failed to read/extract is retried after this long.
    failed_retry_sec: int = 3600
    # A job indexes for at most this long, then yields its slot so one huge
    # filespace cannot starve the others (or their hints) for hours.
    job_budget_sec: float = 120.0
    # Files taken from the queue per job (the budget usually ends it sooner).
    job_max_files: int = 2000
    # Full rescans wait at least this many times the last scan's duration, so
    # a filespace whose metadata walk takes minutes is not walked back to back.
    scan_interval_factor: float = 5.0
    # Connected block storage (In Place, Phase A): "name=/container/path;…"
    # and the workspace they belong to ("" = the vault's active workspace).
    block_sources: str = ""
    block_source_workspace: str = ""
    # Folders from the app (folders.py): roots mounted read-only under this
    # directory, their folders on the Mini, and the Mini folders behind the
    # BLOCK_SOURCES slots (/block/sourceN), all as plain strings.
    block_root_base: str = "/host"
    # The engine computer's name, shown as the group label of its drives (start.sh fills it).
    engine_name: str = ""
    # Drive prompts: a connected folder counts as missing after this long.
    folder_missing_grace_sec: float = 900.0
    block_root_hosts: dict = field(default_factory=dict)
    block_slot_hosts: dict = field(default_factory=dict)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)

    @property
    def embed_configured(self) -> bool:
        return bool(self.embed_base_url and self.embed_model)

    @classmethod
    def from_env(cls) -> "Config":
        links = _int("INDEX_MAX_LINKED_FILESPACES", 2, floor=1)
        return cls(
            db_path=os.environ.get("INDEX_DB_PATH", "/data/index.db"),
            internal_key=os.environ.get("INDEXER_INTERNAL_KEY", ""),
            embed_base_url=os.environ.get("EMBED_BASE_URL", "").rstrip("/"),
            embed_model=os.environ.get("EMBED_MODEL", ""),
            embed_api_key=os.environ.get("EMBED_API_KEY", ""),
            embed_timeout=_float("EMBED_TIMEOUT", 120.0),
            ruvector_url=os.environ.get("RUVECTOR_URL", "").rstrip("/"),
            ruvector_api_key=os.environ.get("RUVECTOR_API_KEY", ""),
            llm_base_url=os.environ.get("LLM_BASE_URL", "").rstrip("/"),
            llm_model=os.environ.get("LLM_MODEL", ""),
            llm_api_key=os.environ.get("LLM_API_KEY", ""),
            llm_timeout=_float("LLM_TIMEOUT", 180.0),
            sync_interval_sec=_int("SYNC_INTERVAL_SEC", 300, floor=10),
            max_file_bytes=_int("INDEX_MAX_FILE_BYTES", 25 * 1024 * 1024, floor=1),
            concurrency=_int("INDEX_CONCURRENCY", links, floor=1),
            max_linked_filespaces=links,
            max_clients=_int("INDEX_MAX_CLIENTS", 2, floor=1),
            job_budget_sec=_float("INDEX_JOB_BUDGET_SEC", 120.0),
            block_sources=os.environ.get("BLOCK_SOURCES", "").strip(),
            block_source_workspace=os.environ.get("BLOCK_SOURCE_WORKSPACE", "").strip(),
            folder_missing_grace_sec=_float("FOLDER_MISSING_GRACE_SEC", 900.0),
            engine_name=os.environ.get("ENGINE_NAME", "").strip(),
            block_root_base=os.environ.get("BLOCK_ROOT_BASE", "/host").rstrip("/") or "/host",
            block_root_hosts={rid: os.environ.get(var, "").strip()
                              for rid, var in (("volumes", "BLOCK_ROOT_VOLUMES"), ("icloud", "BLOCK_ROOT_ICLOUD"),
                                               ("desktop", "BLOCK_ROOT_DESKTOP"),
                                               ("documents", "BLOCK_ROOT_DOCUMENTS"),
                                               ("cloud", "BLOCK_ROOT_CLOUD"))},
            block_slot_hosts={f"/block/source{n}": os.environ.get(
                "BLOCK_SOURCE_PATH" if n == 1 else f"BLOCK_SOURCE_PATH_{n}", "").strip() for n in range(1, 7)},
        )
