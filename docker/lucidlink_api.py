"""LucidLink file service — FastAPI app.

Core file CRUD + filespace listing + a Range-aware download, plus the Connect
(external S3/HTTP files) and insights (stats / data preview / agent) routers.
Shared daemon plumbing lives in ll_core; SDK is lucidlink >=0.12.

Also the owner-facing pieces of docs/INDEXER.md: the `/vault/*` router, the
owner-only `/index/*` proxy to the indexer container, and best-effort index
hints after successful file mutations.
"""
from contextlib import asynccontextmanager

from fastapi import (FastAPI, HTTPException, Depends, Request, UploadFile, File, Form,
                     BackgroundTasks)
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel
import os
import time
import mimetypes
from urllib.parse import unquote
import logging
from typing import Optional
import httpx
import lucidlink
from datetime import datetime, timezone

from ll_core import (
    require_token,
    require_token_and_filespace,
    require_caller_and_filespace,
    Caller,
    _with_fs,
    _list_filespaces_cached,
    registry_stats,
    shutdown as _ll_shutdown,
    vault_token_for_owner,
)
from connect_api import router as connect_router
from insights_api import router as insights_router
import vault
from vault import router as vault_router
from updates import router as updates_router, start_scheduler, stop_scheduler
import owner_auth

logger = logging.getLogger("lucidlink_api")

BOOT_TIME = datetime.now(timezone.utc)
BOOT_MONO = time.monotonic()

@asynccontextmanager
async def _lifespan(_app):
    # One-time purge of retired MinIO profiles from the vault (phase 4).
    try:
        vault.ensure_migrated()
    except Exception as e:  # never block startup on the vault
        logger.warning("vault migration skipped: %s", type(e).__name__)
    start_scheduler()
    yield
    stop_scheduler()
    _ll_shutdown()


app = FastAPI(title="LucidLink File Service", version="0.14", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(connect_router)
app.include_router(insights_router)
app.include_router(vault_router)
# Owner only: this service is the public catch-all and the router has no auth.
app.include_router(updates_router, dependencies=[Depends(owner_auth.require_owner)])


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
    return {
        "sdk": getattr(lucidlink, "__version__", "unknown"),
        "git_sha": os.environ.get("GIT_SHA", "unknown"),
        "mgmt_api_upstream": os.environ.get("MGMT_API_UPSTREAM", "unset"),
        **registry_stats(),
    }


@app.get("/filespaces")
def list_filespaces(token: str = Depends(require_token)):
    return {"filespaces": _list_filespaces_cached(token)}


@app.get("/filespaces/{name}")
def get_filespace(name: str, token: str = Depends(require_token)):
    for fs in _list_filespaces_cached(token):
        if fs["name"] == name:
            return fs
    raise HTTPException(status_code=404, detail=f"Filespace '{name}' not found")


# --- Index hints ------------------------------------------------------------
# After a successful mutation the indexer is told which path changed, so in-app
# edits show up in search within seconds instead of at the next delta scan.
# Strictly best effort: it runs after the response, has a short timeout, and a
# failure is only logged. The periodic scan is the safety net.
INDEXER_URL = os.environ.get("INDEXER_URL", "http://lucidlink-indexer:8100").rstrip("/")
HINT_TIMEOUT = 2.0
# Tests swap this for an httpx.MockTransport.
_hint_transport: Optional[httpx.BaseTransport] = None


def _internal_key() -> str:
    return os.environ.get("INDEXER_INTERNAL_KEY", "")


def _send_hint(body: dict, workspace_id: Optional[str]) -> None:
    key = _internal_key()
    if not key:
        return
    if workspace_id:
        body = {"workspaceId": workspace_id, **body}
    try:
        with httpx.Client(timeout=HINT_TIMEOUT, transport=_hint_transport) as client:
            resp = client.post(f"{INDEXER_URL}/index/hint", json=body,
                               headers={"X-Internal-Key": key})
        if resp.status_code >= 400:
            logger.warning("index hint %s %s -> HTTP %s", body.get("op"), body.get("path"), resp.status_code)
    except Exception as e:
        logger.warning("index hint %s %s failed: %s", body.get("op"), body.get("path"), type(e).__name__)


def _hint(background: BackgroundTasks, ctx: "tuple[Caller, str]",
          op: str, path: str, src: Optional[str] = None) -> None:
    """Queue a hint, for the owner only.

    A LucidLink-token caller may be anyone with a token for any workspace and a
    filespace that merely shares a name with the owner's; letting them hint
    would let them force scans of the owner's index. The workspace id sent is
    the vault's resolution for this request, never the raw header.
    """
    caller, filespace = ctx
    if caller.owner is None or not _internal_key():
        return
    body = {"filespace": filespace, "op": op, "path": path}
    if src is not None:
        body["src"] = src
    background.add_task(_send_hint, body, caller.workspace_id)


def _creds(ctx: "tuple[Caller, str]") -> tuple[str, str]:
    return ctx[0].token, ctx[1]


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
def write_file(req: WriteRequest, background: BackgroundTasks,
               ctx=Depends(require_caller_and_filespace)):
    def op(fs):
        fs.write_file(req.path, req.content.encode())
        return {"status": "ok"}
    result = _files_op(_creds(ctx), op)
    _hint(background, ctx, "write", req.path)
    return result


@app.post("/files/mkdir")
def make_dir(path: str, background: BackgroundTasks,
             ctx=Depends(require_caller_and_filespace)):
    def op(fs):
        fs.create_dir(path)
        return {"status": "ok"}
    result = _files_op(_creds(ctx), op)
    _hint(background, ctx, "mkdir", path)
    return result


@app.delete("/files")
def delete_file(path: str, background: BackgroundTasks,
                ctx=Depends(require_caller_and_filespace)):
    def op(fs):
        fs.delete(path)
        return {"status": "ok"}
    result = _files_op(_creds(ctx), op)
    _hint(background, ctx, "delete", path)
    return result


@app.delete("/files/dir")
def delete_dir(path: str, background: BackgroundTasks, recursive: bool = True,
               ctx=Depends(require_caller_and_filespace)):
    def op(fs):
        fs.delete_dir(path, recursive=recursive)
        return {"status": "ok"}
    result = _files_op(_creds(ctx), op)
    _hint(background, ctx, "delete", path)
    return result


@app.post("/files/move")
def move_file(req: MoveRequest, background: BackgroundTasks,
              ctx=Depends(require_caller_and_filespace)):
    def op(fs):
        fs.move(req.src, req.dst)
        return {"status": "ok"}
    result = _files_op(_creds(ctx), op)
    _hint(background, ctx, "move", req.dst, src=req.src)
    return result


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


def _parse_range(range_header: str, total: int):
    """Parse a single 'bytes=start-end'. Returns (start, end) inclusive, or None
    if unsatisfiable."""
    try:
        spec = range_header.split("=", 1)[1].split(",")[0].strip()
    except IndexError:
        return None
    start_s, _, end_s = spec.partition("-")
    if start_s == "" and end_s == "":
        return None
    if start_s == "":  # suffix range: last N bytes
        n = int(end_s)
        if n <= 0:
            return None
        start = max(0, total - n)
        end = total - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else total - 1
    end = min(end, total - 1)
    if start > end or start >= total:
        return None
    return start, end


@app.get("/files/download")
def download_file(path: str, request: Request, creds=Depends(require_token_and_filespace)):
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    filename = os.path.basename(path)
    range_header = request.headers.get("range")

    if range_header and range_header.lower().startswith("bytes="):
        def op(fs):
            total = fs.get_entry(path).size
            rng = _parse_range(range_header, total)
            if rng is None:
                return {"satisfiable": False, "total": total}
            start, end = rng
            f = fs.open(path, "rb")
            try:
                f.seek(start)
                data = f.read(end - start + 1)
            finally:
                try:
                    f.close()
                except Exception:
                    pass
            return {"satisfiable": True, "start": start, "end": end,
                    "total": total, "data": data}

        r = _files_op(creds, op, not_found_on_error=True)
        if not r["satisfiable"]:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{r['total']}"})
        return Response(
            content=r["data"], status_code=206, media_type=content_type,
            headers={
                "Content-Range": f"bytes {r['start']}-{r['end']}/{r['total']}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(r["data"])),
                "Content-Disposition": f'inline; filename="{filename}"',
            },
        )

    data = _files_op(creds, lambda fs: fs.read_file(path), not_found_on_error=True)
    return Response(
        content=data, media_type=content_type,
        headers={"Accept-Ranges": "bytes",
                 "Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/files/upload")
async def upload_file(
    background: BackgroundTasks,
    path: str = Form(...),
    file: UploadFile = File(...),
    ctx=Depends(require_caller_and_filespace),
):
    data = await file.read()
    def op(fs):
        fs.write_file(path, data)
        return {"status": "ok", "path": path, "size": len(data)}
    result = _files_op(_creds(ctx), op)
    _hint(background, ctx, "write", path)
    return result


def _has_dot_segment(path: str) -> bool:
    """True if any segment is `.` or `..`, after percent-decoding (repeatedly,
    so double-encoding does not slip through) and treating `\\` as `/`."""
    decoded = path
    for _ in range(3):
        nxt = unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    return any(seg in (".", "..") for seg in decoded.replace("\\", "/").split("/"))


# --- Management API Proxy: /api/v1/* -> LucidLink Management API container ---
MGMT_API_UPSTREAM = os.environ.get("MGMT_API_UPSTREAM", "http://lucidlink-api:3003")
_http_client = httpx.AsyncClient(base_url=MGMT_API_UPSTREAM, timeout=120.0)


@app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_management_api(request: Request, path: str):
    # httpx normalises dot-segments, so `/api/v1/%2e%2e/...` would otherwise
    # leave the /api/v1 prefix on the upstream.
    if _has_dot_segment(path):
        return JSONResponse(status_code=400, content={"error": {"message": "Invalid path"}})
    url = f"/api/v1/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    headers = {}
    if "authorization" in request.headers:
        auth = request.headers["authorization"]
        bearer = owner_auth.bearer_from_header(auth)
        if bearer and owner_auth.is_jwt(bearer):
            # Owner session: act with the vault's token for the chosen workspace.
            # Blocking (Supabase + SQLite), so off the event loop.
            try:
                await run_in_threadpool(owner_auth.verify_owner, bearer)
                token = await run_in_threadpool(
                    vault_token_for_owner, request.headers.get("x-workspace-id"))
            except HTTPException as e:
                return JSONResponse(status_code=e.status_code,
                                    content={"error": {"message": e.detail}})
            auth = f"Bearer {token}"
        headers["Authorization"] = auth
    if "content-type" in request.headers:
        headers["Content-Type"] = request.headers["content-type"]
    body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None
    try:
        resp = await _http_client.request(method=request.method, url=url, headers=headers, content=body)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return JSONResponse(status_code=resp.status_code, content=data)
    except httpx.TimeoutException:
        return JSONResponse(status_code=504, content={"error": {"message": "Management API timeout"}})
    except httpx.ConnectError:
        return JSONResponse(status_code=502, content={"error": {"message": "Management API unreachable"}})


# --- Indexer proxy: /index/* -> lucidlink-indexer (owner only) ---------------
# The indexer is not published; the browser reaches it only through here. The
# owner's session is checked on this side and the shared X-Internal-Key added,
# so the indexer itself never sees a browser credential.
_INDEX_TIMEOUT = httpx.Timeout(120.0, connect=5.0)  # ask/survey can run an LLM
_index_transport: Optional[httpx.AsyncBaseTransport] = None
_INDEX_PASS_HEADERS = ("content-length", "content-range", "accept-ranges", "content-disposition")


@app.api_route("/index/{path:path}", methods=["GET", "POST", "PUT"])
async def proxy_indexer(request: Request, path: str,
                        _owner: owner_auth.Owner = Depends(owner_auth.require_owner)):
    key = _internal_key()
    if not key:
        return JSONResponse(status_code=503,
                            content={"detail": "Indexer not configured (set INDEXER_INTERNAL_KEY)"})
    if _has_dot_segment(path):
        raise HTTPException(status_code=400, detail="Invalid index path")
    url = f"{INDEXER_URL}/index/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    headers = {"X-Internal-Key": key}
    if "content-type" in request.headers:
        headers["Content-Type"] = request.headers["content-type"]
    if "x-workspace-id" in request.headers:
        headers["X-Workspace-Id"] = request.headers["x-workspace-id"]
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]
    body = await request.body() if request.method in ("POST", "PUT") else None
    # Streamed, so a large file from a connected drive is never held in memory.
    client = httpx.AsyncClient(timeout=_INDEX_TIMEOUT, transport=_index_transport)
    try:
        resp = await client.send(client.build_request(request.method, url, headers=headers, content=body),
                                 stream=True)
    except httpx.TimeoutException:
        await client.aclose()
        return JSONResponse(status_code=504, content={"detail": "Indexer timeout"})
    except httpx.HTTPError as e:
        await client.aclose()
        # ConnectError and friends: the container is down or restarting.
        logger.warning("indexer proxy %s /index/%s failed: %s", request.method, path, type(e).__name__)
        return JSONResponse(status_code=502, content={"detail": "Indexer unreachable"})

    async def close() -> None:
        await resp.aclose()
        await client.aclose()

    # aiter_bytes decodes any content-encoding, so a re-encoded length must not be passed on.
    passed = {k: resp.headers[k] for k in _INDEX_PASS_HEADERS if k in resp.headers}
    if "content-encoding" in resp.headers:
        passed.pop("content-length", None)
    return StreamingResponse(resp.aiter_bytes(), status_code=resp.status_code, headers=passed,
                             media_type=resp.headers.get("content-type"), background=BackgroundTask(close))
