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
import mimetypes
import os
import signal
import threading
from contextlib import asynccontextmanager
from typing import Callable, Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import activity as activity_mod
from .assist import Action, assist
from .config import Config
from .folders import FolderError
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


class AssistBody(BaseModel):
    action: Action
    text: str = Field(default="", max_length=200_000)
    instruction: str = Field(default="", max_length=4_000)


class FolderBody(BaseModel):
    root: str
    path: str = "/"
    name: Optional[str] = Field(default=None, max_length=200)


class PromptDismissBody(BaseModel):
    kind: Literal["new", "missing"]
    path: Optional[str] = None
    name: Optional[str] = None
    root: str = "volumes"


class FolderOptionsBody(BaseModel):
    name: str = Field(min_length=1)
    downloadOnlineOnly: bool


class FolderRemoveBody(BaseModel):
    name: str = Field(min_length=1)


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


@router.post("/assist")
def assist_route(body: AssistBody, svc: IndexerService = Depends(_service)):
    """Writing assist for the document editor (replaces the docs-ai edge function)."""
    if body.action in ("generate", "custom"):
        if not (body.instruction.strip() or body.text.strip()):
            raise HTTPException(status_code=400, detail="Nothing to do: add an instruction")
    elif not body.text.strip():
        raise HTTPException(status_code=400, detail="Document is empty")
    return assist(svc.llm, body.action, body.text, body.instruction)


@router.get("/folders")
def get_folders(svc: IndexerService = Depends(_service)):
    """Places the engine can see and the connected block storage folders."""
    return svc.folders()


@router.get("/folders/browse")
def browse_folders(root: str, path: str = Query(default="/"), svc: IndexerService = Depends(_service)):
    return svc.browse_folders(root, path)


@router.post("/folders", status_code=201)
def add_folder(body: FolderBody, svc: IndexerService = Depends(_service)):
    return svc.add_folder(body.root, body.path, body.name)


@router.post("/folders/remove")
def remove_folder(body: FolderRemoveBody, svc: IndexerService = Depends(_service)):
    return svc.remove_folder(body.name)


@router.get("/folders/list")
def list_folder(name: str, path: str = Query(default="/"), svc: IndexerService = Depends(_service)):
    """One folder of a connected drive or folder, read-only."""
    return svc.list_folder(name, path)


@router.get("/folders/file")
def folder_file(name: str, path: str, svc: IndexerService = Depends(_service)):
    """A file of a connected drive or folder (Range requests give 206)."""
    full = svc.folder_file(name, path)
    filename = os.path.basename(full)
    media = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(full, media_type=media, content_disposition_type="inline", filename=filename)


@router.get("/activity")
def get_activity(request: Request, after: Optional[int] = Query(default=None, ge=0),
                 limit: int = Query(default=200, ge=1, le=activity_mod.LIMIT_MAX)):
    """Recent engine log lines for the app's engine console (docs: Engine activity feed)."""
    return request.app.state.activity.read(after, limit)


@router.get("/folders/prompts")
def folder_prompts(svc: IndexerService = Depends(_service)):
    """New drives to offer and connected folders that have gone missing."""
    return svc.folder_prompts()


@router.post("/folders/prompts/dismiss")
def dismiss_prompt(body: PromptDismissBody, svc: IndexerService = Depends(_service)):
    return svc.dismiss_prompt(body.kind, body.path, body.name, body.root)


@router.post("/folders/options")
def folder_options(body: FolderOptionsBody, svc: IndexerService = Depends(_service)):
    """Per-source options; today whether a cloud folder may download online-only files to read them."""
    return svc.folder_options(body.name, body.downloadOnlineOnly)


@router.post("/folders/restart", status_code=202)
def restart(request: Request):
    """Exit so Docker restarts the indexer (it then sees shares mounted since it started)."""
    request.app.state.restart()
    return {"restarting": True}


@router.get("/survey")
def get_survey(filespace: str, workspaceId: Optional[str] = None, scope: str = Query(default="/"),
               svc: IndexerService = Depends(_service), x_ws: Optional[str] = WorkspaceHeader):
    return svc.get_survey(_ws(workspaceId, x_ws), filespace, scope)


@router.post("/survey")
def post_survey(body: SurveyBody, svc: IndexerService = Depends(_service),
                x_ws: Optional[str] = WorkspaceHeader):
    return svc.regenerate_survey(_ws(body.workspaceId, x_ws), body.filespace, body.scope)


def _activity_lines() -> int:
    try:
        return int(os.environ.get("ACTIVITY_LINES", "1000"))
    except ValueError:
        return 1000


def _restart_soon() -> None:
    """Shut down gracefully (SIGTERM) shortly after the response; Docker's restart policy starts us again."""
    threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()


def create_app(service_factory: Optional[Callable[[], IndexerService]] = None,
               start_scheduler: bool = True, restart: Callable[[], None] = _restart_soon) -> FastAPI:
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
    app.state.restart = restart
    app.state.activity = activity_mod.install(_activity_lines())

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

    @app.exception_handler(FolderError)
    async def _folder_error(_, e: FolderError):
        return JSONResponse(status_code=e.status, content={"detail": e.detail})

    @app.exception_handler(NotFound)
    async def _not_found(_, e: NotFound):
        return JSONResponse(status_code=404, content={"detail": str(e)})

    app.include_router(router)
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Per-file lines go to the engine console (activity ring) but not to docker logs.
for _h in logging.getLogger().handlers:
    if not isinstance(_h, activity_mod.ActivityLog):
        _h.addFilter(lambda r: r.name != "indexer.files")
app = create_app()
