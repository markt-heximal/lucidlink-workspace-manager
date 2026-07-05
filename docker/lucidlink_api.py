from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
import lucidlink, os, time, logging, mimetypes
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

logger = logging.getLogger("lucidlink_api")

BOOT_TIME = datetime.now(timezone.utc)
BOOT_MONO = time.monotonic()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Daemon is process-wide and does NOT need a token to start.
# Each request authenticates with a token (from header or env-var fallback).
daemon = lucidlink.create_daemon()
daemon.start()

# The native LucidLink daemon has a single-active-workspace state model.
# `daemon.authenticate()` mutates global daemon state (stops the previously
# active workspace, links a new one). Caching Workspace or Filesystem objects
# across requests with different tokens would yield stale handles that point at
# a workspace the daemon has since unlinked. So we don't cache them — every
# request re-authenticates under the daemon lock.
#
# Filespace list results are immutable data (not daemon handles), so we keep
# a short TTL cache keyed by token to avoid an authenticate round-trip per call.
_daemon_lock = Lock()
_FS_LIST_TTL = 45.0
_fs_list_cache: dict[str, tuple[float, list]] = {}
_cache_lock = Lock()
_current_link: dict = {"token": None, "filespace": None, "fs": None, "fsobj": None}


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


# Headers-only dependencies: extract values from the request without touching
# the daemon. Handlers then acquire `_daemon_lock` and authenticate themselves,
# so the daemon's single-active-workspace state is always set by whoever holds
# the lock right now (no stale cached workspaces).

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


def _with_fs(token: str, filespace_name: str, fn):
    """Run `fn(fs)` with a linked filespace. Reuses the existing link if the
    token and filespace haven't changed, avoiding expensive unlink/relink cycles.
    On a cache miss the previously linked filespace is torn down with
    Filespace.unlink() (SDK >=0.12: the old no-arg daemon.unlink_filespace() now
    requires the LinkedFilespace handle and is deprecated in favour of
    Filespace.unlink())."""
    with _daemon_lock:
        try:
            if _current_link["token"] == token and _current_link["filespace"] == filespace_name and _current_link["fs"] is not None:
                return fn(_current_link["fs"])
            # Cache miss: unlink the previously linked filespace (if any) before relinking.
            if _current_link["fsobj"] is not None:
                try:
                    _current_link["fsobj"].unlink()
                except Exception:
                    pass
            workspace = _authenticate(token)
            filespace = workspace.link_filespace(name=filespace_name)
            _current_link["token"] = token
            _current_link["filespace"] = filespace_name
            _current_link["fs"] = filespace.fs
            _current_link["fsobj"] = filespace
        except Exception as e:
            _current_link["token"] = None
            _current_link["filespace"] = None
            _current_link["fs"] = None
            _current_link["fsobj"] = None
            raise _auth_error(e)
        return fn(_current_link["fs"])


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
    token, filespace_name = creds
    try:
        return _with_fs(token, filespace_name, fn)
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
