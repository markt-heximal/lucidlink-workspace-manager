"""Related files (Collections step 2): from one file, follow the thread.

POST /index/related returns, for one file, the files linked to it by four kinds
of evidence, each with the reason it is there (docs/INDEXER.md, "Related
files"):

  versions     other drafts and exports of the same document: the same name
               once version words are stripped ("Proposal v2", "Proposal FINAL
               (signed).pdf"), or at least half of the text in common
  names        files sharing this file's distinctive names: words that are rare
               across the whole archive (a project, client, code or person)
  meaning      the closest files by meaning: file vectors (the mean of a file's
               chunk vectors) compared with each other
  references   files this one names ("see Budget 2024.xlsx") and files that
               name it

Time and place (same folder, edited within a week) only tips the order. All of
it comes from indexes the engine already has; no file is read again.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Iterable, Optional

from .block import KIND_LUCIDLINK, type_of
from .search import STOPWORDS, _corpus_df, _fold, _quote, QueryPlan, snippet
from .store import fold_name

if TYPE_CHECKING:
    from .service import IndexerService
    from .vectors import Scope

K = 10
K_MAX = 50
COLLECTION_MAX = 100
COPIES_WIDEN = 4             # each group reads k × this, so k remain once copies are folded

# Versions
VERSION_SHARE = 0.5          # share of the smaller file's passages in common
VERSION_SPREAD = 20          # a passage in more chunks than this is boilerplate
NAME_CANDIDATES = 500        # files scanned per base name
BASE_MIN = 4                 # shorter base names are too vague to call versions
SPECIFIC_BASE = 12           # a base name this long is specific however often it recurs
GENERIC_BASES = frozenset("""untitled document doc newdocument image img scan screenshot screenshots photo
picture notes note readme index file copy export download attachment page sheet book workbook presentation
slide slides draft final default template test temp tmp backup data output report""".split())
_VERSION_WORDS = (r"v|ver|version|rev|revision|draft|final|vfinal|vdraft|copy|signed|edited|edit|old|new|latest|"
                  r"updated|update|clean|redline|redlined|tracked|approved|wip|backup|bak|review|reviewed|comments")
_COPY_NO = re.compile(r"[(\[]\s*\d+\s*[)\]]")
_DATE = re.compile(r"(?<!\d)(?:19|20)\d{2}[-_. ]?[01]\d[-_. ]?[0-3]\d(?!\d)")
_VERSION = re.compile(rf"(?<![^\W_])(?:{_VERSION_WORDS})\s*\d*(?:[.]\d+)*(?![^\W_])", re.I)
_PUNCT = re.compile(r"[^\w\s]")
_V_NUM = re.compile(r"(?<![^\W_])v\d+(?:[.]\d+)*(?![^\W_])", re.I)

# Distinctive names
NAME_TERMS = 12              # names looked up per file
NAME_LOOKUPS = 80            # candidates checked against the keyword index
NAME_DF_FLOOR = 50           # a name may appear in at most max(this, …)
NAME_DF_RATIO = 0.001        # … or this share of all chunks, else it is too common
NAME_FILES_PER_TERM = 300    # chunks read per name
CAPS_MAX = 10                # longer all-capitals words are headings run together
_TOKEN = re.compile(r"[^\W_]+")
# Words fused by PDF text extraction: "14Shareholder", "Performance1".
_GLUED = re.compile(r"^\d+[A-Za-z]{3,}$|^[A-Z]?[a-z]{3,}\d+$")
_HONORIFIC = re.compile(r"(?:^|\s)(?:Mr|Mrs|Ms|Mx|Dr|Prof|Sir)\.?\s+$")
_BREAK = re.compile(r"[.!?:;\n\r\t•|]")
_DETERMINERS = frozenset("the a an our their this that these those".split())


def _name_like(text: str, start: int) -> bool:
    """A capitalised word reads as a name inside a sentence: after a lowercase
    word, a comma or a title (Mr, Dr…). At the start of a line, sentence or
    table cell it may be any word ("Smallest", "Meeting")."""
    before = text[max(0, start - 80):start]
    if _HONORIFIC.search(before):
        return True
    seg = _BREAK.split(before)[-1]
    if seg.rstrip().endswith(","):
        return True
    words = _TOKEN.findall(seg)
    if words and all(w.lower() in _DETERMINERS for w in words):
        return True                 # "The ThinkTank team…"
    return any(w[0].islower() for w in words)

# Meaning
MEANING_FLOOR = 0.5

# References
REF_WORDS = 6                # words before ".pdf" tried as a file name
REF_CANDIDATES = 200
REF_PER_NAME = 3
LISTING_REFS = 8             # a passage naming this many files is a listing, not a reference
_EXT = ("pdf|docx?|dotx|xlsx?|xlsm|csv|pptx?|key|pages|numbers|txt|md|rtf|odt|ods|odp|jpe?g|png|gif|heic|tiff?|"
        "psd|ai|indd|sketch|fig|mov|mp4|m4v|m4a|mp3|wav|zip|eml|msg|html?")
_FILE_REF = re.compile(rf"([^\n\t\"'“”‘’<>|:;*?/\\]{{1,160}}?)\.({_EXT})(?![^\W_])", re.I)

# Time and place
WEEK = 7 * 86400.0
NEAR_BONUS = 0.05


def version_base(name: str) -> str:
    """A file name with version words, copy numbers, dates and the extension
    stripped, folded like find (case, spaces, -, _ and . ignored):
    "Proposal v2.docx", "proposal_FINAL (signed).pdf" → "proposal"."""
    return fold_name(_PUNCT.sub(" ", _strip_versions(name)))  # what "(signed)" leaves behind


def _version_needle(name: str) -> str:
    """The longest run of the stripped name with no punctuation in it: what a
    stored folded name (which keeps "&", "[…]") must contain to share the base."""
    return max((fold_name(p) for p in _PUNCT.split(_strip_versions(name))), key=len)


def _strip_versions(name: str) -> str:
    stem = name.rsplit(".", 1)[0] if "." in name.lstrip(".") else name
    s = _fold(stem).replace("_", " ").replace("-", " ")
    s = _COPY_NO.sub(" ", s)
    s = _DATE.sub(" ", s)
    s = _V_NUM.sub(" ", s)
    prev = None
    while prev != s:                      # "final final", "signed copy"
        prev, s = s, _VERSION.sub(" ", s)
    return s


def distinctive_names(svc: "IndexerService", texts: list[str]) -> list[tuple[str, str, float]]:
    """[(folded term, as written, weight)] of the rarest names in `texts`, best
    first: capitalised words and codes ("ThinkTank", "Acme", "AB123") found in
    at most max(NAME_DF_FLOOR, NAME_DF_RATIO of all chunks) chunks, and at
    least one outside these texts."""
    store = svc.store
    tf: Counter = Counter()
    in_chunks: Counter = Counter()
    named: set[str] = set()                # read as a name at least once
    shown: dict[str, Counter] = defaultdict(Counter)
    for t in texts:
        seen = set()
        for m in _TOKEN.finditer(t):
            tok = m.group()
            # Codes start with a letter ("WS2008", "T03"); "2011F", "73x" are figures.
            if (len(tok) < 3 or tok[0].isdigit() or _GLUED.match(tok)
                    or (tok.isupper() and len(tok) > CAPS_MAX)):
                continue
            code = any(c.isdigit() for c in tok)
            if not (tok[0].isupper() or code):
                continue
            f = _fold(tok)
            if f in STOPWORDS:
                continue
            tf[f] += 1
            shown[f][tok] += 1
            seen.add(f)
            if f not in named and (code or _name_like(t, m.start())):
                named.add(f)
        in_chunks.update(seen)
    tf = Counter({f: n for f, n in tf.items() if f in named})
    if not tf:
        return []
    total = max(1, store.chunk_total())
    cap = max(NAME_DF_FLOOR, int(total * NAME_DF_RATIO))
    # A word the corpus sample sees often is common; skip it before the exact
    # (and, for common words, slow) vocabulary lookup.
    n_sample, sample = _corpus_df(store)
    scale = total / max(1, n_sample)
    cands = [f for f in tf if sample.get(f, 0) * scale <= cap]
    cands.sort(key=lambda f: (-tf[f], f))
    df = store.term_docs(cands[:NAME_LOOKUPS])
    out = []
    for f, n in df.items():
        if in_chunks[f] < n <= cap:
            weight = math.log(total / n) * (1.0 + math.log(tf[f]))
            out.append((f, shown[f].most_common(1)[0][0], weight))
    out.sort(key=lambda t: (-t[2], t[0]))
    return out[:NAME_TERMS]


def _mtime_s(v) -> Optional[float]:
    from .service import mtime_seconds
    return mtime_seconds(v)


def _mtime_iso(v) -> Optional[str]:
    from .service import mtime_iso
    return mtime_iso(v)


def _folder(path: str) -> str:
    return path.rsplit("/", 1)[0] or "/"


def _near(me: dict, r: dict) -> Optional[str]:
    """"folder" (same folder), "week" (edited within a week of each other) or None."""
    if (r["workspace_id"], r["filespace"], _folder(r["path"])) == (me["workspace_id"], me["filespace"],
                                                                 _folder(me["path"])):
        return "folder"
    a, b = _mtime_s(me["mtime"]), _mtime_s(r["mtime"])
    if a and b and abs(a - b) <= WEEK:
        return "week"
    return None


_NEAR_TEXT = {"folder": "in the same folder", "week": "edited the same week"}


def _with_near(reason: str, near: Optional[str]) -> str:
    return f"{reason}, {_NEAR_TEXT[near]}" if near else reason


def _list_names(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) <= 3:
        return ", ".join(names[:-1]) + " and " + names[-1]
    return ", ".join(names[:2]) + f" and {len(names) - 2} more"


class _Out:
    """Builds the shared file shape and remembers what each group used."""

    def __init__(self, svc: "IndexerService", me: dict):
        self.kinds = svc.kinds()
        self.locs = svc.locations()
        self.me = me

    def file(self, r: dict, reason: Optional[str] = None, **extra) -> dict:
        key = (r["workspace_id"], r["filespace"])
        name = r["path"].rsplit("/", 1)[-1]
        out = {"workspaceId": r["workspace_id"], "filespace": r["filespace"],
               "kind": self.kinds.get(key, KIND_LUCIDLINK), "location": self.locs.get(key),
               "path": r["path"], "name": name, "size": r.get("size"), "mtime": _mtime_iso(r["mtime"]),
               "type": type_of(name)}
        if reason is not None:
            near = _near(self.me, r)
            out.update({"reason": _with_near(reason, near), "near": near})
        out.update(extra)
        return out


def _key(r: dict) -> tuple[str, str, str]:
    return (r["workspace_id"], r["filespace"], r["file_id"])


def _versions(svc, me: dict, scopes: "set[Scope]") -> list[tuple[dict, bool, float]]:
    """[(row, by name, share of text in common)], newest first (all of them:
    every version is left out of the other groups, not only those shown)."""
    store = svc.store
    found: dict[tuple, list] = {}
    base = version_base(me["path"].rsplit("/", 1)[-1])
    if len(base) >= BASE_MIN and base not in GENERIC_BASES:
        # The stored folded name keeps punctuation ("&", "[…]"), so look for the
        # longest unbroken run of the name, then compare bases exactly.
        needle = _version_needle(me["path"].rsplit("/", 1)[-1])
        cands = store.files_name_containing(scopes, needle, NAME_CANDIDATES) if len(needle) >= BASE_MIN else []
        same = [r for r in cands if _key(r) != _key(me) and version_base(r["path"].rsplit("/", 1)[-1]) == base]
        # Hundreds of "proposal"s are not versions of one; a long, specific name
        # ("t03shareholdervaluemetrics") is, however many backup copies there are.
        if len(same) <= NAME_CANDIDATES // 10 or len(base) >= SPECIFIC_BASE:
            for r in same:
                found[_key(r)] = [r, True, 0.0]
    if me["status"] == "indexed":
        mine, shared = store.shared_passages(*_key(me), max_spread=VERSION_SPREAD)
        rows = store.live_rows(shared)
        for key, n in shared.items():
            r = rows.get(key)
            if r is None or (r["workspace_id"], r["filespace"]) not in scopes:
                continue
            share = n / max(1, min(mine, r["chunk_count"] or n))
            if share >= VERSION_SHARE and (n >= 2 or mine <= 2):
                found.setdefault(key, [r, False, 0.0])[2] = round(min(1.0, share), 2)
    out = [tuple(v) for v in found.values()]
    out.sort(key=lambda t: (-(_mtime_s(t[0]["mtime"]) or 0), t[0]["path"]))
    return out


def _version_reason(by_name: bool, share: float) -> str:
    pct = f"{round(share * 100)}%"
    if by_name and share:
        return f"Same name, and {pct} of the text in common"
    if by_name:
        return "Same name: another version"
    return f"{pct} of the text in common"


def _names(svc, me: dict, texts: list[str], scopes, skip: set, k: int):
    """(the file's names as written, [(row, shared names as written, score)])."""
    terms = distinctive_names(svc, texts)
    if not terms:
        return [], []
    score: dict[tuple, float] = defaultdict(float)
    shared: dict[tuple, list[str]] = defaultdict(list)
    for f, shown, w in terms:
        for key in svc.store.match_files(scopes, _quote(f), NAME_FILES_PER_TERM):
            if key == _key(me) or key in skip:
                continue
            score[key] += w
            shared[key].append(shown)
    rows = svc.store.live_rows(score)
    best = sorted(((rows[key], shared[key], s) for key, s in score.items() if key in rows),
                  key=lambda t: (-(t[2] + (NEAR_BONUS * t[2] if _near(me, t[0]) else 0.0)), t[0]["path"]))
    return [shown for _, shown, _ in terms], best[:k]


def _meaning(svc, me: dict, scopes, skip: set, k: int) -> list[tuple[dict, float]]:
    model = svc.embedder.model
    q = svc.store.get_file_vector(*_key(me), model)
    if q is None:
        got = svc.store.file_mean_vector(*_key(me), model)
        if got is None:
            return []
        q = got[0]
    hits = svc.file_index.search(q, k + len(skip) + 1 + k, scopes)
    keep = [(key, sim) for key, sim in hits if key != _key(me) and key not in skip and sim >= MEANING_FLOOR]
    rows = svc.store.live_rows(key for key, _ in keep)
    out = [(rows[key], sim) for key, sim in keep if key in rows]
    out.sort(key=lambda t: (-(t[1] + (NEAR_BONUS if _near(me, t[0]) else 0.0)), t[0]["path"]))
    return out[:k]


def _mentions(svc, me: dict, texts: list[str], scopes, k: int) -> list[tuple[dict, str]]:
    """Files this one names: (row, the name as written)."""
    occurrences: list[list[tuple[str, str]]] = []      # per mention: (fold, as written), longest first
    folds: set[str] = set()
    for t in texts:
        for m in _FILE_REF.finditer(t):
            words = m.group(1).split()
            if not words:
                continue
            tries = []
            for n in range(min(REF_WORDS, len(words)), 0, -1):
                written = " ".join(words[-n:]) + "." + m.group(2)
                tries.append((fold_name(written), written))
            occurrences.append(tries)
            folds |= {f for f, _ in tries}
            if len(folds) >= REF_CANDIDATES:
                break
    if not folds:
        return []
    by_fold: dict[str, list[dict]] = defaultdict(list)
    for r in svc.store.files_named(folds):
        if (r["workspace_id"], r["filespace"]) in scopes and _key(r) != _key(me):
            by_fold[r["name_fold"]].append(r)
    out: dict[tuple, tuple[dict, str]] = {}
    for tries in occurrences:
        for f, written in tries:                        # the longest name that is a real file
            if f in by_fold:
                rows = sorted(by_fold[f], key=lambda r: (_near(me, r) != "folder",
                                                         (r["workspace_id"], r["filespace"]) != (
                                                             me["workspace_id"], me["filespace"]),
                                                         -(_mtime_s(r["mtime"]) or 0)))
                for r in rows[:REF_PER_NAME]:
                    out.setdefault(_key(r), (r, written.strip()))
                break
    return list(out.values())[:k]


def _mentioned_by(svc, me: dict, scopes, k: int) -> list[tuple[dict, str]]:
    """Files that name this one: (row, a snippet around the mention)."""
    name = me["path"].rsplit("/", 1)[-1]
    tokens = _TOKEN.findall(_fold(name))
    stem = name.rsplit(".", 1)[0] if "." in name.lstrip(".") else ""
    if len(tokens) < 2 or len(fold_name(stem)) < BASE_MIN:
        return []
    hits = svc.store.match_files(scopes, _quote(" ".join(tokens)), NAME_FILES_PER_TERM)
    hits.pop(_key(me), None)
    rows = svc.store.live_rows(hits)
    chunks = svc.store.chunk_rows(list(hits.values()))
    plan = QueryPlan(name)
    out = []
    mine = name.casefold()
    for key, pid in hits.items():
        if key in rows and pid in chunks:
            other = rows[key]["path"].rsplit("/", 1)[-1].casefold()
            if other.endswith(".log") or other.startswith(mine + "."):
                continue            # a sync log, or a sidecar ("report.pdf.json") about this file
            if len(_FILE_REF.findall(chunks[pid]["text"])) >= LISTING_REFS:
                continue            # a folder listing names everything
            text, _, _ = snippet(chunks[pid]["text"], plan)
            out.append((rows[key], text))
    out.sort(key=lambda t: (_near(me, t[0]) is None, -(_mtime_s(t[0]["mtime"]) or 0), t[0]["path"]))
    return out[:k]


def _copy_key(r: dict) -> tuple:
    return (r["path"].rsplit("/", 1)[-1].casefold(), r.get("size"))


def _collapse(items: list, k: int, me: Optional[dict] = None) -> list[tuple]:
    """Backup copies (same name and size) shown once: [(item, other copies)],
    best first, at most k. Copies of `me` itself are left out."""
    out: dict[tuple, list] = {}
    mine = _copy_key(me) if me is not None else None
    for item in items:
        key = _copy_key(item[0])
        if key == mine:
            continue
        if key in out:
            out[key][1] += 1
        elif len(out) < k:
            out[key] = [item, 0]
    return [tuple(v) for v in out.values()]


def _copies(reason: str, n: int) -> str:
    if not n:
        return reason
    return f"{reason} (also in {n} other place{'s' if n > 1 else ''})"


def _ref(r: dict) -> dict:
    return {"workspaceId": r["workspace_id"], "filespace": r["filespace"], "path": r["path"]}


def related(svc: "IndexerService", me: dict, scopes: "set[Scope]", k: int = K) -> dict:
    """me: the file's ledger row. scopes: where related files may live."""
    k = max(1, min(int(k), K_MAX))
    out = _Out(svc, me)
    indexed = me["status"] == "indexed"
    texts = svc.store.file_texts(*_key(me)) if indexed else []

    every_version = _versions(svc, me, scopes)
    vkeys = {_key(r) for r, _, _ in every_version}
    # Each group asks for more than k, then shows backup copies once.
    wide = k * COPIES_WIDEN
    names_shown, names = (_names(svc, me, texts, scopes, vkeys, wide) if texts and svc.store.fts_ready
                          else ([], []))
    versions = _collapse(every_version, k)
    names = _collapse(names, k, me)
    meaning = _collapse(_meaning(svc, me, scopes, vkeys, wide) if indexed else [], k, me)
    mentions = _collapse(_mentions(svc, me, texts, scopes, wide) if texts else [], k, me)
    mentioned_by = _collapse(_mentioned_by(svc, me, scopes, wide) if svc.store.fts_ready else [], k, me)

    body = {
        "file": {**out.file(me), "indexed": indexed},
        "versions": [out.file(r, _copies(_version_reason(by_name, share), n), copies=n,
                              version={"byName": by_name, "shared": share})
                     for (r, by_name, share), n in versions],
        "names": {"terms": names_shown,
                  "files": [out.file(r, _copies(f"Both mention {_list_names(shown)}", n), copies=n, names=shown)
                            for (r, shown, _), n in names]},
        "meaning": [out.file(r, _copies("About the same thing", n), copies=n, similarity=round(sim, 4))
                    for (r, sim), n in meaning],
        "references": {
            "mentions": [out.file(r, _copies(f"This file names {written}", n), copies=n)
                         for (r, written), n in mentions],
            "mentionedBy": [out.file(r, _copies("Names this file", n), copies=n, snippet=text)
                            for (r, text), n in mentioned_by],
        },
    }
    # Make a collection from here: the file and what is related to it, as pins.
    pins: dict[tuple, dict] = {_key(me): _ref(me)}
    for group in (versions, mentions, mentioned_by, names, meaning[:5]):
        for item, _ in group:
            pins.setdefault(_key(item[0]), _ref(item[0]))
    stem = me["path"].rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem.lstrip(".") else stem
    body["collection"] = {"name": stem[:80].strip() or "Related files",
                          "pinned": list(pins.values())[:COLLECTION_MAX]}
    if not svc.store.fts_ready:
        body["keywordReady"] = False
    if indexed and not svc.file_vectors_ready:
        body["meaningPartial"] = True
    return body
