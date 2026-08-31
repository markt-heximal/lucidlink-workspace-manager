from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
import lucidlink, os, time, logging, mimetypes
from datetime import datetime, timezone
from threading import Lock
from typing import Optional
from collections import OrderedDict
from contextlib import asynccontextmanager

logger = logging.getLogger("lucidlink_api")

BOOT_TIME = datetime.now(timezone.utc)
BOOT_MONO = time.monotonic()

@asynccontextmanager
async def _lifespan(_app):
    yield
    _shutdown()


app = FastAPI(lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The SDK's `Client` (0.12.0+) replaces the deprecated `Daemon`. Unlike Daemon,
# it has no single-instance-per-process guard — the C++ global-state constraint
# that forced this service's old "one active workspace, re-authenticate every
# request" design is gone. A Client is bound to one token for its lifetime
# (`login()` with a different token raises), so we keep one Client per token and
# link filespaces beneath it.
#
# Both registries are bounded LRUs because each level costs real resources: a
# Client owns a sandbox cache directory, and each linked filespace runs a full
# client stack with its own disk cache (~1 GB default) plus worker threads.
# Eviction must therefore actually release — `Filespace.unlink()` (which flushes
# pending writes first, under the default SYNC_ALL) and `Client.close()`.
# Floored at 1: a limit of 0 would evict the entry the caller is about to use.
MAX_CLIENTS = max(1, int(os.environ.get("LUCIDLINK_MAX_CLIENTS", "1")))
MAX_LINKS_PER_CLIENT = max(1, int(os.environ.get("LUCIDLINK_MAX_LINKED_FILESPACES", "2")))

# One lock guards the registries *and* the filesystem call, which keeps the same
# request serialization this service has always had. Narrowing it to registry
# operations alone needs per-link refcounting so an eviction cannot pull a
# filespace out from under an in-flight request; that is a separate change.
_registry_lock = Lock()
_clients: "OrderedDict[str, _Session]" = OrderedDict()

_FS_LIST_TTL = 45.0
_fs_list_cache: dict[str, tuple[float, list]] = {}
_cache_lock = Lock()


class _Session:
    """One logged-in Client plus the filespaces linked beneath it."""

    def __init__(self, token: str):
        self.client = lucidlink.Client()  # SANDBOXED storage; distinct temp root per client
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


def _release(fn, what: str):
    """Run a teardown step, logging failures instead of discarding them.

    The predecessor of this function was a bare `except Exception: pass`, which
    is why a signature change in `unlink_filespace()` went unnoticed across four
    SDK releases. Teardown must never abort the caller, but it must be visible.
    """
    try:
        fn()
    except Exception:
        logger.warning("teardown failed: %s", what, exc_info=True)


def _extract_token(authorization: Optional[str], x_lucid_token: Optional[str]) -> Optional[str]:
    """Accept either `Authorization: Bearer <token>` or `X-LucidLink-Token: <token>`.
    Bearer prefix is stripped if present; a raw Authorization value (no scheme)
    is also accepted. No env-var fallback — every request must carry a token."""
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


# Headers-only dependencies: extract values from the request without touching
# the SDK. Handlers then acquire `_registry_lock` themselves.

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
        raise HTTPException(
            status_code=400,
            detail="Missing X-LucidLink-Filespace header",
        )
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


def _with_fs(token: str, filespace_id: str, fn):
    """Run `fn(filesystem)` against a linked filespace, by id."""
    with _registry_lock:
        session = _get_session(token)
        try:
            filespace = session.link(filespace_id)
        except Exception as e:
            raise _auth_error(e)
        return fn(filespace.fs)


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


def _resolve_filespace_id(token: str, ref: str) -> str:
    """Map the X-LucidLink-Filespace header to a stable filespace id.

    `link_filespace(name=...)` is deprecated upstream because names are mutable:
    a rename silently links to nothing, or to the wrong filespace. The HTTP API
    still accepts a name for compatibility; it is resolved here, through the
    existing list cache, and everything below this line works in ids.

    Must NOT be called while holding `_registry_lock` — the list lookup takes it.
    """
    for entry in _list_filespaces_cached(token):
        if entry["id"] == ref or entry["name"] == ref:
            return entry["id"]
    raise HTTPException(status_code=404, detail=f"Filespace '{ref}' not found")


def _shutdown():
    """Release every link on the way out so pending writes are flushed."""
    with _registry_lock:
        for _, session in list(_clients.items()):
            session.close()
        _clients.clear()


class WriteRequest(BaseModel):
    path: str
    content: str


class MoveRequest(BaseModel):
    src: str
    dst: str


@app.get("/uptime")
def uptime():
    elapsed = time.monotonic() - BOOT_MONO
    now = datetime.now(timezone.utc)
    return {
        "boot_time": BOOT_TIME.isoformat().replace("+00:00", "Z"),
        "uptime_seconds": round(elapsed),
        "uptime_ms": round(elapsed * 1000),
        "current_time": now.isoformat().replace("+00:00", "Z"),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/version")
def version():
    """Report what is actually deployed, so drift is visible without SSH."""
    with _registry_lock:
        linked = sum(len(s.links) for s in _clients.values())
        clients = len(_clients)
    return {
        "sdk": getattr(lucidlink, "__version__", "unknown"),
        "git_sha": os.environ.get("GIT_SHA", "unknown"),
        "mgmt_api_upstream": os.environ.get("MGMT_API_UPSTREAM", "unset"),
        "clients": clients,
        "linked_filespaces": linked,
        "limits": {"max_clients": MAX_CLIENTS,
                   "max_links_per_client": MAX_LINKS_PER_CLIENT},
    }


@app.get("/filespaces")
def list_filespaces(token: str = Depends(require_token)):
    """List filespaces visible to the Service Account."""
    return {"filespaces": _list_filespaces_cached(token)}


@app.get("/filespaces/{name}")
def get_filespace(name: str, token: str = Depends(require_token)):
    """Resolve a single filespace by name. 404 if not visible to this SA."""
    for fs in _list_filespaces_cached(token):
        if fs["name"] == name:
            return fs
    raise HTTPException(status_code=404, detail=f"Filespace '{name}' not found")


def _files_op(creds: tuple[str, str], fn, *, not_found_on_error: bool = False):
    token, filespace_ref = creds
    # Resolved before _with_fs so the list lookup is not attempted while the
    # (non-reentrant) registry lock is held.
    filespace_id = _resolve_filespace_id(token, filespace_ref)
    try:
        return _with_fs(token, filespace_id, fn)
    except HTTPException:
        raise
    except Exception as e:
        status = 404 if not_found_on_error else 500
        raise HTTPException(status_code=status, detail=str(e))


@app.get("/files")
def list_files(path: str = "/", creds=Depends(require_token_and_filespace)):
    return _files_op(creds, lambda fs: [
        {"name": e.name, "is_dir": e.is_dir(), "size": e.size,
         "path": path.rstrip("/") + "/" + e.name}
        for e in fs.read_dir(path)
    ], not_found_on_error=True)


@app.get("/files/read")
def read_file(path: str, creds=Depends(require_token_and_filespace)):
    data = _files_op(creds, lambda fs: fs.read_file(path), not_found_on_error=True)
    try:
        return {"content": data.decode("utf-8"), "encoding": "utf-8"}
    except UnicodeDecodeError:
        import base64
        return {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"}


@app.post("/files/write")
def write_file(req: WriteRequest, creds=Depends(require_token_and_filespace)):
    def op(fs):
        fs.write_file(req.path, req.content.encode())
        return {"status": "ok"}
    return _files_op(creds, op)


@app.post("/files/mkdir")
def make_dir(path: str, creds=Depends(require_token_and_filespace)):
    def op(fs):
        fs.create_dir(path)
        return {"status": "ok"}
    return _files_op(creds, op)


@app.delete("/files")
def delete_file(path: str, creds=Depends(require_token_and_filespace)):
    def op(fs):
        fs.delete(path)
        return {"status": "ok"}
    return _files_op(creds, op)


@app.delete("/files/dir")
def delete_dir(path: str, recursive: bool = True, creds=Depends(require_token_and_filespace)):
    def op(fs):
        fs.delete_dir(path, recursive=recursive)
        return {"status": "ok"}
    return _files_op(creds, op)


@app.post("/files/move")
def move_file(req: MoveRequest, creds=Depends(require_token_and_filespace)):
    def op(fs):
        fs.move(req.src, req.dst)
        return {"status": "ok"}
    return _files_op(creds, op)


@app.get("/files/stat")
def stat_file(path: str, creds=Depends(require_token_and_filespace)):
    def op(fs):
        entry = fs.get_entry(path)
        return {"name": entry.name, "size": entry.size,
                "is_dir": entry.is_dir(), "is_file": entry.is_file()}
    return _files_op(creds, op, not_found_on_error=True)


@app.get("/files/exists")
def file_exists(path: str, creds=Depends(require_token_and_filespace)):
    return _files_op(creds, lambda fs: {"exists": fs.file_exists(path) or fs.dir_exists(path)})


@app.get("/files/download")
def download_file(path: str, creds=Depends(require_token_and_filespace)):
    data = _files_op(creds, lambda fs: fs.read_file(path), not_found_on_error=True)
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    filename = os.path.basename(path)
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


from fastapi import UploadFile, File, Form

@app.post("/files/upload")
async def upload_file(
    path: str = Form(...),
    file: UploadFile = File(...),
    creds=Depends(require_token_and_filespace),
):
    data = await file.read()
    def op(fs):
        fs.write_file(path, data)
        return {"status": "ok", "path": path, "size": len(data)}
    return _files_op(creds, op)

# --- Management API Proxy (append to lucidlink_api.py) ---
# Proxies /api/v1/* to the LucidLink Management API container (Docker internal network)

import httpx, os
from fastapi import Request
from fastapi.responses import JSONResponse

MGMT_API_UPSTREAM = os.environ.get("MGMT_API_UPSTREAM", "http://lucidlink-api:3003")
_http_client = httpx.AsyncClient(base_url=MGMT_API_UPSTREAM, timeout=120.0)


@app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_management_api(request: Request, path: str):
    url = f"/api/v1/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {}
    if "authorization" in request.headers:
        headers["Authorization"] = request.headers["authorization"]
    if "content-type" in request.headers:
        headers["Content-Type"] = request.headers["content-type"]

    body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None

    try:
        resp = await _http_client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=body,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return JSONResponse(status_code=resp.status_code, content=data)
    except httpx.TimeoutException:
        return JSONResponse(status_code=504, content={"error": {"message": "Management API timeout"}})
    except httpx.ConnectError:
        return JSONResponse(status_code=502, content={"error": {"message": "Management API unreachable"}})
