"""Ask: answer a question from the indexed chunks (prompt from `discover-rag`).

Retrieval is the same hybrid ranking as /index/search (meaning + keywords +
file names, search.retrieve), so a question naming "ThinkTank" sees the
documents that literally say it; the LLM sees at most `k`
excerpts of 1200 chars each and is told to cite paths. The file service's
/agent/query calls this first and falls back to its shallow live walk when the
indexer is unreachable or answers `empty`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import search as search_mod

if TYPE_CHECKING:
    from .service import IndexerService
    from .vectors import Scope

ASK_SYSTEM = (
    "You answer questions about a document filespace using only the provided excerpts. "
    "Cite the file paths you relied on inline. If the excerpts do not contain the answer, say so plainly."
)
EXCERPT_CHARS = 1200
MAX_SOURCES = 10

EMPTY_ANSWER = "Nothing is indexed for this filespace yet. Wait for the indexer to finish a sync, then ask again."


def answer(svc: "IndexerService", scopes: "set[Scope]", question: str, k: int = 12) -> dict:
    """Raises LLMUnavailable (503) / LLMError (502) / EmbedError."""
    svc.llm_required()
    empty = {"answer": EMPTY_ANSWER, "sources": [], "citations": [], "empty": True}
    if not svc.store.has_chunks(scopes):
        return empty
    ret = search_mod.retrieve(svc, scopes, question, "all", pool=max(search_mod.POOL // 2, k),
                              question=True)
    used = [(h.row, h.score) for h in ret.hits[:max(1, k)]]
    if not used:
        return empty
    context = "\n---\n".join(f"PATH: {r['path']}\nEXCERPT: {r['text'][:EXCERPT_CHARS]}" for r, _ in used)
    label = ", ".join(sorted({fs for _, fs in scopes}))
    kinds = svc.kinds()
    locs = svc.locations()
    text = svc.llm.chat(ASK_SYSTEM, f"Filespace: {label}\nQuestion: {question}\n\nExcerpts:\n{context}")
    return {
        "answer": text,
        "sources": list(dict.fromkeys(r["path"] for r, _ in used))[:MAX_SOURCES],
        "citations": [{"kind": kinds.get((r["workspace_id"], r["filespace"]), "lucidlink"), "workspaceId": r["workspace_id"], "filespace": r["filespace"], "location": locs.get((r["workspace_id"], r["filespace"])), "path": r["path"], "chunkIndex": r["chunk_index"], "score": round(s, 4)}
                      for r, s in used],
    }
