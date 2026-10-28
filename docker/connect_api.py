"""LucidLink Connect (external S3 / HTTP files) routes.

Maps the /connect/* contract onto SDK 1.0's `filespace.connect.data_stores`
(lucidlink.connect). The HTTP request and response shapes are the ones the web
app was built against on SDK 0.12-0.14; what 1.0 changed underneath:

* `kind` is now "s3" or "http_link" (was "S3DataStore"), and `keyId` is always
  null: 1.0 no longer reports a key id.
* HTTP-link stores (filespace format 3.8+) are listed too; their S3 fields
  (endpoint, bucket, ...) are null.
* `POST /connect/link-url` links through an HTTP-link data store on format 3.8+
  (`dataStore` in the body, default $LUCIDLINK_HTTP_LINK_STORE / "http-links",
  created on first use) and through `HttpConnect_3_6` on format 3.6.
* `GET /connect/external-files` pages with an integer offset in `cursor`
  (1.0 iterates files without exposing the service's cursor).
* `DELETE /connect/link` deletes only a file a data store reports as its own;
  anything else is refused with 400 (1.0 removed `unlink_file`, and a plain
  `file_system.delete` would remove any file).

Secrets (secret_key) are NEVER returned; access keys are masked.
"""
import itertools
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from lucidlink import exceptions as llx
from lucidlink.connect import (
    DataStoreKind,
    HttpConnect_3_6,
    HttpLinkDataStoreConfig,
    S3Credentials,
    S3DataStoreConfig,
)

from ll_core import require_token_and_filespace, _with_filespace

logger = logging.getLogger("lucidlink_api")

router = APIRouter(prefix="/connect", tags=["connect"])

# Filespace format number from which HTTP links go through an HTTP-link data
# store (3.8 = 12; 3.6 = 10 uses HttpConnect_3_6). Same threshold as the SDK.
HTTP_LINK_STORE_MIN_VERSION = 12
DEFAULT_HTTP_LINK_STORE = os.environ.get("LUCIDLINK_HTTP_LINK_STORE", "http-links")
MAX_EXTERNAL_FILES_PAGE = 1000


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
    # Format 3.8+ only: the HTTP-link data store to link through. Optional so
    # the web app's existing body still works.
    dataStore: Optional[str] = None


class UnlinkRequest(BaseModel):
    filePath: str


def _mask(key: str) -> str:
    if not key:
        return ""
    return key[:4] + "…" if len(key) > 4 else "…"


def _value(v):
    return getattr(v, "value", v)


def _store_public(info, external_file_count: Optional[int] = None) -> dict:
    """Serialize a DataStoreInfo / S3DataStoreInfo WITHOUT the secret; access
    key masked. Fields a kind does not have are null."""
    s3 = info.kind == DataStoreKind.S3
    out = {
        "name": info.name,
        "kind": _value(info.kind),
        "endpoint": info.endpoint if s3 else None,
        "bucket": info.bucket_name if s3 else None,
        "region": info.region if s3 else None,
        "keyId": None,  # not reported since SDK 1.0
        "accessKey": _mask(info.access_key) if s3 else None,
        "urlExpirationMinutes": info.url_expiration_minutes if s3 else None,
        "useVirtualAddressing": info.use_virtual_addressing if s3 else None,
        "rekeyState": _value(info.rekey_state) if s3 else None,
    }
    if external_file_count is not None:
        out["externalFileCount"] = external_file_count
    return out


def _status_for(e: Exception) -> int:
    """HTTP status for an error raised by a Connect call (SDK 1.0 types)."""
    if isinstance(e, llx.UnsupportedOperationError):
        return 409                      # Connect not available on this filespace
    if isinstance(e, (llx.DataStoreNotFoundError, FileNotFoundError, llx.ExternalObjectNotFoundError)):
        return 404
    if isinstance(e, (llx.DataStoreExistsError, llx.DataStoreInUseError, FileExistsError)):
        return 409
    if isinstance(e, llx.DataStoreCredentialsError):
        return 400                      # the S3 credentials given were rejected
    if isinstance(e, llx.ExternalStoreUnavailableError):
        return 502
    if isinstance(e, PermissionError):
        return 403
    if isinstance(e, llx.ConnectError):
        return 409                      # store limit reached, data-store lock held: retry
    if isinstance(e, (llx.ClientClosedError, llx.FilespaceNotLinkedError)):
        return 503                      # evicted or shut down under us: retry
    if isinstance(e, (ValueError, NotADirectoryError, IsADirectoryError)):
        return 400                      # InvalidArgumentError is a ValueError
    return 500


def _connect_op(creds, fn):
    """Run fn(connect, filespace) under a linked filespace, mapping SDK 1.0's
    typed errors to statuses (credential/service errors are mapped by ll_core)."""
    token, filespace_name = creds

    def op(filespace):
        return fn(filespace.connect, filespace)

    try:
        return _with_filespace(token, filespace_name, op)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=_status_for(e), detail=str(e))


def _sync(filespace, why: str) -> None:
    """Best-effort sync so a new data store is visible to count()/iter_files()."""
    try:
        filespace.sync()
    except Exception:
        logger.warning("filespace sync after %s failed", why, exc_info=True)


def _available(c) -> bool:
    try:
        c.data_stores.list()
        return True
    except llx.UnsupportedOperationError:
        return False


@router.get("/datastores/available")
def datastores_available(creds=Depends(require_token_and_filespace)):
    return {"available": _connect_op(creds, lambda c, fsp: _available(c))}


@router.get("/datastores")
def list_datastores(creds=Depends(require_token_and_filespace)):
    def fn(c, fsp):
        try:
            stores = c.data_stores.list()
        except llx.UnsupportedOperationError:
            return {"available": False, "dataStores": []}
        result = []
        for s in stores:
            try:
                cnt = s.count()
            except Exception:
                cnt = None              # e.g. a store not synced yet
            result.append(_store_public(s.info(), cnt))
        return {"available": True, "dataStores": result}
    return _connect_op(creds, fn)


@router.post("/datastores")
def add_datastore(req: DataStoreCreate, creds=Depends(require_token_and_filespace)):
    def fn(c, fsp):
        config = S3DataStoreConfig(
            access_key=req.accessKeyId,
            secret_key=req.secretAccessKey,
            bucket_name=req.bucket,
            region=req.region,
            endpoint=req.endpoint,
            url_expiration_minutes=req.urlExpirationMinutes,
            use_virtual_addressing=req.useVirtualAddressing,
        )
        store = c.data_stores.add(req.name, config)
        _sync(fsp, f"adding data store {req.name!r}")
        return _store_public(store.info())
    return _connect_op(creds, fn)


@router.post("/datastores/{name}/rekey")
def rekey_datastore(name: str, req: RekeyRequest, creds=Depends(require_token_and_filespace)):
    def fn(c, fsp):
        c.data_stores.get(name, DataStoreKind.S3).rekey(
            S3Credentials(access_key=req.accessKeyId, secret_key=req.secretAccessKey))
        return {"status": "ok"}
    return _connect_op(creds, fn)


@router.delete("/datastores/{name}")
def remove_datastore(name: str, creds=Depends(require_token_and_filespace)):
    def fn(c, fsp):
        c.data_stores.remove(name)
        return {"status": "ok"}
    return _connect_op(creds, fn)


@router.post("/link")
def link_file(req: LinkRequest, creds=Depends(require_token_and_filespace)):
    def fn(c, fsp):
        store = c.data_stores.get(req.dataStore, DataStoreKind.S3)
        # size+checksum must be provided together (or neither) per SDK contract.
        if req.size is not None and req.checksum:
            store.link(req.filePath, req.objectKey, size=req.size, checksum=req.checksum)
        else:
            store.link(req.filePath, req.objectKey)
        return {"status": "ok", "filePath": req.filePath}
    return _connect_op(creds, fn)


def _http_link_store(c, fsp, name: Optional[str]):
    """The HTTP-link data store to link through (format 3.8+). Only the default
    store is created on demand; a named store must already exist."""
    wanted = name or DEFAULT_HTTP_LINK_STORE
    try:
        return c.data_stores.get(wanted, DataStoreKind.HTTP_LINK)
    except llx.DataStoreNotFoundError:
        if name and name != DEFAULT_HTTP_LINK_STORE:
            raise
    store = c.data_stores.add(wanted, HttpLinkDataStoreConfig())
    _sync(fsp, f"adding HTTP-link data store {wanted!r}")
    return store


@router.post("/link-url")
def link_url(req: LinkUrlRequest, creds=Depends(require_token_and_filespace)):
    def fn(c, fsp):
        if fsp.version >= HTTP_LINK_STORE_MIN_VERSION:
            _http_link_store(c, fsp, req.dataStore).link(req.filePath, req.url, size=req.size)
        else:
            HttpConnect_3_6(fsp).link(req.filePath, req.url, size=req.size)
        return {"status": "ok", "filePath": req.filePath}
    return _connect_op(creds, fn)


def _owning_store(c, path: str):
    """The data store `path` is linked from, or None. Raises FileNotFoundError
    when the path does not exist."""
    for store in c.data_stores.list():
        try:
            store.get_file(path)
            return store
        except llx.DataStoreNotFoundError:
            continue                    # removed while we looked
        except llx.InvalidArgumentError:
            continue                    # not this store's file (or not external at all)
    return None


@router.delete("/link")
def unlink_file(req: UnlinkRequest, creds=Depends(require_token_and_filespace)):
    def fn(c, fsp):
        # 1.0 unlinks by deleting the file. file_system.delete removes ANY file,
        # so this route first insists the path is an external file of a store;
        # a regular file is never deleted through /connect.
        if _owning_store(c, req.filePath) is None:
            raise HTTPException(
                status_code=400,
                detail=f"'{req.filePath}' is not an external file linked from a data store "
                       "(HTTP links on filespace format 3.6 have none: delete those with DELETE /files)")
        fsp.file_system.delete(req.filePath)
        return {"status": "ok"}
    return _connect_op(creds, fn)


@router.get("/external-files")
def external_files(dataStore: str, limit: int = 100, cursor: str = "",
                   creds=Depends(require_token_and_filespace)):
    try:
        offset = int(cursor) if cursor else 0
    except ValueError:
        raise HTTPException(status_code=400, detail="cursor must be the value a previous page returned")
    if offset < 0:
        raise HTTPException(status_code=400, detail="cursor must not be negative")
    limit = max(1, min(limit, MAX_EXTERNAL_FILES_PAGE))

    def fn(c, fsp):
        store = c.data_stores.get(dataStore)
        # One extra item says whether there is another page. iter_files() has
        # no cursor in 1.0, so a later page re-walks the earlier ones.
        page = list(itertools.islice(store.iter_files(), offset, offset + limit + 1))
        has_more = len(page) > limit
        files = [{"filePath": f.get_path(), "fileId": f.entry_id, "source": dataStore}
                 for f in page[:limit]]
        return {"files": files, "hasMore": has_more,
                "cursor": str(offset + limit) if has_more else ""}
    return _connect_op(creds, fn)
