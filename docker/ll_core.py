"""Shared LucidLink client plumbing for the file service.

Holds the per-token Client registry, the linked-filespace cache, authentication,
and the `_with_fs` / `_with_filespace` / `_with_workspace` helpers that every
route module builds on. Split out of lucidlink_api.py so feature routers (files,
connect, insights) can share it without a circular import.

SDK >=0.14. Public surface (used by lucidlink_api / connect_api / insights_api)
is unchanged: require_token, require_token_and_filespace, _with_fs,
_with_filespace, _with_workspace, _list_filespaces_cached.
"""
from fastapi import HTTPException, Header, Depends
import lucidlink
import os
import time
import logging
from collections import OrderedDict
from threading import Lock
from typing import Optional

logger = logging.getLogger("lucidlink_api")

# `lucidlink.Client` replaces the deprecated `Daemon`/`create_daemon()`. Two
# behaviours of the old model drove the previous design and no longer apply:
#
#   1. Daemon carried a single-instance-per-process guard (C++ global state).
#      Client has none — concurrent clients are supported, given a distinct
#      StorageConfig each (SANDBOXED, the default, allocates its own temp root).
#   2. `daemon.authenticate()` called `_stop_active_workspace()` first, and
#      `Workspace.stop()` unlinks every linked filespace. So re-authenticating
#      to list filespaces silently tore down the hot link while the cache went
#      on holding the dead handle. `Client.login()` is idempotent for the same
#      token and never re-authenticates, which removes that failure mode.
#
# Each level of the registry costs real resources — a Client owns a sandbox
# cache directory, and each linked filespace runs a full client stack with its
# own disk cache (~1 GB by default) plus worker threads — so both levels are
# bounded LRUs whose eviction actually releases: `Filespace.unlink()` (which
# flushes pending writes first, under the default SYNC_ALL) and `Client.close()`.
#
# Floored at 1: a limit of 0 would evict the entry the caller is about to use.
MAX_CLIENTS = max(1, int(os.environ.get("LUCIDLINK_MAX_CLIENTS", "1")))
MAX_LINKS_PER_CLIENT = max(1, int(os.environ.get("LUCIDLINK_MAX_LINKED_FILESPACES", "2")))

# One lock guards the registry and the filespace call, preserving the request
# serialization this service has always had. Narrowing it to registry operations
# alone needs per-link refcounting, so an eviction cannot pull a filespace out
# from under an in-flight request; that is a separate change.
_registry_lock = Lock()
_clients: "OrderedDict[str, _Session]" = OrderedDict()

_FS_LIST_TTL = 45.0
_fs_list_cache: dict[str, tuple[float, list]] = {}
_cache_lock = Lock()


def _release(fn, what: str):
    """Run a teardown step, logging failures instead of discarding them.

    Teardown must never abort the caller, but it must be visible: a bare
    `except Exception: pass` here is how the SDK's `unlink_filespace()`
    signature change could have gone unnoticed.
    """
    try:
        fn()
    except Exception:
        logger.warning("teardown failed: %s", what, exc_info=True)


class _Session:
    """One logged-in Client plus the filespaces linked beneath it.

    A Client is bound to one token for its lifetime — `login()` with different
    credentials raises — so the registry keys Sessions by token.
    """

    def __init__(self, token: str):
        self.client = lucidlink.Client()  # SANDBOXED storage; own temp root
        self.client.login(lucidlink.ServiceAccountCredentials(token=token))
        # Service-account tokens are workspace-scoped, so there is exactly one.
        self.workspace = self.client.get_workspace(self.client.list_workspaces()[0].id)
        self.links: "OrderedDict[str, object]" = OrderedDict()

    def link(self, filespace_id: str):
        """Link by id and return the Filespace. Idempotent; LRU-bounded."""
        existing = self.links.get(filespace_id)
        if existing is not None:
            self.links.move_to_end(filespace_id)
            return existing

        filespace = self.workspace.link_filespace(id=filespace_id)
        self.links[filespace_id] = filespace
        while len(self.links) > MAX_LINKS_PER_CLIENT:
            old_id, old = self.links.popitem(last=False)
            _release(lambda: old.unlink(), f"unlink filespace {old_id}")
        return filespace

    def close(self):
        for filespace_id, filespace in list(self.links.items()):
            _release(lambda f=filespace: f.unlink(), f"unlink filespace {filespace_id}")
        self.links.clear()
        _release(self.client.close, "close client")


def _extract_token(authorization: Optional[str], x_lucid_token: Optional[str]) -> Optional[str]:
    """Accept either `Authorization: Bearer <token>` or `X-LucidLink-Token: <token>`."""
    if x_lucid_token:
        return x_lucid_token
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1]
        return authorization
    return None


def _auth_error(e: Exception) -> HTTPException:
    msg = str(e)
    if "401" in msg or "Unauthorized" in msg or "Invalid token" in msg:
        return HTTPException(status_code=401, detail=f"LucidLink auth failed: {msg}")
    if "403" in msg or "Forbidden" in msg:
        return HTTPException(status_code=403, detail=f"LucidLink forbidden: {msg}")
    return HTTPException(status_code=502, detail=f"LucidLink upstream error: {msg}")


def _require_token(authorization: Optional[str], x_lucid_token: Optional[str]) -> str:
    token = _extract_token(authorization, x_lucid_token)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization: Bearer or X-LucidLink-Token header",
        )
    return token


def require_token(
    authorization: Optional[str] = Header(default=None),
    x_lucid_token: Optional[str] = Header(default=None, alias="X-LucidLink-Token"),
) -> str:
    return _require_token(authorization, x_lucid_token)


def require_token_and_filespace(
    authorization: Optional[str] = Header(default=None),
    x_lucid_token: Optional[str] = Header(default=None, alias="X-LucidLink-Token"),
    x_lucid_filespace: Optional[str] = Header(default=None, alias="X-LucidLink-Filespace"),
) -> tuple[str, str]:
    token = _require_token(authorization, x_lucid_token)
    if not x_lucid_filespace:
        raise HTTPException(status_code=400, detail="Missing X-LucidLink-Filespace header")
    return token, x_lucid_filespace


def _get_session(token: str) -> "_Session":
    """Return this token's Session, creating it if needed.

    Caller MUST hold `_registry_lock`.
    """
    session = _clients.get(token)
    if session is not None:
        _clients.move_to_end(token)
        return session

    try:
        session = _Session(token)
    except Exception as e:
        raise _auth_error(e)

    _clients[token] = session
    while len(_clients) > MAX_CLIENTS:
        _, evicted = _clients.popitem(last=False)
        evicted.close()
    return session


def _with_workspace(token: str, fn):
    """Run `fn(workspace)` against this token's logged-in workspace."""
    with _registry_lock:
        return fn(_get_session(token).workspace)


def _resolve_filespace_id(token: str, ref: str) -> str:
    """Map a filespace name (or id) to a stable filespace id.

    `link_filespace(name=...)` is deprecated upstream because names are mutable:
    after a rename a caller silently links to nothing, or to the wrong
    filespace. The HTTP API still accepts a name for compatibility; it is
    resolved here, through the existing list cache, and everything below this
    line works in ids.

    Must NOT be called while holding `_registry_lock` — the lookup takes it.
    """
    for entry in _list_filespaces_cached(token):
        if entry["id"] == ref or entry["name"] == ref:
            return entry["id"]
    raise HTTPException(status_code=404, detail=f"Filespace '{ref}' not found")


def _with_filespace(token: str, filespace_name: str, fn):
    """Run `fn(filespace)` with a live linked Filespace object (for `.connect`,
    `.fs.get_size()`, etc.). Serialized via _registry_lock."""
    filespace_id = _resolve_filespace_id(token, filespace_name)
    with _registry_lock:
        session = _get_session(token)
        try:
            filespace = session.link(filespace_id)
        except HTTPException:
            raise
        except Exception as e:
            raise _auth_error(e)
        return fn(filespace)


def _with_fs(token: str, filespace_name: str, fn):
    """Run `fn(fs)` with the linked Filesystem (the common case)."""
    return _with_filespace(token, filespace_name, lambda filespace: fn(filespace.fs))


def _list_filespaces_cached(token: str) -> list[dict]:
    now = time.monotonic()
    with _cache_lock:
        cached = _fs_list_cache.get(token)
        if cached and cached[0] > now:
            return cached[1]
    items = _with_workspace(token, lambda ws: [
        {"id": fi.id, "name": fi.name, "created": fi.created}
        for fi in ws.list_filespaces()
    ])
    with _cache_lock:
        _fs_list_cache[token] = (now + _FS_LIST_TTL, items)
    return items


def registry_stats() -> dict:
    """Live registry counts, for /version."""
    with _registry_lock:
        return {
            "clients": len(_clients),
            "linked_filespaces": sum(len(s.links) for s in _clients.values()),
            "limits": {
                "max_clients": MAX_CLIENTS,
                "max_links_per_client": MAX_LINKS_PER_CLIENT,
            },
        }


def shutdown():
    """Release every link on the way out so pending writes are flushed."""
    with _registry_lock:
        for _, session in list(_clients.items()):
            session.close()
        _clients.clear()
