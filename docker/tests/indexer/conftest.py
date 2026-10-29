"""Shared fixtures for indexer tests: in-memory filespaces, a deterministic
embedder, and builders for a fully wired IndexerService."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Callable, Optional

import httpx
import numpy as np
import pytest

from indexer.config import Config
from indexer.embedder import Embedder
from indexer.llm import LLM
from indexer.service import IndexerService
from indexer.source import MemorySourceProvider, StaticSettings

DIM = 64
KEY = "test-internal-key"


class FakeEmbedder(Embedder):
    """Bag-of-words hashed into DIM buckets: deterministic, and similar texts
    get similar vectors, so search results are meaningful."""

    def __init__(self, **kw):
        super().__init__("http://embed.test/v1", "fake-embed", **kw)
        self.batches: list[list[str]] = []

    def _request(self, texts):
        self.batches.append(list(texts))
        out = []
        for t in texts:
            v = np.zeros(DIM, dtype=np.float32)
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
            v[0] += 0.01  # never all-zero
            out.append(v.tolist())
        return out


def sse_transport(answer: str = "The answer, see /docs/a.md.", calls: Optional[list] = None,
                  status: int = 200) -> httpx.MockTransport:
    """An OpenAI-compatible streaming chat endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content))
        if status >= 400:
            return httpx.Response(status, text="boom")
        lines = []
        for i in range(0, len(answer), 7):
            lines.append("data: " + json.dumps({"choices": [{"delta": {"content": answer[i:i + 7]}}]}))
        lines.append("data: [DONE]")
        return httpx.Response(200, text="\n\n".join(lines) + "\n\n",
                              headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handler)


SETTINGS = {
    "workspaces": [
        {"id": "ws1", "label": "One", "url": "", "token": "sa_live:tok1",
         "filespace": "fsA", "filespaces": ["fsA", "fsB"]},
    ],
    "activeWorkspaceId": "ws1",
}


@pytest.fixture
def make_service(tmp_path) -> Callable[..., IndexerService]:
    made: list[IndexerService] = []

    def build(llm_answer: Optional[str] = None, llm_calls: Optional[list] = None,
              ruvector: Optional[httpx.MockTransport] = None, internal_key: str = KEY,
              **cfg) -> IndexerService:
        db_path = cfg.pop("db_path", None) or str(tmp_path / f"index{len(made)}.db")
        config = Config(db_path=db_path, internal_key=internal_key,
                        embed_base_url="http://embed.test/v1", embed_model="fake-embed",
                        ruvector_url="http://ruvector.test" if ruvector else "",
                        llm_base_url="http://llm.test/v1" if llm_answer is not None else "",
                        llm_model="chat" if llm_answer is not None else "", **cfg)
        llm = LLM(config.llm_base_url, config.llm_model,
                  transport=sse_transport(llm_answer or "", llm_calls) if llm_answer is not None else None)
        svc = IndexerService(config, settings=StaticSettings(json.loads(json.dumps(SETTINGS))),
                             sources=MemorySourceProvider(), embedder=FakeEmbedder(), llm=llm,
                             ruvector_transport=ruvector)
        made.append(svc)
        return svc

    yield build
    for svc in made:
        try:
            svc.scheduler.stop()
            svc.store.close()
        except Exception:
            pass


def src(svc: IndexerService, fs: str = "fsA", ws: str = "ws1"):
    return svc.sources.get(ws, fs)


def run(svc: IndexerService, fs: str = "fsA", ws: str = "ws1", full: bool = True) -> None:
    svc.scheduler.run_job(ws, fs, full=full)


def files(svc: IndexerService, fs: str = "fsA", ws: str = "ws1") -> dict[str, dict]:
    return {r["path"]: r for r in svc.store.query(
        "SELECT * FROM files WHERE workspace_id=? AND filespace=? AND status != 'deleted'", (ws, fs))}


def para(topic: str, n: int = 30) -> str:
    """A paragraph long enough to make its own chunk."""
    return " ".join(f"{topic} detail {i}." for i in range(n))
