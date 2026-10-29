"""Encrypted settings vault (docs/INDEXER.md, "Vault").

Holds the owner's LucidLink service-account tokens (one per workspace) and the
synced Favorites server-side, so a signed-in browser never needs to keep a
LucidLink token, and the indexer can crawl without a browser open.

MinIO profiles are retired (Simplification phase 4): the settings payload no
longer carries `minioProfiles`/`activeMinioProfileId`, a PUT that still sends
them has them silently dropped, and any stored copy is purged from the
encrypted row on the next write or by `purge_retired()`, which the file service
runs once at startup and before its first vault read.

Storage is one row in SQLite at `VAULT_PATH` (default `/data/vault.db`): the
whole payload as Fernet(`VAULT_KEY`)-encrypted JSON. The file service is the
only writer; the indexer (another process, same volume) reads. WAL mode plus
short transactions keep a read from ever blocking on a write, and a save does
its read-merge-write inside one `BEGIN IMMEDIATE` so two saves cannot lose
each other's changes.

Tokens never leave this module unmasked except through `token_for()` (and
`load()`/`workspaces()` for trusted in-process callers). HTTP responses always
go through `masked()`.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Body, Depends, HTTPException

import owner_auth

logger = logging.getLogger("vault")

MASK = "••••"
_TOP_KEYS = ("workspaces", "activeWorkspaceId")
# Retired in Simplification phase 4 (in-app MinIO console removed). Never
# returned; dropped from incoming PUTs; purged from the stored row.
_RETIRED_KEYS = ("minioProfiles", "activeMinioProfileId")
# Stored alongside the settings in the same encrypted row, but served by its own
# routes (`/vault/favorites`), never in the settings payload.
_FAVORITES_KEY = "favorites"
MAX_FAVORITES = 200
_FAVORITE_FIELDS = ("id", "workspaceId", "filespace", "kind", "path", "label", "addedAt")
_FAVORITE_KINDS = ("lucidlink", "block")
# `sa_live:` style prefixes are shown so the owner can tell token kinds apart.
_PREFIX = re.compile(r"^([A-Za-z0-9_-]{1,24}:)")


class VaultError(RuntimeError):
    """Vault unusable (not configured, or cannot be decrypted). Maps to 503."""


class VaultNotConfigured(VaultError):
    pass


def _empty() -> dict:
    return {"workspaces": [], "activeWorkspaceId": None}


def _path() -> str:
    return os.environ.get("VAULT_PATH", "/data/vault.db")


def _fernet() -> Fernet:
    key = os.environ.get("VAULT_KEY", "").strip()
    if not key:
        raise VaultNotConfigured("Vault is not configured (set VAULT_KEY)")
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        raise VaultNotConfigured("Vault is not configured (VAULT_KEY is not a valid Fernet key)")


def is_configured() -> bool:
    try:
        _fernet()
        return True
    except VaultNotConfigured:
        return False


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = _path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # isolation_level=None: we issue BEGIN ourselves so transactions stay short
    # and explicit. busy_timeout covers the rare overlap with the other process.
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS vault ("
            " id INTEGER PRIMARY KEY CHECK (id = 1),"
            " ciphertext BLOB NOT NULL,"
            " updated_at TEXT NOT NULL)"
        )
        yield conn
    finally:
        conn.close()


def _decrypt_raw(f: Fernet, blob: bytes) -> dict:
    """The stored JSON exactly as written, retired keys included."""
    try:
        data = json.loads(f.decrypt(bytes(blob)))
    except InvalidToken:
        raise VaultError("Vault cannot be decrypted with the configured VAULT_KEY")
    if not isinstance(data, dict):
        raise VaultError("Vault payload is corrupt")
    return data


def _decrypt(f: Fernet, blob: bytes) -> dict:
    """The stored payload normalised to the current shape (retired keys dropped)."""
    return _normalise(_decrypt_raw(f, blob))


def _normalise(data: dict) -> dict:
    out = _empty()
    out.update({k: data.get(k, out[k]) for k in _TOP_KEYS})
    favs = data.get(_FAVORITES_KEY)
    if isinstance(favs, list):
        out[_FAVORITES_KEY] = favs
    return out


def load_record() -> tuple[dict, Optional[str]]:
    """(payload, updatedAt ISO or None). Payload is unmasked."""
    f = _fernet()
    with _connect() as conn:
        row = conn.execute("SELECT ciphertext, updated_at FROM vault WHERE id = 1").fetchone()
    if row is None:
        return _empty(), None
    return _decrypt(f, row[0]), row[1]


def load() -> dict:
    """The full, unmasked payload. In-process use only; never return it over HTTP."""
    return load_record()[0]


def has_settings() -> bool:
    """Whether a row exists. Does not need the key."""
    if not os.path.exists(_path()):
        return False
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM vault WHERE id = 1").fetchone() is not None


# ---------------------------------------------------------------- masking
def mask_token(token: Optional[str]) -> str:
    if not token:
        return ""
    m = _PREFIX.match(token)
    prefix = m.group(1) if m else ""
    rest = token[len(prefix):]
    # Showing the last 4 of a very short secret would reveal most of it.
    if len(rest) < 12:
        return prefix + MASK
    return f"{prefix}{MASK}{rest[-4:]}"


def masked(payload: dict) -> dict:
    """Copy of `payload` safe to return to the owner.

    Each workspace token becomes `<prefix>••••<last4>` and gains a `tokenSet`
    flag. Only the current settings keys are copied, so retired keys (MinIO
    profiles) and favorites never reach the response.
    """
    out = copy.deepcopy({k: payload.get(k, v) for k, v in _empty().items()})
    for ws in out.get("workspaces") or []:
        if isinstance(ws, dict):
            tok = ws.get("token")
            ws["token"] = mask_token(tok)
            ws["tokenSet"] = bool(tok)
    return out


# ---------------------------------------------------------------- merge + save
def _validate(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("Settings must be a JSON object")
    # Retired keys (minioProfiles, activeMinioProfileId) are not validated:
    # older app builds may still send them, and they are dropped unread.
    items = payload.get("workspaces")
    if items is not None:
        if not isinstance(items, list):
            raise ValueError("'workspaces' must be a list")
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                raise ValueError("Every entry in 'workspaces' needs a non-empty string 'id'")
            if item["id"] in seen:
                raise ValueError(f"Duplicate id '{item['id']}' in 'workspaces'")
            seen.add(item["id"])
            tok = item.get("token", "")
            if tok is not None and not isinstance(tok, str):
                raise ValueError("Workspace 'token' must be a string or null")
    active = payload.get("activeWorkspaceId")
    if active is not None and not isinstance(active, str):
        raise ValueError("'activeWorkspaceId' must be a string or null")


def _merge_token(incoming: dict, stored: Optional[dict]) -> Optional[str]:
    """Contract rules for one workspace token:

    - omitted, or containing `••••`          -> keep the stored value
    - `""` sent with `tokenSet: true`         -> keep (the masked form of a set token
                                                 was blanked by the client, not cleared)
    - `null`                                  -> clear
    - any other string                        -> replace
    """
    prev = (stored or {}).get("token") or ""
    if "token" not in incoming:
        return prev
    tok = incoming["token"]
    if tok is None:
        return ""
    if MASK in tok:
        return prev
    if tok == "" and incoming.get("tokenSet"):
        return prev
    return tok


def merge(stored: dict, incoming: dict) -> dict:
    """Apply an owner PUT to the stored payload. Pure; used by `save()`.

    The incoming `workspaces` list is the new list (order and membership), with
    tokens merged by workspace id. A top-level key the client omits keeps its
    stored value. Retired keys (MinIO profiles) in `incoming` are ignored, and
    any left in `stored` are dropped from the result.
    """
    _validate(incoming)
    out = copy.deepcopy(stored)
    for key in _RETIRED_KEYS:
        out.pop(key, None)
    if "workspaces" in incoming and incoming["workspaces"] is not None:
        by_id = {w.get("id"): w for w in stored.get("workspaces") or [] if isinstance(w, dict)}
        merged = []
        for ws in incoming["workspaces"]:
            new = {k: v for k, v in ws.items() if k != "tokenSet"}
            new["token"] = _merge_token(ws, by_id.get(ws["id"]))
            merged.append(new)
        out["workspaces"] = merged
    elif "workspaces" in incoming:
        out["workspaces"] = []
    if "activeWorkspaceId" in incoming:
        out["activeWorkspaceId"] = incoming["activeWorkspaceId"]
    return out


def _update(fn) -> tuple[dict, str]:
    """Read-modify-write the stored payload in one `BEGIN IMMEDIATE`.

    `fn(stored) -> new` must be pure. Returns (new payload, updatedAt).
    """
    f = _fernet()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT ciphertext FROM vault WHERE id = 1").fetchone()
            stored = _decrypt(f, row[0]) if row else _empty()
            new = fn(stored)
            updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            blob = f.encrypt(json.dumps(new, separators=(",", ":")).encode("utf-8"))
            conn.execute(
                "INSERT INTO vault (id, ciphertext, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET ciphertext = excluded.ciphertext, "
                "updated_at = excluded.updated_at",
                (blob, updated_at),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return new, updated_at


def save(payload: dict) -> dict:
    """Merge `payload` into the vault and return the masked result + `updatedAt`.

    Favorites stored in the same row are carried over untouched.
    """
    new, updated_at = _update(lambda stored: merge(stored, payload))
    return {**masked(new), "updatedAt": updated_at}


# ---------------------------------------------------------------- migration
_migrated: set[str] = set()


def purge_retired() -> int:
    """Re-encrypt the stored row without retired keys (MinIO profiles).

    One-time migration for vaults written before Simplification phase 4.
    Workspaces, tokens, favorites and `updatedAt` are kept exactly as stored.
    Returns how many MinIO profiles were removed (0 when there was nothing to
    purge, or no row). Logs that count only, never any value.
    """
    f = _fernet()
    if not os.path.exists(_path()):
        return 0
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT ciphertext FROM vault WHERE id = 1").fetchone()
            raw = _decrypt_raw(f, row[0]) if row else {}
            if not any(k in raw for k in _RETIRED_KEYS):
                conn.execute("ROLLBACK")
                return 0
            profiles = raw.get("minioProfiles")
            count = len(profiles) if isinstance(profiles, list) else 0
            kept = {k: v for k, v in raw.items() if k not in _RETIRED_KEYS}
            blob = f.encrypt(json.dumps(kept, separators=(",", ":")).encode("utf-8"))
            conn.execute("UPDATE vault SET ciphertext = ? WHERE id = 1", (blob,))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    logger.info("vault: purged %d retired MinIO profile(s) from stored settings", count)
    return count


def ensure_migrated() -> None:
    """Run `purge_retired()` once per vault path per process (file service only:
    it is the vault's writer). Quietly does nothing when the vault is not
    configured; a decrypt failure surfaces on the read that follows."""
    path = _path()
    if path in _migrated:
        return
    try:
        purge_retired()
    except VaultError:
        return
    _migrated.add(path)


# ---------------------------------------------------------------- favorites
def validate_favorites(body: Any) -> list[dict]:
    """`{"favorites": [Favorite]}` -> the clean list to store. ValueError when malformed.

    Unknown fields are dropped; `label` is optional; `path` must start with `/`.
    Favorites hold no secrets, so they are stored and returned as they are.
    """
    if not isinstance(body, dict) or not isinstance(body.get("favorites"), list):
        raise ValueError("Body must be {\"favorites\": [...]}")
    items = body["favorites"]
    if len(items) > MAX_FAVORITES:
        raise ValueError(f"At most {MAX_FAVORITES} favorites")
    out: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"Favorite {i} must be an object")
        for key in ("id", "workspaceId", "filespace", "path", "addedAt"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f"Favorite {i} needs a non-empty string '{key}'")
        if item.get("kind") not in _FAVORITE_KINDS:
            raise ValueError(f"Favorite {i} 'kind' must be 'lucidlink' or 'block'")
        if not item["path"].startswith("/"):
            raise ValueError(f"Favorite {i} 'path' must start with '/'")
        label = item.get("label")
        if label is not None and not isinstance(label, str):
            raise ValueError(f"Favorite {i} 'label' must be a string")
        if item["id"] in seen:
            raise ValueError(f"Duplicate favorite id '{item['id']}'")
        seen.add(item["id"])
        out.append({k: item[k] for k in _FAVORITE_FIELDS if k in item and item[k] is not None})
    return out


def load_favorites() -> list[dict]:
    """The stored Favorites; an empty list for vaults saved before favorites existed."""
    favs = load().get(_FAVORITES_KEY)
    return copy.deepcopy(favs) if isinstance(favs, list) else []


def save_favorites(body: Any) -> list[dict]:
    """Validate and replace the stored Favorites; returns the stored list."""
    favs = validate_favorites(body)
    _update(lambda stored: {**stored, _FAVORITES_KEY: favs})
    return copy.deepcopy(favs)


# ---------------------------------------------------------------- lookups
def workspaces() -> list[dict]:
    """Stored workspaces, unmasked (tokens included). In-process callers only."""
    return copy.deepcopy(load().get("workspaces") or [])


def _pick_workspace(payload: dict, workspace_id: Optional[str]) -> Optional[dict]:
    items = [w for w in payload.get("workspaces") or [] if isinstance(w, dict)]
    if workspace_id:
        # An explicit id that is not in the vault does NOT fall back to the
        # active workspace: acting on a different workspace than the one asked
        # for is worse than refusing.
        return next((w for w in items if w.get("id") == workspace_id), None)
    active = payload.get("activeWorkspaceId")
    if active:
        found = next((w for w in items if w.get("id") == active), None)
        if found is not None:
            return found
    return items[0] if len(items) == 1 else None


def token_for(workspace_id: Optional[str]) -> Optional[str]:
    """The LucidLink token for `workspace_id` (or the active workspace when None).

    None when the workspace is unknown or has no token. Raises VaultError when
    the vault is not configured or cannot be decrypted.
    """
    ws = _pick_workspace(load(), workspace_id)
    tok = (ws or {}).get("token")
    return tok or None


def resolve(workspace_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(resolved workspace id, token) in one read; the id `token_for` would use."""
    ws = _pick_workspace(load(), workspace_id) or {}
    return ws.get("id"), (ws.get("token") or None)


# ---------------------------------------------------------------- HTTP
router = APIRouter(prefix="/vault", tags=["vault"])


def _vault_http_error(e: VaultError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(e))


@router.get("/status")
def vault_status():
    """Unauthenticated: lets the web app decide whether to offer owner sign-in."""
    try:
        has = has_settings()
    except Exception:
        has = False
    return {"configured": is_configured(),
            "ownerConfigured": owner_auth.owner_configured(),
            "hasSettings": has}


@router.get("/settings")
def get_settings(_owner: owner_auth.Owner = Depends(owner_auth.require_owner)):
    try:
        ensure_migrated()
        payload, updated_at = load_record()
    except VaultError as e:
        raise _vault_http_error(e)
    return {**masked(payload), "updatedAt": updated_at}


@router.put("/settings")
def put_settings(body: Any = Body(...),
                 _owner: owner_auth.Owner = Depends(owner_auth.require_owner)):
    try:
        return save(body)
    except VaultError as e:
        raise _vault_http_error(e)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/favorites")
def get_favorites(_owner: owner_auth.Owner = Depends(owner_auth.require_owner)):
    try:
        ensure_migrated()
        return {"favorites": load_favorites()}
    except VaultError as e:
        raise _vault_http_error(e)


@router.put("/favorites")
def put_favorites(body: Any = Body(...),
                  _owner: owner_auth.Owner = Depends(owner_auth.require_owner)):
    try:
        return {"favorites": save_favorites(body)}
    except VaultError as e:
        raise _vault_http_error(e)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
