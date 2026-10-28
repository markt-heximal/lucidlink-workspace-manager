"""Discover topic survey, ported from the `discover-rag` edge function.

Same probes, same SURVEY_SYSTEM prompt, same excerpt budget (25 hits per probe,
first 120 distinct chunks, 900 chars each) — only the retrieval now runs over
the local index instead of pgvector, and the result is stored so Discover opens
instantly. The worker regenerates the root survey after a sync that changed
something, at most every `survey_min_interval_sec`.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from .store import now_iso

if TYPE_CHECKING:
    from .service import IndexerService

logger = logging.getLogger("indexer.survey")

PROBES = [
    "project plans, proposals and strategy",
    "contracts, agreements and legal terms",
    "financials, budgets, invoices and pricing",
    "meeting notes, status updates and reports",
    "technical specifications, designs and documentation",
    "people, customers, vendors and correspondence",
]

SURVEY_SYSTEM = (
    "You are a document librarian. You receive excerpts from files in a shared filespace. "
    "Group them into 3-8 distinct topics. Reply with STRICT JSON only, no prose, no code fences, "
    'shaped as {"topics":[{"name":string,"description":string,"dateRange":string,'
    '"documents":[{"path":string,"title":string,"date":string}]}]}. '
    "Only use paths that appear in the excerpts. Leave date fields as an empty string when unknown."
)

HITS_PER_PROBE = 25
MAX_EXCERPTS = 120
EXCERPT_CHARS = 900

_FENCE_START = re.compile(r"^```(?:json)?", re.IGNORECASE)
_FENCE_END = re.compile(r"```$")


def parse_topics(answer: str) -> list[dict]:
    """Parse the model's JSON leniently and coerce it to the Topic shape."""
    cleaned = _FENCE_END.sub("", _FENCE_START.sub("", answer.strip())).strip()
    try:
        raw = json.loads(cleaned)
    except ValueError:
        # Some models wrap the JSON in prose despite the instructions.
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        try:
            raw = json.loads(m.group(0)) if m else {}
        except ValueError:
            raw = {}
    topics = raw.get("topics") if isinstance(raw, dict) else None
    out = []
    for t in topics if isinstance(topics, list) else []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        docs = t.get("documents") or t.get("docs") or []
        out.append({
            "name": str(t.get("name", "")),
            "description": str(t.get("description") or ""),
            "dateRange": str(t.get("dateRange") or ""),
            "documents": [{"path": str(d.get("path", "")), "title": str(d.get("title") or ""),
                           "date": str(d.get("date") or "")}
                          for d in docs if isinstance(d, dict) and d.get("path")],
        })
    return out


def _normalise_scope(scope: str) -> str:
    scope = "/" + "/".join(p for p in (scope or "/").split("/") if p)
    return scope


def generate(svc: "IndexerService", ws: str, fs: str, scope: str = "/") -> dict:
    """Build, store and return a survey. Raises LLMUnavailable / LLMError / EmbedError."""
    scope = _normalise_scope(scope)
    svc.llm_required()
    excerpts: dict[tuple[str, int], dict] = {}
    if svc.store.chunk_count(ws, fs):
        vecs = svc.embedder.embed_texts(PROBES)  # fixed strings: cached after the first run
        for vec in vecs:
            hits = svc.vectors.search(vec, HITS_PER_PROBE, {(ws, fs)}, path_prefix=scope)
            rows = svc.store.chunk_rows([pid for pid, _ in hits])
            for pid, _ in hits:
                r = rows.get(pid)
                if r is not None:
                    excerpts.setdefault((r["path"], r["chunk_index"]), r)
    picked = list(excerpts.values())[:MAX_EXCERPTS]
    generated_at = now_iso()
    if not picked:
        return {"topics": [], "raw": "", "sources": [], "generatedAt": generated_at,
                "stale": False, "empty": True}
    body = "\n---\n".join(f"PATH: {e['path']}\nEXCERPT: {e['text'][:EXCERPT_CHARS]}" for e in picked)
    answer = svc.llm.chat(SURVEY_SYSTEM, f"Filespace: {fs}\nScope: {scope}\n\n{body}")
    payload = {"topics": parse_topics(answer), "raw": answer,
               "sources": list(dict.fromkeys(e["path"] for e in picked))}
    svc.store.put_survey(ws, fs, scope, json.dumps(payload), generated_at)
    return {**payload, "generatedAt": generated_at, "stale": False}


def stored(svc: "IndexerService", ws: str, fs: str, scope: str = "/") -> dict | None:
    row = svc.store.get_survey(ws, fs, _normalise_scope(scope))
    if row is None:
        return None
    return {**json.loads(row["payload"]), "generatedAt": row["generated_at"], "stale": bool(row["stale"])}
