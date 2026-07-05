"""LucidLink file service — FastAPI app.

Core file CRUD + filespace listing + a Range-aware download, plus the Connect
(external S3/HTTP files) and insights (stats / data preview / agent) routers.
Shared daemon plumbing lives in ll_core; SDK is lucidlink >=0.12.
"""
from fastapi import FastAPI, HTTPException, Depends, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel
import os
import time
import mimetypes
import httpx
from datetime import datetime, timezone

from ll_core import (
    require_token,
    require_token_and_filespace,
    _with_fs,
    _list_filespaces_cached,
)
from connect_api import router as connect_router
from insights_api import router as insights_router

BOOT_TIME = datetime.now(timezone.utc)
BOOT_MONO = time.monotonic()

app = FastAPI(title="LucidLink File Service", version="0.12")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(connect_router)
app.include_router(insights_router)


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
    return {"filespaces": _list_filespaces_cached(token)}


@app.get("/filespaces/{name}")
def get_filespace(name: str, token: str = Depends(require_token)):
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
    path: str = Form(...),
    file: UploadFile = File(...),
    creds=Depends(require_token_and_filespace),
):
    data = await file.read()
    def op(fs):
        fs.write_file(path, data)
        return {"status": "ok", "path": path, "size": len(data)}
    return _files_op(creds, op)


# --- Management API Proxy: /api/v1/* -> LucidLink Management API container ---
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
