"""Filespace insights routes: stats, tabular data preview, and the RAG agent.

- /filespaces/{name}/stats  -> SDK FileSystem.statistics()
- /data/preview             -> bounded CSV/TSV preview via FileSystem.open() (no OOM)
- /agent/query              -> the indexer's /index/ask (owner callers), else
                               shallow retrieval over the filespace + an
                               OpenAI-compatible LLM (configured via env)
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from collections import deque
import os
import csv
import json
import logging
import httpx

from ll_core import require_token, require_caller, Caller, _with_fs

logger = logging.getLogger("lucidlink_api")

router = APIRouter(tags=["insights"])

# LLM config for /agent/query (OpenAI-compatible chat completions endpoint).
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "180"))

_TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml"}


# ---------------------------------------------------------------- stats
@router.get("/filespaces/{name}/stats")
def filespace_stats(name: str, token: str = Depends(require_token)):
    # SDK 1.0's statistics() keeps only entries_count, storage_size and the two
    # external_files_* counters. The response keeps every key the web app
    # reads; the counters 1.0 no longer reports are null (the UI shows "—"),
    # and `entriesCount` (files + directories + symlinks) is new.
    def op(fs):
        s = fs.statistics()
        return {
            "sizeBytes": s.storage_size,
            "fileCount": None,
            "dirCount": None,
            "symlinkCount": None,
            "externalFileCount": s.external_files_count,
            "dataSize": None,
            "storageSize": s.storage_size,
            "entriesSize": None,
            "externalFilesSize": s.external_files_size,
            "entriesCount": s.entries_count,
        }
    try:
        return _with_fs(token, name, op)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------- data preview
class PreviewRequest(BaseModel):
    filespace: str
    path: str
    limit: int = 100


@router.post("/data/preview")
def data_preview(req: PreviewRequest, token: str = Depends(require_token)):
    ext = os.path.splitext(req.path)[1].lower()
    if ext == ".parquet":
        raise HTTPException(status_code=415,
                            detail="Parquet preview requires pyarrow (not installed on this service).")
    if ext not in {".csv", ".tsv", ".txt"}:
        raise HTTPException(status_code=415, detail=f"Preview supports CSV/TSV, not '{ext}'.")
    delim = "\t" if ext == ".tsv" else ","
    limitn = max(1, min(req.limit, 1000))

    def op(fs):
        header = None
        rows = []
        truncated = False
        with fs.open(req.path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f, delimiter=delim)
            for row in reader:
                if header is None:
                    header = row
                    continue
                if len(rows) < limitn:
                    rows.append(row)
                else:
                    truncated = True
                    break
        return {"columns": header or [], "rows": rows,
                "rowsReturned": len(rows), "truncated": truncated}

    try:
        return _with_fs(token, req.filespace, op)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------- agent query
class AgentRequest(BaseModel):
    filespace: str
    question: str


# Context is kept small on purpose: a large prompt means a long, silent LLM
# prefill, and the container->LAN NAT drops a connection that goes quiet for too
# long (small prompts stream a first token within seconds and stay alive).
CONTEXT_MAX_FILES = int(os.environ.get("AGENT_MAX_FILES", "4"))
CONTEXT_PER_FILE_BYTES = int(os.environ.get("AGENT_PER_FILE_BYTES", "1500"))
CONTEXT_TOTAL_CHARS = int(os.environ.get("AGENT_MAX_CONTEXT_CHARS", "6000"))


def _gather_context(fs, max_files: int = CONTEXT_MAX_FILES,
                    per_file_bytes: int = CONTEXT_PER_FILE_BYTES):
    """Shallow (depth<=2) walk collecting small text files, returning
    (context_text, sources). Bounded so it stays cheap under the daemon lock."""
    picked = []
    q = deque([("/", 0)])
    dirs_scanned = 0
    while q and len(picked) < max_files:
        path, depth = q.popleft()
        try:
            entries = fs.read_dir(path)
        except Exception:
            continue
        for e in entries:
            full = path.rstrip("/") + "/" + e.name
            if e.is_dir:
                if depth < 2 and dirs_scanned < 25:
                    dirs_scanned += 1
                    q.append((full, depth + 1))
            elif os.path.splitext(e.name)[1].lower() in _TEXT_EXT and 0 < e.size <= 200000:
                picked.append(full)
                if len(picked) >= max_files:
                    break
    parts, sources = [], []
    for fp in picked:
        try:
            # Only the head is used: read that much, not the whole file.
            with fs.open(fp, "rb") as f:
                data = f.read(per_file_bytes)
            parts.append(f"### {fp}\n{data.decode('utf-8', 'replace')}")
            sources.append(fp)
        except Exception:
            continue
    return "\n\n".join(parts), sources


def _call_llm(question: str, context: str) -> str:
    system = ("You answer questions about the contents of a LucidLink filespace using "
              "ONLY the provided file excerpts. Cite the filenames you used. If the answer "
              "is not in the excerpts, say so plainly.")
    user = (f"Question: {question}\n\nFile excerpts:\n"
            f"{context if context else '(no readable text files found in this filespace)'}")
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    # NOTE: stream=True is deliberate. A buffered (non-stream) POST response from
    # a LAN LLM is blackholed by the container NAT return path; streamed chunks get
    # through. We accumulate the SSE delta content into the full answer.
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.2,
        "stream": True,
    }
    pieces = []
    try:
        with httpx.Client(timeout=LLM_TIMEOUT) as client:
            with client.stream("POST", f"{LLM_BASE_URL}/chat/completions",
                               json=payload, headers=headers) as r:
                if r.status_code >= 400:
                    body = r.read().decode("utf-8", "replace")[:200]
                    raise HTTPException(status_code=502, detail=f"LLM error {r.status_code}: {body}")
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0]["delta"]
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if delta.get("content"):
                        pieces.append(delta["content"])
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"LLM unreachable at {LLM_BASE_URL}: {e}")
    answer = "".join(pieces).strip()
    if not answer:
        raise HTTPException(status_code=502, detail="LLM returned an empty response.")
    return answer


# Indexer delegation (docs/INDEXER.md). Read at call time so tests and env
# changes apply without a reload; the transport hook is for tests.
_indexer_transport: Optional[httpx.BaseTransport] = None
INDEXER_ASK_TIMEOUT = httpx.Timeout(120.0, connect=3.0)


def _ask_indexer(req: AgentRequest, workspace_id: Optional[str]) -> Optional[dict]:
    """{answer, sources} from the indexer, or None to fall back.

    Falls back on anything other than a usable answer: indexer not configured
    or unreachable, any non-2xx (503 = no LLM there, 404 = filespace not
    indexed, ...), `"empty": true`, or a malformed reply. The shallow path
    below is slower and shallower but always available.
    """
    key = os.environ.get("INDEXER_INTERNAL_KEY", "")
    if not key:
        return None
    base = os.environ.get("INDEXER_URL", "http://lucidlink-indexer:8100").rstrip("/")
    headers = {"X-Internal-Key": key}
    body: dict = {"filespace": req.filespace, "question": req.question}
    if workspace_id:
        headers["X-Workspace-Id"] = workspace_id
        body["workspaceId"] = workspace_id
    try:
        with httpx.Client(timeout=INDEXER_ASK_TIMEOUT, transport=_indexer_transport) as client:
            resp = client.post(f"{base}/index/ask", json=body, headers=headers)
    except httpx.HTTPError as e:
        logger.info("indexer ask unavailable (%s); using shallow agent", type(e).__name__)
        return None
    if resp.status_code >= 300:
        logger.info("indexer ask -> HTTP %s; using shallow agent", resp.status_code)
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("empty") or not data.get("answer"):
        return None
    sources = data.get("sources") or []
    return {"answer": data["answer"], "sources": [s for s in sources if isinstance(s, str)]}


@router.post("/agent/query")
def agent_query(req: AgentRequest, caller: Caller = Depends(require_caller)):
    # Only the owner's index may answer, and only for a verified owner: a
    # LucidLink-token caller is not checked against the owner's workspaces
    # until the SDK logs in, so it always takes the per-request path.
    if caller.owner is not None:
        indexed = _ask_indexer(req, caller.workspace_id)
        if indexed is not None:
            return indexed
    token = caller.token
    if not LLM_BASE_URL or not LLM_MODEL:
        raise HTTPException(
            status_code=503,
            detail="Agent LLM not configured. Set LLM_BASE_URL and LLM_MODEL env vars "
                   "(OpenAI-compatible /chat/completions endpoint).",
        )
    # Gather context under the daemon lock; call the LLM outside it.
    try:
        context, sources = _with_fs(token, req.filespace, _gather_context)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Context gathering failed: {e}")
    if len(context) > CONTEXT_TOTAL_CHARS:
        context = context[:CONTEXT_TOTAL_CHARS] + "\n…(truncated)"
    answer = _call_llm(req.question, context)
    return {"answer": answer, "sources": sources}
