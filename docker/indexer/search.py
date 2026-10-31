"""Hybrid search: meaning (vectors) + keywords (SQLite FTS5) + file names.

Used by POST /index/search ("All search") and Ask retrieval (docs/INDEXER.md,
"Search quality: keywords and names" and "All search").

Three ranked chunk lists are fused with Reciprocal Rank Fusion (k = 60):
  meaning  nearest chunks by embedding (the vector index, as before)
  keyword  BM25 over chunk text (chunks_fts), CamelCase/compound variants included
  name     first chunk of files whose folded name contains the folded query
plus boosts for chunks that literally contain the query phrase and for files
whose name matches, so a document that says "ThinkTank" always outranks ones
that are merely about similar things. `score` is that fused value divided by the
best possible for the signals that fired (0–1, 1 = top of every signal and both
boosts). Until the keyword index is ready only meaning runs and `score` stays
the cosine similarity, exactly as before.
"""
from __future__ import annotations

import math
import re
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from .block import KIND_LUCIDLINK, type_of
from .embedder import EmbedError
from .store import fold_name

if TYPE_CHECKING:
    from .service import IndexerService
    from .vectors import Scope

RRF_K = 60
POOL = 200            # chunks per channel (and refinements are computed from these)
NAME_POOL = 50
PHRASE_BOOST = 0.05   # > three first places (3/61): a literal mention beats any non-mention
NAME_BOOST = 0.04
MEANING_DROP = 0.15   # a chunk "matches by meaning" within this of the best similarity…
MEANING_FLOOR = 0.2   # …and above this
SNIPPET_CHARS = 300
SNIPPET_LEAD = 80
FILE_SNIPPETS = 3
FILES_MAX = 100
REFINE_MAX = 10
MAX_TOKENS = 12
KEYWORD_RANK_MAX = 20000   # chunks: above this a term is too common to BM25-rank every match
MODES = ("all", "meaning", "keyword")

# GET /index/bykind and filters.kind: the app's kinds onto type classes.
KIND_CLASSES = {
    "documents": ("document", "pdf", "spreadsheet"),
    "presentations": ("presentation",),
    "images": ("image", "design"),
    "video": ("video", "audio"),
}
KIND_LABELS = {"documents": "Documents", "presentations": "Presentations", "images": "Images", "video": "Video"}
_CLASS_KIND = {c: k for k, cs in KIND_CLASSES.items() for c in cs}

_WORD = re.compile(r"[^\W_]+")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")
_SEP = r"[\s\-_.]*"
_TERM = re.compile(r"[^\W\d_][^\W_]{2,}")

STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before being below between
both but by can could did do does doing down during each few for from further had has have having he her here
hers herself him himself his how i if in into is it its itself just me more most my myself no nor not now of off
on once only or other our ours ourselves out over own same she should so some such than that the their theirs
them themselves then there these they this those through to too under until up very was we were what when where
which while who whom why will with would you your yours yourself yourselves detail details page pages file files
document documents new one two three may might must shall per via etc using use used get got make made like
""".split())


QUESTION_WORDS = frozenset("say said says tell show find give know anything something please there".split())


def _fold(text: str) -> str:
    """Lowercase without diacritics, as the FTS tokenizer (remove_diacritics 2) sees it."""
    if text.isascii():
        return text.lower()
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def _quote(t: str) -> str:
    return '"' + t.replace('"', '""') + '"'


class QueryPlan:
    """Everything derived from the query string. Never raises on odd input:
    tokens are runs of letters/digits, so quotes, colons, hyphens and asterisks
    can never reach FTS5 as syntax."""

    def __init__(self, query: str, any_term: bool = False):
        """any_term: a natural-language question (Ask), so keyword matching is
        any content word (stopwords dropped), ranked by BM25, instead of all words."""
        raw = _WORD.findall(query or "")[:MAX_TOKENS]
        self.any_term = any_term
        if any_term:
            raw = [t for t in raw if t.lower() not in STOPWORDS and t.lower() not in QUESTION_WORDS] or raw
        self.tokens = [t.lower() for t in raw]
        self.parts = [p.lower() for t in raw for p in _CAMEL.sub(" ", t).split()]
        self.compact = fold_name("".join(raw))
        self.words = [fold_name(w) for w in raw]
        self.match: Optional[str] = None
        self.prefix: Optional[str] = None
        self.phrase: Optional[re.Pattern] = None
        self.marks: Optional[re.Pattern] = None
        if not raw:
            return
        terms = []
        for i, t in enumerate(raw):
            low = t.lower()
            term = _quote(low) + ("*" if i == len(raw) - 1 and len(low) >= 3 else "")
            sub = _CAMEL.sub(" ", t).split()
            if len(sub) > 1:          # ThinkTank → also the phrase "think tank"
                term = f"({term} OR {_quote(' '.join(s.lower() for s in sub))})"
            terms.append(term)
        expr = (" OR " if any_term else " AND ").join(terms)
        if len(raw) > 1:              # think tank → also "thinktank"
            expr = f"({expr}) OR {_quote(''.join(self.tokens))}"
        self.match = expr
        self.prefix = self.tokens[-1] if len(self.tokens[-1]) >= 3 else None
        # The phrase with any (or no) space/hyphen/underscore/dot between its parts:
        # think tank, ThinkTank, Think-Tank, think_tank.
        body = _SEP.join(re.escape(p) for p in self.parts)
        self.phrase = re.compile(rf"(?<![^\W_]){body}(?:s|es)?(?![^\W_])", re.I)
        alts = [body] + sorted({re.escape(t) for t in self.tokens + self.parts if len(t) >= 2}, key=len, reverse=True)
        self.marks = re.compile(rf"(?<![^\W_])(?:{'|'.join(alts)})", re.I)

    def mentions(self, text: str) -> bool:
        return bool(self.phrase and self.phrase.search(text))

    def name_matches(self, name: str) -> bool:
        return len(self.compact) >= 2 and self.compact in fold_name(name)


@dataclass
class Filters:
    kind: Optional[str] = None
    path_prefix: Optional[str] = None
    modified_after: Optional[float] = None
    modified_before: Optional[float] = None
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    in_name: bool = False
    raw: dict = field(default_factory=dict)   # as sent, echoed into refinement patches

    @classmethod
    def parse(cls, d: Optional[dict], path_prefix: Optional[str] = None) -> "Filters":
        d = {k: v for k, v in (d or {}).items() if v not in (None, [], "")}
        return cls(kind=d.get("kind"), path_prefix=d.get("pathPrefix") or path_prefix,
                   modified_after=_ts(d.get("modifiedAfter")), modified_before=_ts(d.get("modifiedBefore")),
                   include=[t for t in d.get("include", []) if str(t).strip()],
                   exclude=[t for t in d.get("exclude", []) if str(t).strip()],
                   in_name=bool(d.get("inName")), raw=d)


def _ts(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _iso_day(y: int) -> str:
    return f"{y:04d}-01-01T00:00:00Z"


def _mtime_s(v) -> Optional[float]:
    from .service import mtime_seconds
    return mtime_seconds(v)


def _mtime_iso(v) -> Optional[str]:
    from .service import mtime_iso
    return mtime_iso(v)


@dataclass
class Hit:
    pid: int
    row: dict
    vec_rank: Optional[int] = None
    sim: float = 0.0
    kw_rank: Optional[int] = None
    name_rank: Optional[int] = None
    meaning: bool = False
    keyword: bool = False
    name: bool = False
    phrase: bool = False
    score: float = 0.0

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.row["workspace_id"], self.row["filespace"], self.row["file_id"])


@dataclass
class Retrieval:
    hits: list[Hit]                 # filtered, fused, best first
    hybrid: bool                    # False: meaning only, score = cosine (keyword index not ready)
    capped: bool                    # a channel hit its pool size (totals are lower bounds)
    keyword_ready: bool
    meaning_ok: bool = True


# ---------------------------------------------------------------- file limits
MAX_FILE_REFS = 500


@dataclass
class FileSet:
    """A request limited to some files (`files: [FileRef]`): FileRefs resolved
    once against the ledger. Refs not in the ledger (or deleted) are ignored."""
    rows: list[dict]                       # live ledger rows, newest first per path
    _pids: Optional[set[int]] = None

    @property
    def keys(self) -> set[tuple[str, str, str]]:
        return {(r["workspace_id"], r["filespace"], r["file_id"]) for r in self.rows}

    @property
    def scopes(self) -> "set[Scope]":
        return {(r["workspace_id"], r["filespace"]) for r in self.rows}

    def point_ids(self, store) -> set[int]:
        if self._pids is None:
            self._pids = store.point_ids_of(self.keys)
        return self._pids

    def within(self, scopes: "set[Scope]") -> "FileSet":
        return FileSet([r for r in self.rows if (r["workspace_id"], r["filespace"]) in scopes])


def ref_key(ref: dict) -> tuple[str, str, str]:
    return (ref["workspaceId"], ref["filespace"], ref["path"])


def resolve_files(svc: "IndexerService", refs: list[dict]) -> FileSet:
    return FileSet(svc.store.files_at_paths([ref_key(r) for r in refs]))


def file_scopes(svc: "IndexerService", workspace_id: Optional[str], filespace: Optional[str],
                files: FileSet) -> "set[Scope]":
    """Scopes of a file-limited request: the refs' own locations, narrowed by an
    explicit workspaceId ("*" = any) and/or filespace when the body gives them."""
    out = files.scopes
    if workspace_id and workspace_id != "*":
        out = {s for s in out if s[0] == workspace_id}
    if filespace:
        out = {s for s in out if s[1] == filespace}
    return out


def retrieve(svc: "IndexerService", scopes: "set[Scope]", query: str, mode: str = "all",
             filters: Optional[Filters] = None, pool: int = POOL, question: bool = False,
             only: Optional[FileSet] = None, qcache: Optional[dict] = None) -> Retrieval:
    """Run the channels, apply filters and fuse. Raises EmbedError only when
    meaning is the only signal available. question=True: Ask (any content word).
    only: limit every channel (meaning, keyword, name) to these files.
    qcache: reuse a query embedding across calls of one request."""
    filters = filters or Filters()
    plan = QueryPlan(query, any_term=question)
    store = svc.store
    if only is not None:
        only = only.within(scopes)
        if not only.rows:
            return Retrieval([], store.fts_ready and mode != "meaning", False, store.fts_ready)
    keys = only.keys if only is not None else None
    kw_ready = store.fts_ready
    prefix = filters.path_prefix
    if mode == "keyword" and not kw_ready:
        mode = "meaning"              # nothing else to offer until the index is built
    vec: list[tuple[int, float]] = []
    kw: list[tuple[int, float]] = []
    names: list[int] = []
    meaning_ok = True
    if mode in ("all", "keyword") and kw_ready:
        kw = (store.keyword_search(scopes, plan.match, pool, prefix, ranked=_rankable(store, plan), files=keys)
              if plan.match else [])
        names = store.name_chunk_hits(scopes, plan.compact, NAME_POOL, prefix, files=keys)
    if mode in ("all", "meaning"):
        try:
            if qcache is not None and query in qcache:
                qv = qcache[query]
            else:
                qv = svc.embedder.embed_query(query)
                if qcache is not None:
                    qcache[query] = qv
            vec = svc.vectors.search(qv, pool, scopes, prefix,
                                     point_ids=only.point_ids(store) if only is not None else None)
        except EmbedError:
            if mode == "meaning" or not (kw or names):
                raise
            meaning_ok = False        # keyword results still answer
    hybrid = kw_ready and mode != "meaning"
    capped = len(vec) >= pool or len(kw) >= pool or len(names) >= NAME_POOL

    rows = store.chunk_rows(list(dict.fromkeys([p for p, _ in vec] + [p for p, _ in kw] + names)))
    hits: dict[int, Hit] = {}

    def hit(pid: int) -> Optional[Hit]:
        if pid not in rows:
            return None
        if keys is not None and (rows[pid]["workspace_id"], rows[pid]["filespace"], rows[pid]["file_id"]) not in keys:
            return None
        return hits.setdefault(pid, Hit(pid, rows[pid]))

    top_sim = vec[0][1] if vec else 0.0
    for i, (pid, sim) in enumerate(vec):
        h = hit(pid)
        if h:
            h.vec_rank, h.sim = i + 1, sim
            h.meaning = sim >= max(MEANING_FLOOR, top_sim - MEANING_DROP)
    for i, (pid, _) in enumerate(kw):
        h = hit(pid)
        if h:
            h.kw_rank, h.keyword = i + 1, True
    for i, pid in enumerate(names):
        h = hit(pid)
        if h:
            h.name_rank = i + 1
    for h in hits.values():
        h.phrase = plan.mentions(h.row["text"])
        h.keyword = h.keyword or h.phrase
        h.name = plan.name_matches(h.row["path"].rsplit("/", 1)[-1])

    out = _apply_filters(svc, list(hits.values()), filters, plan)
    if hybrid:
        fired = [lst for lst in (vec, kw, names) if lst]
        ideal = len(fired) / (RRF_K + 1)
        ideal += PHRASE_BOOST if any(h.phrase for h in out) else 0.0
        ideal += NAME_BOOST if any(h.name for h in out) else 0.0
        for h in out:
            raw = sum(1.0 / (RRF_K + r) for r in (h.vec_rank, h.kw_rank, h.name_rank) if r is not None)
            raw += (PHRASE_BOOST if h.phrase else 0.0) + (NAME_BOOST if h.name else 0.0)
            h.score = min(1.0, raw / ideal) if ideal else 0.0
    else:
        for h in out:
            h.score = h.sim
    out.sort(key=lambda h: (-h.score, h.vec_rank or 1e9, h.pid))
    return Retrieval(out, hybrid, capped, kw_ready, meaning_ok)


def _rankable(store, plan: QueryPlan) -> bool:
    """False when every match set is huge: the rarest required token (a cheap
    estimate from the FTS vocabulary) appears in more than KEYWORD_RANK_MAX chunks."""
    docs = store.term_docs(plan.tokens)
    est = [docs.get(t, 0) for t in plan.tokens[:-1]] if len(plan.tokens) > 1 else []
    last = plan.tokens[-1]
    est.append(store.prefix_docs(last) if plan.prefix else docs.get(last, 0))
    # AND: the rarest token bounds the matches; OR (Ask): every token adds its own.
    return (sum(est) if plan.any_term else min(est)) <= KEYWORD_RANK_MAX


def _apply_filters(svc, hits: list[Hit], f: Filters, plan: QueryPlan) -> list[Hit]:
    if f.kind:
        classes = KIND_CLASSES.get(f.kind, ())
        hits = [h for h in hits if type_of(h.row["path"]) in classes]
    if f.modified_after is not None:
        hits = [h for h in hits if (_mtime_s(h.row["mtime"]) or 0) >= f.modified_after]
    if f.modified_before is not None:
        hits = [h for h in hits if (_mtime_s(h.row["mtime"]) or 0) < f.modified_before]
    if f.in_name:
        hits = [h for h in hits if h.name]
    for term in f.include:
        q = QueryPlan(term)
        ok = svc.store.files_mentioning({h.key for h in hits}, q.match) if q.match else set()
        hits = [h for h in hits if h.key in ok]
    for term in f.exclude:
        q = QueryPlan(term)
        bad = svc.store.files_mentioning({h.key for h in hits}, q.match) if q.match else set()
        hits = [h for h in hits if h.key not in bad]
    return hits


# ---------------------------------------------------------------- snippets
def snippet(text: str, plan: Optional[QueryPlan]) -> tuple[str, bool, list[list[int]]]:
    """(snippet, keyword, highlights): starts near the first mention when there is one."""
    flat = " ".join(text.split())
    m = None
    if plan is not None and plan.marks is not None:
        m = (plan.phrase.search(flat) if plan.phrase else None) or plan.marks.search(flat)
    start = 0
    if m and m.start() > SNIPPET_LEAD:
        start = flat.rfind(" ", 0, m.start() - SNIPPET_LEAD) + 1
        if m.start() - start > SNIPPET_LEAD + 40:
            start = m.start() - SNIPPET_LEAD
    body = flat[start:start + SNIPPET_CHARS]
    lead = "…" if start > 0 else ""
    out = lead + body + ("…" if start + SNIPPET_CHARS < len(flat) else "")
    marks = []
    if plan is not None and plan.marks is not None:
        marks = [[mm.start() + len(lead), mm.end() + len(lead)] for mm in plan.marks.finditer(body)]
    return out, bool(m), marks


# ---------------------------------------------------------------- All search
def search(svc: "IndexerService", workspace_id: Optional[str], filespace: Optional[str], query: str,
           k: int = 20, path_prefix: Optional[str] = None, mode: str = "all",
           filters: Optional[dict] = None, files: Optional[list[dict]] = None) -> dict:
    """files: [FileRef] limits every result to those files (docs: Collections)."""
    mode = mode if mode in MODES else "all"
    f = Filters.parse(filters, path_prefix)
    only = None
    if files is not None:
        only = resolve_files(svc, files)
        scopes = file_scopes(svc, workspace_id, filespace, only)
    else:
        scopes = svc.scopes(workspace_id, filespace)
    empty = {"results": [], "files": [], "total": 0, "counts": {"meaning": 0, "keyword": 0, "both": 0, "name": 0},
             "refinements": []}
    if not scopes or not svc.store.has_chunks(scopes):
        return empty
    ret = retrieve(svc, scopes, query, mode, f, only=only)
    plan = QueryPlan(query)
    kinds = svc.kinds()
    locs = svc.locations()
    kmax = max(1, min(int(k), 200))

    results = []
    for h in ret.hits[:kmax]:
        r = h.row
        text, _, _ = snippet(r["text"], plan if ret.hybrid or h.phrase else None)
        if not (ret.hybrid or h.phrase):
            flat = " ".join(r["text"].split())
            text = flat[:SNIPPET_CHARS] + ("…" if len(flat) > SNIPPET_CHARS else "")
        results.append({
            "workspaceId": r["workspace_id"], "filespace": r["filespace"],
            "kind": kinds.get((r["workspace_id"], r["filespace"]), KIND_LUCIDLINK), "path": r["path"],
            "location": locs.get((r["workspace_id"], r["filespace"])),
            "chunkIndex": r["chunk_index"], "score": round(h.score, 4), "snippet": text,
            "mtime": _mtime_iso(r["mtime"]),
        })

    files = _group_files(ret, plan, mode)
    total = len(files)
    counts = {"meaning": sum(1 for x in files if x["why"]["meaning"]),
              "keyword": sum(1 for x in files if x["why"]["keyword"]),
              "both": sum(1 for x in files if x["why"]["meaning"] and x["why"]["keyword"]),
              "name": sum(1 for x in files if x["why"]["name"])}
    refinements = _refinements(svc, files, plan, mode, f, scopes, query)
    fmax = max(1, min(int(k), FILES_MAX))
    out_files = [_file_out(x["_row"], x["score"], x["why"], x["snippets"], kinds, locs) for x in files[:fmax]]
    body = {"results": results, "files": out_files, "total": total, "counts": counts, "refinements": refinements}
    if ret.capped:
        body["totalApprox"] = True
    if not ret.keyword_ready:
        body["keywordReady"] = False
    if not ret.meaning_ok:
        body["meaningUnavailable"] = True
    return body


def _file_out(r: dict, score: float, why: dict, snippets: list, kinds: dict, locs: dict) -> dict:
    """One SearchFile from a chunk or ledger row (workspace_id, filespace, path, size, mtime)."""
    key = (r["workspace_id"], r["filespace"])
    name = r["path"].rsplit("/", 1)[-1]
    return {
        "workspaceId": r["workspace_id"], "filespace": r["filespace"],
        "kind": kinds.get(key, KIND_LUCIDLINK), "location": locs.get(key),
        "path": r["path"], "name": name, "size": r.get("size"), "mtime": _mtime_iso(r["mtime"]),
        "type": type_of(name), "score": round(score, 4), "why": why, "snippets": snippets,
    }


# ---------------------------------------------------------------- collections
COLLECTION_K = 200
COLLECTION_K_MAX = 500
NO_MATCH = {"meaning": False, "keyword": False, "name": False, "mentions": 0}


def collection_files(svc: "IndexerService", query: str, mode: str = "all", filters: Optional[dict] = None,
                     scope: Optional[dict] = None, pinned: Optional[list[dict]] = None,
                     excluded: Optional[list[dict]] = None, k: int = COLLECTION_K) -> dict:
    """A collection's current members (docs: Collections): the saved search's
    file results (up to k) ∪ pinned − excluded; pinned first, then by score.
    Excluded wins over pinned. Pinned refs not live in the ledger are dropped."""
    mode = mode if mode in MODES else "all"
    k = max(1, min(int(k), COLLECTION_K_MAX))
    f = Filters.parse(filters)
    excl = {ref_key(r) for r in excluded or []}
    pin_order = {key: i for i, key in enumerate(dict.fromkeys(ref_key(r) for r in pinned or []))
                 if key not in excl}
    pins = resolve_files(svc, [{"workspaceId": a, "filespace": b, "path": c} for a, b, c in pin_order])
    # One row per pinned path (the newest live file there).
    pin_rows: dict[tuple, dict] = {}
    for r in pins.rows:
        pin_rows.setdefault((r["workspace_id"], r["filespace"], r["path"]), r)
    kinds = svc.kinds()
    locs = svc.locations()

    matched: list[dict] = []
    capped = False
    extra = {}
    if (query or "").strip():
        scopes = svc.scopes("*", None) if scope is None else svc.scopes(scope.get("workspaceId"),
                                                                       scope.get("filespace") or None)
        if scopes and svc.store.has_chunks(scopes):
            plan = QueryPlan(query)
            qcache: dict = {}
            ret = retrieve(svc, scopes, query, mode, f, qcache=qcache)
            capped = ret.capped
            if not ret.keyword_ready:
                extra["keywordReady"] = False
            if not ret.meaning_ok:
                extra["meaningUnavailable"] = True
            matched = [x for x in _group_files(ret, plan, mode)
                       if (x["_row"]["workspace_id"], x["_row"]["filespace"], x["_row"]["path"]) not in excl]
            # Pinned files the pool did not reach: ask the same search, limited to them.
            seen = {(x["_row"]["workspace_id"], x["_row"]["filespace"], x["_row"]["path"]) for x in matched}
            missing = FileSet([r for key, r in pin_rows.items() if key not in seen])
            if missing.rows:
                more = retrieve(svc, scopes, query, mode, f, only=missing, qcache=qcache)
                matched += _group_files(more, plan, mode)

    by_path = {(x["_row"]["workspace_id"], x["_row"]["filespace"], x["_row"]["path"]): x for x in matched}
    pinned_out = []
    for key, r in pin_rows.items():
        x = by_path.get(key)
        if x is not None:
            item = _file_out(x["_row"], x["score"], x["why"], x["snippets"], kinds, locs)
        else:
            item = _file_out(r, 0.0, dict(NO_MATCH), [], kinds, locs)
        item["pinned"] = True
        pinned_out.append((-item["score"], pin_order[key], item))
    pinned_out.sort(key=lambda t: (t[0], t[1]))
    rest = []
    for x in matched:
        key = (x["_row"]["workspace_id"], x["_row"]["filespace"], x["_row"]["path"])
        if key in pin_rows:
            continue
        if len(rest) >= k:
            break
        item = _file_out(x["_row"], x["score"], x["why"], x["snippets"], kinds, locs)
        item["pinned"] = False
        rest.append(item)
    total = len(set(by_path) | set(pin_rows))
    body = {"files": [t[2] for t in pinned_out] + rest, "total": total, **extra}
    if capped:
        body["totalApprox"] = True
    return body


def _group_files(ret: Retrieval, plan: QueryPlan, mode: str) -> list[dict]:
    """Chunk hits → matched files, best first, with why/mentions/snippets (internal `_row`, `_hits`)."""
    by_file: dict[tuple, list[Hit]] = defaultdict(list)
    for h in ret.hits:
        by_file[h.key].append(h)
    # Per-file rank in each channel: the order files first appear in it.
    ranks: dict[str, dict[tuple, int]] = {}
    for ch, attr in (("vec", "vec_rank"), ("kw", "kw_rank"), ("name", "name_rank")):
        ordered = sorted((h for h in ret.hits if getattr(h, attr) is not None), key=lambda h: getattr(h, attr))
        seen: dict[tuple, int] = {}
        for h in ordered:
            seen.setdefault(h.key, len(seen) + 1)
        ranks[ch] = seen
    fired = [ch for ch in ranks if ranks[ch]]
    ideal = len(fired) / (RRF_K + 1)
    ideal += PHRASE_BOOST if any(h.phrase for h in ret.hits) else 0.0
    ideal += NAME_BOOST if any(h.name for h in ret.hits) else 0.0
    files = []
    for key, hs in by_file.items():
        meaning = any(h.meaning for h in hs) and mode != "keyword"
        keyword = any(h.keyword for h in hs)
        name = hs[0].name
        phrase = any(h.phrase for h in hs)
        if mode == "keyword":
            matched = keyword or name
        elif mode == "meaning":
            matched = meaning
        else:
            matched = meaning or keyword or name
        if not ret.hybrid:
            matched = meaning
        if not matched:
            continue
        if ret.hybrid:
            raw = sum(1.0 / (RRF_K + ranks[ch][key]) for ch in fired if key in ranks[ch])
            raw += (PHRASE_BOOST if phrase else 0.0) + (NAME_BOOST if name else 0.0)
            score = min(1.0, raw / ideal) if ideal else 0.0
        else:
            score = max(h.sim for h in hs)
        mentions = 0
        if plan.marks is not None:
            mentions = sum(len(plan.marks.findall(" ".join(h.row["text"].split())))
                           for h in hs if h.keyword) if keyword else 0
        ordered = sorted(hs, key=lambda h: (not h.keyword, h.kw_rank or 1e9, -h.score))
        snips = []
        for h in ordered[:FILE_SNIPPETS]:
            text, kwd, marks = snippet(h.row["text"], plan)
            snips.append({"chunkIndex": h.row["chunk_index"], "text": text, "keyword": bool(h.keyword and kwd),
                          "highlights": marks})
        files.append({"_row": hs[0].row, "_hits": hs, "score": score,
                      "why": {"meaning": meaning, "keyword": keyword, "name": name, "mentions": mentions},
                      "snippets": snips})
    files.sort(key=lambda x: (-x["score"], x["_row"]["path"]))
    return files


# ---------------------------------------------------------------- refinements
def _refinements(svc, files: list[dict], plan: QueryPlan, mode: str, f: Filters, scopes, query: str) -> list[dict]:
    total = len(files)
    if total < 2:
        return []
    shown = query.strip()
    narrows = lambda n: 0 < n < total  # noqa: E731

    def patch_filters(**extra) -> dict:
        return {"filters": {**f.raw, **extra}}

    groups: dict[str, list[dict]] = defaultdict(list)

    def add(kind, rid, label, count, apply, weight=None):
        if narrows(count):
            groups[kind].append({"id": rid, "kind": kind, "label": label, "count": count, "apply": apply,
                                 "_w": count if weight is None else weight})

    # match: what the other modes would keep
    if mode == "all":
        add("match", "match:keyword", f"Only files that mention “{shown}”",
            sum(1 for x in files if x["why"]["keyword"] or x["why"]["name"]), {"mode": "keyword"})
        add("match", "match:meaning", "About it, even without the word",
            sum(1 for x in files if x["why"]["meaning"]), {"mode": "meaning"})
    if not f.in_name and plan.compact:
        add("match", "match:name", f"Name contains “{shown}”",
            sum(1 for x in files if x["why"]["name"]), patch_filters(inName=True))

    # location
    locs = Counter((x["_row"]["workspace_id"], x["_row"]["filespace"]) for x in files)
    if len(locs) > 1:
        for (ws, fs), n in locs.most_common():
            add("location", f"location:{ws}/{fs}", fs, n, {"scope": {"workspaceId": ws, "filespace": fs}})

    # folder: where matches cluster (prefer the deepest folder with the same coverage)
    folders: Counter = Counter()
    base = (f.path_prefix or "").rstrip("/")
    for x in files:
        segs = x["_row"]["path"].split("/")[1:-1]
        seen = set()
        for i in range(1, len(segs) + 1):
            p = "/" + "/".join(segs[:i])
            if len(p) > len(base) and p not in seen:
                seen.add(p)
                folders[p] += 1
    keep = []
    for p, n in sorted(folders.items(), key=lambda kv: (-kv[1], -kv[0].count("/"), kv[0])):
        if any(q.startswith(p + "/") and folders[q] == n for q in folders):
            continue
        keep.append((p, n))
    for p, n in keep[:6]:
        add("folder", f"folder:{p}", f"In {p}", n, patch_filters(pathPrefix=p))

    # type
    if not f.kind:
        kinds = Counter(_CLASS_KIND.get(type_of(x["_row"]["path"])) for x in files)
        for kd, n in kinds.most_common():
            if kd:
                add("type", f"type:{kd}", KIND_LABELS[kd], n, patch_filters(kind=kd))

    # date
    now = time.time()
    secs = [(_mtime_s(x["_row"]["mtime"]) or 0.0) for x in files]
    recent_cut = now - 30 * 86400
    add("date", "date:30d", "Changed in the last 30 days", sum(1 for s in secs if s >= recent_cut),
        patch_filters(modifiedAfter=datetime.fromtimestamp(recent_cut, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        weight=total)
    years = Counter(datetime.fromtimestamp(s, timezone.utc).year for s in secs if s > 0)
    if len(years) > 1:
        for y, n in years.most_common(2):
            add("date", f"date:{y}", str(y), n, patch_filters(modifiedAfter=_iso_day(y), modifiedBefore=_iso_day(y + 1)))
        latest = max(years)
        older = sum(n for y, n in years.items() if y < latest)
        add("date", f"date:before:{latest}", f"Before {latest}", older, patch_filters(modifiedBefore=_iso_day(latest)))

    # term: distinctive words in matched files' chunks, scored against corpus frequency
    for t, n, w in _distinctive_terms(svc, files, plan, f):
        add("term", f"term:{t}", f"+ “{t}”", n, patch_filters(include=[*f.include, t]), weight=w)

    order = ("match", "location", "folder", "type", "term", "date")
    for kd in order:
        groups[kd].sort(key=lambda r: -r["_w"])
    out: list[dict] = []
    i = 0
    while len(out) < REFINE_MAX and any(i < len(groups[kd]) for kd in order):
        for kd in order:
            if i < len(groups[kd]) and len(out) < REFINE_MAX:
                r = dict(groups[kd][i])
                r.pop("_w")
                out.append(r)
        i += 1
    return out


SAMPLE_CHUNKS = 2000
SAMPLE_TTL = 600.0


def _corpus_df(store) -> tuple[int, Counter]:
    """Chunk frequency of words in a random sample of the corpus, cached for
    SAMPLE_TTL (a small corpus, cheap to read, is re-read on any change). Exact
    counts from chunks_fts_vocab cost ~15 ms per common term on a million
    chunks; corpus word statistics barely move, so a sample is plenty to rank
    refinements."""
    now = time.monotonic()
    cached = getattr(store, "_corpus_sample", None)
    if cached is not None and now - cached[0] < SAMPLE_TTL and (
            cached[1] >= SAMPLE_CHUNKS // 2 or cached[3] == store.generation):
        return cached[1], cached[2]
    texts = store.sample_texts(SAMPLE_CHUNKS, seed=int(now))
    df: Counter = Counter()
    for t in texts:
        df.update(set(_TERM.findall(_fold(t))))
    store._corpus_sample = (now, len(texts), df, store.generation)
    return len(texts), df


def _distinctive_terms(svc, files: list[dict], plan: QueryPlan, f: Filters, top: int = 5) -> list[tuple[str, int, float]]:
    total = len(files)
    skip = set(plan.tokens) | set(plan.parts) | {_fold(t) for t in f.include}
    df: Counter = Counter()
    for x in files:
        words = set()
        for h in x["_hits"]:
            for w in _TERM.findall(_fold(h.row["text"])):
                if w not in STOPWORDS and w not in skip and (not plan.compact or plan.compact not in w):
                    words.add(w)
        df.update(words)
    cands = [(w, n) for w, n in df.most_common(40) if 0 < n < total and (n >= 2 or total <= 3)][:25]
    if not cands:
        return []
    n_sample, corpus = _corpus_df(svc.store)
    scored = []
    for w, n in cands:
        idf = math.log((n_sample + 1) / (corpus.get(w, 0) + 1)) + 0.1
        scored.append((w, n, (n / total) * idf))
    scored.sort(key=lambda t: -t[2])
    return scored[:top]
