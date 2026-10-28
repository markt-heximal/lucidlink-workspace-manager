"""Where filespace bytes come from: an abstract source, the SDK, and a fake.

The scanner only needs three calls — list a directory, stat a path, read a
file — so it is written against `FilespaceSource`. `SdkSourceProvider` backs it
with the LucidLink SDK; `MemorySource` is an in-memory filespace for tests.

The SDK side mirrors ll_core's registry (one Client per service-account token,
filespaces linked by id, both LRU-bounded because each link carries a ~1 GB
cache and worker threads) with two differences that matter for a crawler:

* A *lease* pins a linked filespace for the length of one job, so LRU eviction
  never unlinks a filespace mid-scan; eviction skips pinned links and the cache
  may briefly exceed its bound instead.
* Each linked filespace has its own lock. The SDK is synchronous and not known
  to be thread-safe per Filespace, and this indexer's own Client means a crawl
  never contends with the file service's `_registry_lock`.

Settings (which workspaces exist, their tokens) come from the vault, imported
lazily because docker/vault.py is owned by the file service and may be absent
in tests.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional, Protocol

logger = logging.getLogger("indexer.source")


@dataclass
class Entry:
    name: str
    type: str          # "file" | "dir" | "link"
    size: int
    mtime: int
    file_id: str


class SourceError(Exception):
    """The filespace could not be reached at all (no token, link failed)."""


class FilespaceSource(ABC):
    @abstractmethod
    def list_dir(self, path: str) -> list[Entry]:
        ...

    @abstractmethod
    def read_file(self, path: str) -> bytes:
        ...

    @abstractmethod
    def stat(self, path: str) -> Optional[Entry]:
        """Metadata for `path`, or None when it does not exist."""


class SourceProvider(ABC):
    @abstractmethod
    def lease(self, workspace_id: str, filespace: str):
        """Context manager yielding a FilespaceSource pinned for one job."""

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- settings
class SettingsProvider(Protocol):
    def workspaces(self) -> list[dict]: ...
    def active_workspace_id(self) -> Optional[str]: ...
    def token_for(self, workspace_id: Optional[str]) -> Optional[str]: ...


class VaultSettings:
    """SettingsProvider over docker/vault.py (`load`, `workspaces`, `token_for`)."""

    def _vault(self):
        import vault  # lazy: owned by the file service, only needs VAULT_KEY + /data
        return vault

    def workspaces(self) -> list[dict]:
        return list(self._vault().workspaces() or [])

    def active_workspace_id(self) -> Optional[str]:
        payload = self._vault().load() or {}
        return payload.get("activeWorkspaceId") or None

    def token_for(self, workspace_id: Optional[str]) -> Optional[str]:
        return self._vault().token_for(workspace_id)


def configured_filespaces(workspace: dict) -> list[str]:
    """Filespace names a vault workspace lists: `filespaces` plus `filespace`."""
    names = list(workspace.get("filespaces") or [])
    if workspace.get("filespace"):
        names.append(workspace["filespace"])
    return [n for n in dict.fromkeys(names) if n]


# ---------------------------------------------------------------- SDK
def _release(fn, what: str) -> None:
    try:
        fn()
    except Exception:
        logger.warning("teardown failed: %s", what, exc_info=True)


class _Link:
    def __init__(self, filespace_id: str, filespace):
        self.filespace_id = filespace_id
        self.filespace = filespace
        self.lock = threading.Lock()
        self.refs = 0


class _SdkSession:
    """One logged-in Client and the filespaces linked beneath it."""

    _NAMES_TTL = 60.0

    def __init__(self, token: str):
        import lucidlink
        self.client = lucidlink.Client()  # SANDBOXED storage; own temp root
        self.client.login(lucidlink.ServiceAccountCredentials(token=token))
        # Service-account tokens are workspace-scoped, so there is exactly one.
        self.workspace = self.client.get_workspace(self.client.list_workspaces()[0].id)
        self.links: "OrderedDict[str, _Link]" = OrderedDict()
        self._names: tuple[float, list] = (0.0, [])

    def resolve(self, ref: str) -> str:
        """Filespace name (or id) -> id. Names are mutable; we link by id."""
        expires, items = self._names
        if expires < time.monotonic():
            items = [(fi.id, fi.name) for fi in self.workspace.list_filespaces()]
            self._names = (time.monotonic() + self._NAMES_TTL, items)
        for fid, name in items:
            if ref in (fid, name):
                return fid
        raise SourceError(f"filespace '{ref}' not found in workspace")

    def busy(self) -> bool:
        return any(link.refs for link in self.links.values())

    def close(self) -> None:
        for fid, link in list(self.links.items()):
            _release(link.filespace.unlink, f"unlink filespace {fid}")
        self.links.clear()
        _release(self.client.close, "close client")


class SdkFilespaceSource(FilespaceSource):
    def __init__(self, fs):
        self._fs = fs

    @staticmethod
    def _entry(e) -> Entry:
        return Entry(name=e.name, type=e.type, size=int(e.size or 0),
                     mtime=int(e.mtime or 0), file_id=str(e.file_id))

    def list_dir(self, path: str) -> list[Entry]:
        return [self._entry(e) for e in self._fs.read_dir(path)]

    def read_file(self, path: str) -> bytes:
        return self._fs.read_file(path)

    def stat(self, path: str) -> Optional[Entry]:
        try:
            e = self._fs.get_entry(path)
        except FileNotFoundError:
            return None
        entry = self._entry(e)
        entry.name = entry.name or path.rsplit("/", 1)[-1]
        return entry


class SdkSourceProvider(SourceProvider):
    def __init__(self, settings: SettingsProvider, max_clients: int = 2, max_links: int = 2):
        self._settings = settings
        self._max_clients = max(1, max_clients)
        self._max_links = max(1, max_links)
        self._lock = threading.Lock()
        self._sessions: "OrderedDict[str, _SdkSession]" = OrderedDict()

    def _session(self, token: str) -> _SdkSession:
        session = self._sessions.get(token)
        if session is None:
            session = _SdkSession(token)
            self._sessions[token] = session
        self._sessions.move_to_end(token)
        # Evict idle clients beyond the bound (a rotated token leaves one behind).
        for old_token in list(self._sessions):
            if len(self._sessions) <= self._max_clients:
                break
            old = self._sessions[old_token]
            if old is not session and not old.busy():
                del self._sessions[old_token]
                old.close()
        return session

    def _link(self, session: _SdkSession, filespace: str) -> _Link:
        fid = session.resolve(filespace)
        link = session.links.get(fid)
        if link is None:
            link = _Link(fid, session.workspace.link_filespace(id=fid))
            session.links[fid] = link
        session.links.move_to_end(fid)
        for old_id in list(session.links):
            if len(session.links) <= self._max_links:
                break
            old = session.links[old_id]
            if old is not link and old.refs == 0:
                del session.links[old_id]
                _release(old.filespace.unlink, f"unlink filespace {old_id}")
        return link

    @contextmanager
    def lease(self, workspace_id: str, filespace: str) -> Iterator[FilespaceSource]:
        token = self._settings.token_for(workspace_id)
        if not token:
            raise SourceError(f"no LucidLink token in the vault for workspace '{workspace_id}'")
        with self._lock:
            try:
                link = self._link(self._session(token), filespace)
            except SourceError:
                raise
            except Exception as e:
                raise SourceError(f"could not link filespace '{filespace}': {e}") from e
            link.refs += 1
        try:
            with link.lock:
                yield SdkFilespaceSource(link.filespace.fs)
        finally:
            with self._lock:
                link.refs -= 1

    def close(self) -> None:
        with self._lock:
            for session in self._sessions.values():
                session.close()
            self._sessions.clear()


# ---------------------------------------------------------------- in-memory fake
class MemorySource(FilespaceSource):
    """A filespace held in a dict, with stable file ids and failure injection.

    `reads` / `lists` count calls so tests can assert a rescan did no I/O;
    `fail_dirs` makes listing those directories raise.
    """

    def __init__(self):
        self.files: dict[str, dict] = {}   # path -> {data, mtime, file_id}
        self.dirs: set[str] = {"/"}
        self.fail_dirs: set[str] = set()
        self.fail_reads: set[str] = set()
        self.reads = 0
        self.lists = 0
        self._clock = 1_700_000_000

    @staticmethod
    def _parent(path: str) -> str:
        return path.rsplit("/", 1)[0] or "/"

    def _ensure_parents(self, path: str) -> None:
        p = self._parent(path)
        while p not in self.dirs:
            self.dirs.add(p)
            p = self._parent(p)

    def tick(self) -> int:
        self._clock += 10
        return self._clock

    def write(self, path: str, data, mtime: Optional[int] = None) -> str:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._ensure_parents(path)
        cur = self.files.get(path)
        fid = cur["file_id"] if cur else uuid.uuid4().hex
        self.files[path] = {"data": data, "mtime": mtime if mtime is not None else self.tick(), "file_id": fid}
        return fid

    def touch(self, path: str, mtime: Optional[int] = None) -> None:
        self.files[path]["mtime"] = mtime if mtime is not None else self.tick()

    def mkdir(self, path: str) -> None:
        self._ensure_parents(path + "/x")

    def move(self, src: str, dst: str) -> None:
        if src in self.files:
            self._ensure_parents(dst)
            self.files[dst] = self.files.pop(src)
            return
        prefix = src.rstrip("/") + "/"
        for p in [p for p in self.files if p.startswith(prefix)]:
            np_ = dst.rstrip("/") + "/" + p[len(prefix):]
            self._ensure_parents(np_)
            self.files[np_] = self.files.pop(p)
        self.dirs = {d for d in self.dirs if d != src and not d.startswith(prefix)}
        self.mkdir(dst)

    def delete(self, path: str) -> None:
        if path in self.files:
            del self.files[path]
            return
        prefix = path.rstrip("/") + "/"
        for p in [p for p in self.files if p.startswith(prefix)]:
            del self.files[p]
        self.dirs = {d for d in self.dirs if d != path and not d.startswith(prefix)}

    def list_dir(self, path: str) -> list[Entry]:
        self.lists += 1
        if path in self.fail_dirs:
            raise OSError(f"listing {path} failed (injected)")
        if path not in self.dirs:
            raise FileNotFoundError(path)
        out = []
        for d in sorted(self.dirs):
            if d != "/" and self._parent(d) == path:
                out.append(Entry(d.rsplit("/", 1)[-1], "dir", 0, 0, "dir:" + d))
        for p, f in sorted(self.files.items()):
            if self._parent(p) == path:
                out.append(Entry(p.rsplit("/", 1)[-1], "file", len(f["data"]), f["mtime"], f["file_id"]))
        return out

    def read_file(self, path: str) -> bytes:
        self.reads += 1
        if path in self.fail_reads:
            raise OSError(f"reading {path} failed (injected)")
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]["data"]

    def stat(self, path: str) -> Optional[Entry]:
        f = self.files.get(path)
        if f is not None:
            return Entry(path.rsplit("/", 1)[-1], "file", len(f["data"]), f["mtime"], f["file_id"])
        if path in self.dirs:
            return Entry(path.rsplit("/", 1)[-1], "dir", 0, 0, "dir:" + path)
        return None


class MemorySourceProvider(SourceProvider):
    def __init__(self, sources: Optional[dict[tuple[str, str], MemorySource]] = None):
        self.sources = sources or {}

    def get(self, workspace_id: str, filespace: str) -> MemorySource:
        return self.sources.setdefault((workspace_id, filespace), MemorySource())

    @contextmanager
    def lease(self, workspace_id: str, filespace: str) -> Iterator[FilespaceSource]:
        yield self.get(workspace_id, filespace)


class StaticSettings:
    """SettingsProvider from a literal vault-shaped payload (tests, local runs)."""

    def __init__(self, payload: dict):
        self.payload = payload

    def workspaces(self) -> list[dict]:
        return list(self.payload.get("workspaces") or [])

    def active_workspace_id(self) -> Optional[str]:
        return self.payload.get("activeWorkspaceId")

    def token_for(self, workspace_id: Optional[str]) -> Optional[str]:
        wid = workspace_id or self.active_workspace_id()
        for w in self.workspaces():
            if w.get("id") == wid:
                return w.get("token") or None
        return None
