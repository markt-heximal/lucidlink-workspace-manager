"""HTTP surface of `lucidlink-indexer` (internal port 8100).

The browser never reaches this directly: the file service proxies
`/index/{path}` after verifying the owner and adds `X-Internal-Key`, so every
/index route here requires that header to match INDEXER_INTERNAL_KEY — and, to
fail closed, refuses everything while the key is unset. `/health` is open for
the container healthcheck. The proxy may also forward `X-Workspace-Id`; it is
the default workspace when the body/query omits `workspaceId`.

Run: `uvicorn indexer.app:app --host 0.0.0.0 --port 8100` (from docker/).
"""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Callable, Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import Config
from .embedder import EmbedError
from .llm import LLMError, LLMUnavailable
from .service import IndexerService, NotFound

logger = logging.getLogger("indexer.app")


def _service(request: Request) -> IndexerService:
    return request.app.state.service


def require_internal_key(request: Request,
                         x_internal_key: Optional[str] = Header(default=None, alias="X-Internal-Key")) -> None:
    expected = request.app.state.service.config.internal_key
    if not expected:
        raise HTTPException(status_code=503, detail="INDEXER_INTERNAL_KEY is not set; indexer refuses all requests")
    if not x_internal_key or not hmac.compare_digest(x_internal_key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Internal-Key")


def _ws(body_ws: Optional[str], header_ws: Optional[str]) -> Optional[str]:
    return body_ws or header_ws or None


WorkspaceHeader = Header(default=None, alias="X-Workspace-Id")


class SyncBody(BaseModel):
    workspaceId: Optional[str] = None
    filespace: Optional[str] = None


class FilespaceBody(BaseModel):
    workspaceId: Optional[str] = None
    filespace: str
    enabled: bool


class HintBody(BaseModel):
    workspaceId: Optional[str] = None
    filespace: str
    op: Literal["write", "delete", "move", "mkdir"]
    path: str
    src: Optional[str] = None


class SearchBody(BaseModel):
    workspaceId: Optional[str] = None
    filespace: Optional[str] = None
    query: str = Field(min_length=1)
    k: int = Field(default=20, ge=1, le=200)
    pathPrefix: Optional[str] = None


class AskBody(BaseModel):
    workspaceId: Optional[str] = None
    filespace: Optional[str] = None
    question: str = Field(min_length=1)
    k: int = Field(default=12, ge=1, le=50)


class SurveyBody(BaseModel):
    workspaceId: Optional[str] = None
    filespace: str
    scope: str = "/"


class RetryBody(BaseModel):
    workspaceId: Optional[str] = None
    filespace: str
    paths: Optional[list[str]] = None
    all: bool = False


class PriorityItem(BaseModel):
    workspaceId: str
    filespace: str


class ControlBody(BaseModel):
    paused: Optional[bool] = None
    priority: Optional[list[PriorityItem]] = None


router = APIRouter(prefix="/index", dependencies=[Depends(require_internal_key)])


@router.get("/status")
def status(svc: IndexerService = Depends(_service)):
    return svc.status()


@router.post("/sync", status_code=202)
def sync(body: SyncBody, svc: IndexerService = Depends(_service), x_ws: Optional[str] = WorkspaceHeader):
    return {"queued": svc.sync(_ws(body.workspaceId, x_ws), body.filespace)}


@router.put("/filespaces")
def put_filespace(body: FilespaceBody, svc: IndexerService = Depends(_service),
                  x_ws: Optional[str] = WorkspaceHeader):
    return svc.set_enabled(_ws(body.workspaceId, x_ws), body.filespace, body.enabled)


@router.post("/hint", status_code=202)
def hint(body: HintBody, svc: IndexerService = Depends(_service), x_ws: Optional[str] = WorkspaceHeader):
    svc.hint(_ws(body.workspaceId, x_ws), body.filespace, body.op, body.path, body.src)
    return {}


@router.post("/search")
def search(body: SearchBody, svc: IndexerService = Depends(_service), x_ws: Optional[str] = WorkspaceHeader):
    return {"results": svc.search(_ws(body.workspaceId, x_ws), body.filespace, body.query, body.k,
                                  body.pathPrefix)}


@router.get("/recent")
def recent(workspaceId: Optional[str] = None, filespace: Optional[str] = None,
           limit: int = Query(default=20, ge=1), svc: IndexerService = Depends(_service),
           x_ws: Optional[str] = WorkspaceHeader):
    """Recently changed files; `limit` is capped at 200."""
    return {"files": svc.recent(_ws(workspaceId, x_ws), filespace or None, limit)}


@router.get("/files")
def list_files(filespace: str, status: Literal["failed", "skipped", "pending", "indexed"],
               workspaceId: Optional[str] = None, limit: int = Query(default=50, ge=1),
               offset: int = Query(default=0, ge=0), q: Optional[str] = None,
               svc: IndexerService = Depends(_service), x_ws: Optional[str] = WorkspaceHeader):
    """A source's files in one status, paged; `limit` is capped at 200."""
    return svc.files(_ws(workspaceId, x_ws), filespace, status, limit, offset, q)


@router.post("/retry")
def retry(body: RetryBody, svc: IndexerService = Depends(_service), x_ws: Optional[str] = WorkspaceHeader):
    return {"requeued": svc.retry(_ws(body.workspaceId, x_ws), body.filespace, body.paths, body.all)}


@router.get("/control")
def get_control(svc: IndexerService = Depends(_service)):
    return svc.control()


@router.put("/control")
def put_control(body: ControlBody, svc: IndexerService = Depends(_service)):
    prio = None if body.priority is None else [{"workspaceId": p.workspaceId, "filespace": p.filespace}
                                               for p in body.priority]
    return svc.set_control(body.paused, prio)


@router.get("/profile")
def profile(filespace: str, workspaceId: Optional[str] = None, svc: IndexerService = Depends(_service),
            x_ws: Optional[str] = WorkspaceHeader):
    """What a source holds (types, ages, largest folders, skips), from the ledger only."""
    return svc.profile(_ws(workspaceId, x_ws), filespace)


@router.post("/ask")
def ask(body: AskBody, svc: IndexerService = Depends(_service), x_ws: Optional[str] = WorkspaceHeader):
    return svc.ask(_ws(body.workspaceId, x_ws), body.filespace, body.question, body.k)


@router.get("/survey")
def get_survey(filespace: str, workspaceId: Optional[str] = None, scope: str = Query(default="/"),
               svc: IndexerService = Depends(_service), x_ws: Optional[str] = WorkspaceHeader):
    return svc.get_survey(_ws(workspaceId, x_ws), filespace, scope)


@router.post("/survey")
def post_survey(body: SurveyBody, svc: IndexerService = Depends(_service),
                x_ws: Optional[str] = WorkspaceHeader):
    return svc.regenerate_survey(_ws(body.workspaceId, x_ws), body.filespace, body.scope)


def create_app(service_factory: Optional[Callable[[], IndexerService]] = None,
               start_scheduler: bool = True) -> FastAPI:
    """Build the app. The service is created in the lifespan, so importing this
    module never touches /data."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = service_factory() if service_factory else IndexerService(Config.from_env())
        app.state.service = svc
        if not svc.config.internal_key:
            logger.error("INDEXER_INTERNAL_KEY is not set: all /index requests will be refused")
        if start_scheduler:
            svc.start()
        try:
            yield
        finally:
            svc.stop()

    app = FastAPI(title="LucidLink Indexer", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.exception_handler(LLMUnavailable)
    async def _llm_unavailable(_, e: LLMUnavailable):
        return JSONResponse(status_code=503, content={"detail": str(e)})

    @app.exception_handler(LLMError)
    async def _llm_error(_, e: LLMError):
        return JSONResponse(status_code=502, content={"detail": str(e)})

    @app.exception_handler(EmbedError)
    async def _embed_error(_, e: EmbedError):
        return JSONResponse(status_code=503, content={"detail": f"Embedder unavailable: {e}"})

    @app.exception_handler(NotFound)
    async def _not_found(_, e: NotFound):
        return JSONResponse(status_code=404, content={"detail": str(e)})

    app.include_router(router)
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
