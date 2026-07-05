"""Shared LucidLink daemon plumbing for the file service.

Holds the process-wide daemon, the single-active-link cache, authentication, and
the `_with_fs` / `_with_filespace` / `_with_workspace` helpers that every route
module builds on. Split out of lucidlink_api.py so feature routers (files,
connect, insights) can share it without a circular import.
"""
from fastapi import HTTPException, Header, Depends
import lucidlink
import time
import logging
from threading import Lock
from typing import Optional

logger = logging.getLogger("lucidlink_api")

# Daemon is process-wide and does NOT need a token to start.
# NOTE: create_daemon() is deprecated in SDK >=0.12 in favour of lucidlink.Client,
# but still supported; kept here to minimise churn on the working service.
daemon = lucidlink.create_daemon()
daemon.start()

# Single-active-link cache. link_filespace() in SDK >=0.12 supports multiple
# concurrent links and is idempotent, but this service keeps one hot link and
# reuses it while the (token, filespace) pair is unchanged, avoiding an expensive
# authenticate+link round-trip (~5s) on every request. On a cache miss the
# previously linked filespace is torn down with Filespace.unlink() (the old
# no-arg daemon.unlink_filespace() now requires the handle and is deprecated).
_daemon_lock = Lock()
_current_link: dict = {"token": None, "filespace": None, "fs": None, "fsobj": None}

_FS_LIST_TTL = 45.0
_fs_list_cache: dict[str, tuple[float, list]] = {}
_cache_lock = Lock()


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


def _authenticate(token: str):
    """Fresh authenticate. Caller MUST hold _daemon_lock."""
    credentials = lucidlink.ServiceAccountCredentials(token=token)
    return daemon.authenticate(credentials)


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


def _ensure_link(token: str, filespace_name: str):
    """Return a live linked Filespace for (token, filespace_name). Caller MUST
    hold _daemon_lock. Reuses the cached link on a hit; on a miss unlinks the
    previous filespace (Filespace.unlink()) then authenticates + relinks."""
    if (
        _current_link["token"] == token
        and _current_link["filespace"] == filespace_name
        and _current_link["fsobj"] is not None
    ):
        return _current_link["fsobj"]
    # Cache miss: unlink the previously linked filespace (if any) before relinking.
    if _current_link["fsobj"] is not None:
        try:
            _current_link["fsobj"].unlink()
        except Exception:
            pass
    try:
        workspace = _authenticate(token)
        filespace = workspace.link_filespace(name=filespace_name)
        _current_link.update(
            {"token": token, "filespace": filespace_name, "fs": filespace.fs, "fsobj": filespace}
        )
        return filespace
    except Exception:
        _current_link.update({"token": None, "filespace": None, "fs": None, "fsobj": None})
        raise


def _with_filespace(token: str, filespace_name: str, fn):
    """Run `fn(filespace)` with a live linked Filespace object (for `.connect`,
    `.fs.get_size()`, etc.). Serialized via _daemon_lock."""
    with _daemon_lock:
        try:
            filespace = _ensure_link(token, filespace_name)
        except Exception as e:
            raise _auth_error(e)
        return fn(filespace)


def _with_fs(token: str, filespace_name: str, fn):
    """Run `fn(fs)` with the linked Filesystem (the common case)."""
    return _with_filespace(token, filespace_name, lambda filespace: fn(filespace.fs))


def _with_workspace(token: str, fn):
    """Run `fn(workspace)` with a freshly authenticated workspace."""
    with _daemon_lock:
        try:
            workspace = _authenticate(token)
        except Exception as e:
            raise _auth_error(e)
        return fn(workspace)


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
