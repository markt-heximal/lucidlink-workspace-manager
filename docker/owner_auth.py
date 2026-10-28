"""Owner sign-in for a single-owner deployment (docs/INDEXER.md, "Owner auth").

The web app already signs its user in with Supabase. Rather than validating the
JWT locally (which would tie this service to however the Supabase project signs
tokens: HS256 secret, rotating asymmetric keys, ...), we ask Supabase who the
bearer is via `GET {SUPABASE_URL}/auth/v1/user` and accept exactly one account:
`OWNER_EMAIL` and/or `OWNER_USER_ID`.

Fail closed: with no owner configured, or Supabase env missing, nobody is the
owner. A Supabase outage is reported as 503, not 401, so the browser does not
mistake "can't check right now" for "signed out".

Results are cached by sha256(token) — never the raw token — for 60 s when
accepted and briefly when rejected, so a page that fires a dozen requests does
not fire a dozen Supabase round-trips.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from threading import Lock
from typing import Optional

import httpx
from fastapi import Header, HTTPException

logger = logging.getLogger("lucidlink_api.owner_auth")

POSITIVE_TTL = 60.0
NEGATIVE_TTL = 10.0
_CACHE_MAX = 256
SUPABASE_TIMEOUT = httpx.Timeout(5.0, connect=3.0)

# Tests swap this for an httpx.MockTransport; production uses the default.
_transport: Optional[httpx.BaseTransport] = None

_B64URL = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")


@dataclass(frozen=True)
class Owner:
    id: str
    email: str


# sha256(token) -> (expires_at, Owner | None). None is a cached rejection.
_cache: dict[str, tuple[float, Optional[Owner]]] = {}
_cache_lock = Lock()


def is_jwt(token: Optional[str]) -> bool:
    """Three base64url segments, the header starting with `eyJ` (`{"`).

    LucidLink service-account tokens (`sa_live:key:secret`) never match, which
    is what lets one Authorization header carry either kind.
    """
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3 or not parts[0].startswith("eyJ"):
        return False
    # The signature segment may be empty for unsigned tokens; header and payload may not.
    if not parts[0] or not parts[1]:
        return False
    return all(_B64URL.match(p) for p in parts if p)


def _config() -> tuple[str, str, str, str]:
    return (
        os.environ.get("SUPABASE_URL", "").strip().rstrip("/"),
        os.environ.get("SUPABASE_ANON_KEY", "").strip(),
        os.environ.get("OWNER_EMAIL", "").strip().lower(),
        os.environ.get("OWNER_USER_ID", "").strip(),
    )


def owner_configured() -> bool:
    """True when owner sign-in can succeed at all (env complete)."""
    url, anon, email, user_id = _config()
    return bool(url and anon and (email or user_id))


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> tuple[bool, Optional[Owner]]:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return False, None
        if hit[0] <= now:
            _cache.pop(key, None)
            return False, None
        return True, hit[1]


def _cache_put(key: str, owner: Optional[Owner]) -> None:
    now = time.monotonic()
    ttl = POSITIVE_TTL if owner is not None else NEGATIVE_TTL
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            for k in [k for k, (exp, _) in _cache.items() if exp <= now]:
                del _cache[k]
            while len(_cache) >= _CACHE_MAX:
                _cache.pop(next(iter(_cache)))
        _cache[key] = (now + ttl, owner)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _not_owner() -> HTTPException:
    return HTTPException(status_code=401, detail="Not signed in as the owner")


def _match(user: dict, owner_email: str, owner_id: str) -> Optional[Owner]:
    uid = str(user.get("id") or "")
    email = str(user.get("email") or "")
    if owner_id and uid and uid == owner_id:
        return Owner(id=uid, email=email)
    # An email match also needs the address to be confirmed, so an unconfirmed
    # sign-up using the owner's address can never pass as the owner.
    confirmed = user.get("email_confirmed_at") or user.get("confirmed_at")
    if owner_email and email and email.lower() == owner_email and confirmed:
        return Owner(id=uid, email=email)
    return None


def verify_owner(bearer: str) -> Owner:
    """Return the Owner for this Supabase access token, or raise.

    401: not a valid session, or a valid session for someone else.
    503: owner auth not configured, or Supabase unreachable / erroring.
    """
    url, anon, owner_email, owner_id = _config()
    if not (owner_email or owner_id):
        raise HTTPException(status_code=503,
                            detail="Owner sign-in is not configured (set OWNER_EMAIL or OWNER_USER_ID)")
    if not (url and anon):
        raise HTTPException(status_code=503,
                            detail="Owner sign-in is not configured (set SUPABASE_URL and SUPABASE_ANON_KEY)")
    if not bearer:
        raise _not_owner()

    key = _cache_key(bearer)
    hit, cached = _cache_get(key)
    if hit:
        if cached is None:
            raise _not_owner()
        return cached

    try:
        with httpx.Client(timeout=SUPABASE_TIMEOUT, transport=_transport) as client:
            resp = client.get(f"{url}/auth/v1/user",
                              headers={"apikey": anon, "Authorization": f"Bearer {bearer}"})
    except httpx.HTTPError as e:
        # Deliberately not cached: the next request should retry.
        logger.warning("Supabase user lookup failed: %s", type(e).__name__)
        raise HTTPException(status_code=503, detail="Owner sign-in check unavailable (Supabase unreachable)")

    if resp.status_code in (401, 403):
        _cache_put(key, None)
        raise _not_owner()
    if resp.status_code >= 400:
        logger.warning("Supabase user lookup returned HTTP %s", resp.status_code)
        if resp.status_code >= 500 or resp.status_code == 429:
            raise HTTPException(status_code=503, detail="Owner sign-in check unavailable (Supabase error)")
        _cache_put(key, None)
        raise _not_owner()
    try:
        user = resp.json()
    except ValueError:
        raise HTTPException(status_code=503, detail="Owner sign-in check unavailable (bad Supabase response)")
    if not isinstance(user, dict):
        raise HTTPException(status_code=503, detail="Owner sign-in check unavailable (bad Supabase response)")

    owner = _match(user, owner_email, owner_id)
    _cache_put(key, owner)
    if owner is None:
        logger.info("rejected non-owner Supabase session")
        raise _not_owner()
    return owner


def bearer_from_header(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def require_owner(authorization: Optional[str] = Header(default=None)) -> Owner:
    """FastAPI dependency: `Authorization: Bearer <supabase-jwt>` of the owner.

    Sync on purpose: FastAPI runs it in the threadpool, so the blocking
    Supabase call never stalls the event loop.
    """
    token = bearer_from_header(authorization)
    if not token or not is_jwt(token):
        raise HTTPException(status_code=401, detail="Owner sign-in required (Authorization: Bearer <session token>)")
    return verify_owner(token)

