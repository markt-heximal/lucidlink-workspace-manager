"""Ask: answer a question from the indexed chunks (prompt from `discover-rag`).

Retrieval is the same vector search as /index/search; the LLM sees at most `k`
excerpts of 1200 chars each and is told to cite paths. The file service's
/agent/query calls this first and falls back to its shallow live walk when the
indexer is unreachable or answers `empty`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

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
    if not any(svc.store.chunk_count(ws, fs) for ws, fs in scopes):
        return empty
    q = svc.embedder.embed_query(question)
    hits = svc.vectors.search(q, max(1, k), scopes)
    rows = svc.store.chunk_rows([pid for pid, _ in hits])
    used = [(rows[pid], score) for pid, score in hits if pid in rows]
    if not used:
        return empty
    context = "\n---\n".join(f"PATH: {r['path']}\nEXCERPT: {r['text'][:EXCERPT_CHARS]}" for r, _ in used)
    label = ", ".join(sorted({fs for _, fs in scopes}))
    text = svc.llm.chat(ASK_SYSTEM, f"Filespace: {label}\nQuestion: {question}\n\nExcerpts:\n{context}")
    return {
        "answer": text,
        "sources": list(dict.fromkeys(r["path"] for r, _ in used))[:MAX_SOURCES],
        "citations": [{"path": r["path"], "chunkIndex": r["chunk_index"], "score": round(s, 4)}
                      for r, s in used],
    }
