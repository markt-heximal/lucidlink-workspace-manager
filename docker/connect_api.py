"""LucidLink Connect (external S3 / HTTP files) routes.

Maps the /connect/* contract onto SDK 0.12's `filespace.connect` (ConnectManager).
Secrets (secret_key) are NEVER returned; access keys are masked.
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import lucidlink

from ll_core import require_token_and_filespace, _with_filespace

router = APIRouter(prefix="/connect", tags=["connect"])


class DataStoreCreate(BaseModel):
    name: str
    endpoint: str = ""
    region: str = "us-east-1"
    bucket: str
    accessKeyId: str
    secretAccessKey: str
    urlExpirationMinutes: int = 10080
    useVirtualAddressing: bool = False


class RekeyRequest(BaseModel):
    accessKeyId: str
    secretAccessKey: str


class LinkRequest(BaseModel):
    dataStore: str
    objectKey: str
    filePath: str
    size: Optional[int] = None
    checksum: Optional[str] = None


class LinkUrlRequest(BaseModel):
    url: str
    filePath: str
    size: Optional[int] = None


class UnlinkRequest(BaseModel):
    filePath: str


def _mask(key: str) -> str:
    if not key:
        return ""
    return key[:4] + "…" if len(key) > 4 else "…"


def _store_public(info, external_file_count: Optional[int] = None) -> dict:
    """Serialize a DataStoreInfo WITHOUT the secret; access key masked."""
    out = {
        "name": info.name,
        "kind": getattr(info.kind, "value", str(info.kind)),
        "endpoint": info.endpoint,
        "bucket": info.bucket_name,
        "region": info.region,
        "keyId": info.key_id,
        "accessKey": _mask(info.access_key),
        "urlExpirationMinutes": info.url_expiration_minutes,
        "useVirtualAddressing": info.use_virtual_addressing,
        "rekeyState": getattr(info.rekey_state, "value", str(info.rekey_state)),
    }
    if external_file_count is not None:
        out["externalFileCount"] = external_file_count
    return out


def _connect_op(creds, fn):
    """Run fn(connect) under a linked filespace, translating the SDK's
    'not available' RuntimeError into a clean 409."""
    token, filespace_name = creds

    def op(filespace):
        connect = filespace.connect
        return fn(connect)

    try:
        return _with_filespace(token, filespace_name, op)
    except HTTPException:
        raise
    except RuntimeError as e:
        msg = str(e)
        if "not available" in msg.lower():
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=500, detail=msg)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/datastores/available")
def datastores_available(creds=Depends(require_token_and_filespace)):
    return {"available": _connect_op(creds, lambda c: c.are_data_stores_available())}


@router.get("/datastores")
def list_datastores(creds=Depends(require_token_and_filespace)):
    def fn(c):
        stores = c.list_data_stores()
        result = []
        for s in stores:
            try:
                cnt = c.count_external_files(s.name)
            except Exception:
                cnt = None
            result.append(_store_public(s, cnt))
        return result
    return {"available": _connect_op(creds, lambda c: c.are_data_stores_available()),
            "dataStores": _connect_op(creds, fn)}


@router.post("/datastores")
def add_datastore(req: DataStoreCreate, creds=Depends(require_token_and_filespace)):
    def fn(c):
        config = lucidlink.S3DataStoreConfig(
            access_key=req.accessKeyId,
            secret_key=req.secretAccessKey,
            bucket_name=req.bucket,
            region=req.region,
            endpoint=req.endpoint,
            url_expiration_minutes=req.urlExpirationMinutes,
            use_virtual_addressing=req.useVirtualAddressing,
        )
        info = c.add_data_store(req.name, config)
        return _store_public(info)
    return _connect_op(creds, fn)


@router.post("/datastores/{name}/rekey")
def rekey_datastore(name: str, req: RekeyRequest, creds=Depends(require_token_and_filespace)):
    def fn(c):
        c.rekey_data_store(name, new_access_key=req.accessKeyId, new_secret_key=req.secretAccessKey)
        return {"status": "ok"}
    return _connect_op(creds, fn)


@router.delete("/datastores/{name}")
def remove_datastore(name: str, creds=Depends(require_token_and_filespace)):
    def fn(c):
        c.remove_data_store(name)
        return {"status": "ok"}
    return _connect_op(creds, fn)


@router.post("/link")
def link_file(req: LinkRequest, creds=Depends(require_token_and_filespace)):
    def fn(c):
        # size+checksum must be provided together (or neither) per SDK contract.
        if req.size is not None and req.checksum:
            c.link_file(req.filePath, req.dataStore, req.objectKey,
                        size=req.size, checksum=req.checksum)
        else:
            c.link_file(req.filePath, req.dataStore, req.objectKey)
        return {"status": "ok", "filePath": req.filePath}
    return _connect_op(creds, fn)


@router.post("/link-url")
def link_url(req: LinkUrlRequest, creds=Depends(require_token_and_filespace)):
    def fn(c):
        c.link_http_file(req.filePath, req.url, size=req.size)
        return {"status": "ok", "filePath": req.filePath}
    return _connect_op(creds, fn)


@router.delete("/link")
def unlink_file(req: UnlinkRequest, creds=Depends(require_token_and_filespace)):
    def fn(c):
        c.unlink_file(req.filePath)
        return {"status": "ok"}
    return _connect_op(creds, fn)


@router.get("/external-files")
def external_files(dataStore: str, limit: int = 100, cursor: str = "",
                   creds=Depends(require_token_and_filespace)):
    def fn(c):
        res = c.list_external_files(dataStore, limit=limit, cursor=cursor)
        files = [
            {"filePath": p, "fileId": fid, "source": dataStore}
            for p, fid in zip(res.file_paths, res.file_ids)
        ]
        return {"files": files, "hasMore": res.has_more, "cursor": res.cursor}
    return _connect_op(creds, fn)
