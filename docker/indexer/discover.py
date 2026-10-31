"""Discovered collections (Collections step 3): groups of files the engine
proposes on its own (docs/INDEXER.md, "Discovered collections").

For each location the owner switches on, a background run links its files by
the evidence Related files uses, all from indexes the engine already has:

  names       a name both mention that is rare across the archive and in at
              most NAME_NODES_MAX files here (strong; rarer is stronger)
  versions    the same name once version words are stripped, or at least half
              of the text in common (strong)
  references  one file names the other (strong)
  meaning     each is among the other's closest files by meaning (medium)
  time/place  same folder, or edited the same week (weak: only tips the balance)

Strengths combine as independent evidence (1 - Π(1 - s)) and a link needs
LINK_MIN, so meaning alone links only the closest pairs. Backup copies (same
name and size) and ".json" sidecars are folded into one file first. Groups are
the communities of the link graph (Louvain modularity; a community over
GROUP_MAX is split again at a finer resolution) holding at least GROUP_MIN
different documents, so a file and its drafts alone are not a collection.

Groups are proposed, not imposed: the owner keeps one (it becomes a collection),
renames, merges or dismisses it. The next run recognises a group by its files
(MATCH_SAME in common), so a dismissed group stays dismissed and a name stays.
The local AI names proposals afterwards, strongest first, one at a time; until
then a group carries a name made from its shared names or folder.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import uuid
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterable, Optional

import numpy as np

from . import related as rel
from .search import _quote
from .store import fold_name, now_iso

if TYPE_CHECKING:
    from .service import IndexerService
    from .vectors import Scope

logger = logging.getLogger("indexer.discover")

# Groups
GROUP_MIN = 3                # different documents (versions folded) for a group to count
GROUP_MAX = 60               # files (copies folded) per group; bigger communities are split
RESOLUTIONS = (1.0, 2.0, 4.0, 8.0)
GROUP_NAMES = 8              # names shown per group
GROUP_NAME_SHARE = 0.2       # a group's name is in at least this share of its files (and 2)

# Links
LINK_MIN = 0.5
EDGES_PER_NODE = 15          # strongest links kept per file
S_VERSION = 0.9
S_REFERENCE = 0.8
S_MEANING_CLOSE = 0.5        # each in the other's MEANING_CLOSE closest
S_MEANING = 0.35             # each in the other's MEANING_K closest
S_NEAR = 0.1
MEANING_K = 10
MEANING_CLOSE = 3
MEANING_MIN = 0.7
NAME_NODES_MAX = 40          # a name in more files of the location links nothing
NAME_STRENGTH = ((5, 0.7), (15, 0.55), (NAME_NODES_MAX, 0.4))
LINK_DF_RATIO = 0.0002       # … nor one in more than this share of all chunks (and NAME_DF_FLOOR)
NEAR_FOLDER_MAX = 50         # a folder this big says nothing about two files in it
NEAR_WEEK_MAX = 100          # nor does a week in which this many files were saved
TEXT_CHUNKS = 40             # chunks read per file for names and references
VERSION_SPREAD = 8           # a passage in more files is boilerplate
BASE_GROUP_MAX = 50          # more files with one base name are not versions of one document
REF_PER_FILE = 200
# Only documents people write, outside system folders (old computers' backups
# hold browser caches, app bundles and logs that link to each other strongly).
KINDS = frozenset({"document", "spreadsheet", "presentation", "pdf"})
SYSTEM_DIRS = frozenset("""temporary internet files|content.ie5|content.outlook|local settings|application data|
appdata|cache|caches|temp|tmp|logs|log|node_modules|.git|__pycache__|site-packages|program files|
program files (x86)|system32|library|cookies|outlooksettings|.trash|$recycle.bin|
recycler""".replace("\n", "").split("|"))
SYSTEM_SUFFIXES = (".app", ".bundle", ".framework", ".lproj", ".plugin", ".pkg", ".photoslibrary")

# Bits recording why two files are linked
B_NAMES, B_VERSION, B_REF_AB, B_REF_BA, B_MEANING, B_NEAR = 1, 2, 4, 8, 16, 32

# Runs
MATCH_SAME = 0.5             # share of files in common: the same group as last run
MATCH_KEEP_NAME = 0.7        # … and close enough to keep the AI's name
RERUN_EVERY = 86400.0
BLOCK, CHUNK = 1024, 8192    # meaning comparisons per step (rows × columns)

# Naming
LLM_FILES = 30
EXCERPT = 300
NAME_MAX = 80
SUMMARY_MAX = 300
NAME_SYSTEM = (
    "You name one group of related files from a person's document archive, so they can tell it apart from "
    "hundreds of other groups. Reply with JSON only: {\"name\": \"...\", \"summary\": \"...\"}. "
    "The name: 3 to 7 words saying what is specific to these files: which organisation, project, deal, "
    "committee, person or event, and what kind of documents (minutes, audit packs, 10-K drafts, pitch decks, "
    "invoices...). Use the recurring names when they identify it. Never a generic name such as "
    "\"Board Documents\" or \"Project Files\", never a name from the already-used list, no file counts, no "
    "quotation marks. The summary: one plain sentence on what the files are and the years they span."
)
USED_NAMES = 40

# Suggestions for a collection
SUGGEST_K = 20
SUGGEST_MEMBERS = 30
SUGGEST_VERSION_MEMBERS = 10
SUGGEST_NAME_HITS = 300


class Stopped(Exception):
    pass


@dataclass
class Node:
    """One document: the newest of its backup copies, with the copies and sidecars."""
    rep: dict
    files: list = field(default_factory=list)
    base: str = ""
    copies: int = 0


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def link_cap(store) -> int:
    """Chunks a name may appear in, across the archive, and still link files."""
    return max(rel.NAME_DF_FLOOR, int(max(1, store.chunk_total()) * LINK_DF_RATIO))


def _strength(n_files: int) -> float:
    for limit, s in NAME_STRENGTH:
        if n_files <= limit:
            return s
    return 0.0


def wanted(path: str) -> bool:
    """A document a person wrote: a document, spreadsheet, presentation or PDF
    outside system folders (caches, app bundles, logs, temp)."""
    if rel.type_of(_name(path)) not in KINDS:
        return False
    for part in path.split("/")[:-1]:
        p = part.casefold()
        if p in SYSTEM_DIRS or p.endswith(SYSTEM_SUFFIXES):
            return False
    return True


def fold_nodes(rows: list[dict]) -> list[Node]:
    """Files → documents: backup copies (same name and size) and "x.pdf.json"
    sidecars of an indexed "x.pdf" become one node, the newest copy first."""
    by_path = {r["path"]: r for r in rows}
    sidecars: dict[str, list[dict]] = defaultdict(list)
    copies: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        name = _name(r["path"])
        if name.casefold().endswith(".json") and "." in name[:-5].lstrip("."):
            orig = r["path"][:-5]
            if orig in by_path:
                sidecars[orig].append(r)
                continue
        if wanted(r["path"]):
            copies[(name.casefold(), r["size"])].append(r)
    nodes = []
    for group in copies.values():
        # The newest copy; on a tie the shallower path (backups sit deeper).
        group.sort(key=lambda r: (-(rel._mtime_s(r["mtime"]) or 0), r["path"].count("/"), r["path"]))
        files = list(group)
        for r in group:
            files += sidecars.get(r["path"], [])
        nodes.append(Node(rep=group[0], files=files, base=rel.version_base(_name(group[0]["path"])),
                          copies=len(group) - 1))
    nodes.sort(key=lambda n: n.rep["path"])
    return nodes


# ---------------------------------------------------------------- the link graph
class Graph:
    """Nodes plus links as sorted numpy arrays (pair key a*n+b with a < b)."""

    def __init__(self, nodes: list[Node]):
        self.nodes = nodes
        self.n = len(nodes)
        self.terms: list[str] = []                  # term id → folded name
        self.shown: list[str] = []                  # term id → as written
        self.term_files: list[int] = []             # term id → files here that mention it
        self.node_terms: list[array] = [array("i") for _ in nodes]
        self.parts: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
        self.keys = np.zeros(0, dtype=np.int64)
        self.score = np.zeros(0, dtype=np.float32)
        self.bits = np.zeros(0, dtype=np.uint8)

    def add(self, a: np.ndarray, b: np.ndarray, s, bits) -> None:
        a, b = np.asarray(a, dtype=np.int64), np.asarray(b, dtype=np.int64)
        keep = a != b
        a, b = a[keep], b[keep]
        if not len(a):
            return
        lo, hi = np.minimum(a, b), np.maximum(a, b)
        s = np.broadcast_to(np.asarray(s, dtype=np.float32), keep.shape)[keep]
        bits = np.broadcast_to(np.asarray(bits, dtype=np.uint8), keep.shape)[keep]
        self.parts.append((lo * self.n + hi, s, bits))

    def combine(self) -> None:
        """Merge every signal per pair (1 - Π(1 - s)), add time and place, keep
        links of at least LINK_MIN and, per file, its EDGES_PER_NODE strongest."""
        if not self.parts:
            return
        keys = np.concatenate([p[0] for p in self.parts])
        miss = np.log1p(-np.minimum(np.concatenate([p[1] for p in self.parts]), 0.999).astype(np.float64))
        bits = np.concatenate([p[2] for p in self.parts])
        self.parts = []
        order = np.argsort(keys, kind="stable")
        keys, miss, bits = keys[order], miss[order], bits[order]
        starts = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])
        keys = keys[starts]
        score = 1.0 - np.exp(np.add.reduceat(miss, starts))
        bits = np.bitwise_or.reduceat(bits, starts)
        a, b = keys // self.n, keys % self.n
        near = self._near(a, b)
        score = np.where(near, 1.0 - (1.0 - score) * (1.0 - S_NEAR), score)
        bits = np.where(near, bits | B_NEAR, bits).astype(np.uint8)
        keep = score >= LINK_MIN
        keys, score, bits, a, b = keys[keep], score[keep], bits[keep], a[keep], b[keep]
        # Each file's strongest links: rank links by score within each end.
        rank_ok = np.zeros(len(keys), dtype=bool)
        for end in (a, b):
            order = np.lexsort((-score, end))
            first = np.r_[0, np.flatnonzero(end[order][1:] != end[order][:-1]) + 1]
            run = np.arange(len(order)) - np.repeat(first, np.diff(np.r_[first, len(order)]))
            rank_ok[order[run < EDGES_PER_NODE]] = True
        self.keys, self.score, self.bits = keys[rank_ok], score[rank_ok].astype(np.float32), bits[rank_ok]

    def _near(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """Same folder, or saved the same week, where that says something: not in
        a folder of hundreds of files, nor in the week an archive was copied in."""
        folders = Counter(nd.rep["path"].rsplit("/", 1)[0] for nd in self.nodes)
        folder = np.array([hash(nd.rep["path"].rsplit("/", 1)[0])
                           if folders[nd.rep["path"].rsplit("/", 1)[0]] <= NEAR_FOLDER_MAX else -1 - i
                           for i, nd in enumerate(self.nodes)], dtype=np.int64)
        mt = np.array([rel._mtime_s(nd.rep["mtime"]) or 0.0 for nd in self.nodes], dtype=np.float64)
        week = np.floor(mt / rel.WEEK).astype(np.int64)
        busy = Counter(week.tolist())
        quiet = np.array([mt[i] > 0 and busy[week[i]] <= NEAR_WEEK_MAX for i in range(self.n)], dtype=bool)
        return (folder[a] == folder[b]) | (quiet[a] & quiet[b] & (np.abs(mt[a] - mt[b]) <= rel.WEEK))

    def link(self, u: int, v: int) -> tuple[float, int]:
        a, b = min(u, v), max(u, v)
        i = int(np.searchsorted(self.keys, a * self.n + b))
        if i < len(self.keys) and self.keys[i] == a * self.n + b:
            return float(self.score[i]), int(self.bits[i])
        return 0.0, 0

    def adjacency(self) -> dict[int, dict[int, float]]:
        adj: dict[int, dict[int, float]] = defaultdict(dict)
        for key, s in zip(self.keys.tolist(), self.score.tolist()):
            a, b = divmod(key, self.n)
            adj[a][b] = s
            adj[b][a] = s
        return adj


def _check(stop: Optional[threading.Event]) -> None:
    if stop is not None and stop.is_set():
        raise Stopped()


def _texts_pass(svc: "IndexerService", ws: str, fs: str, g: Graph, stop, progress) -> list[list[list[str]]]:
    """Names and file references of every document, from its first TEXT_CHUNKS
    chunks. Returns per node its mentions of file names, each as the folded
    names to try, longest first ("2024 Budget.xlsx", "Budget.xlsx")."""
    node_of = {n.rep["file_id"]: i for i, n in enumerate(g.nodes)}
    term_id: dict[str, int] = {}
    refs: list[list] = [[] for _ in g.nodes]
    done = 0
    for fid, texts in svc.store.iter_discover_texts(ws, fs, TEXT_CHUNKS):
        i = node_of.get(fid)
        if i is None:
            continue
        done += 1
        if done % 500 == 0:
            _check(stop)
            progress("reading", done, g.n)
        tf, _, shown = rel.scan_names(texts)
        ids = array("i")
        for f in tf:
            t = term_id.get(f)
            if t is None:
                t = term_id[f] = len(g.terms)
                g.terms.append(f)
                g.shown.append(shown[f].most_common(1)[0][0])
                g.term_files.append(0)
            g.term_files[t] += 1
            ids.append(t)
        g.node_terms[i] = ids
        if _name(g.nodes[i].rep["path"]).casefold().endswith(".log"):
            continue
        for t in texts:
            found = rel._FILE_REF.findall(t)
            if len(found) >= rel.LISTING_REFS:
                continue                            # a folder listing names everything
            for stem, ext in found:
                words = stem.split()
                tries = [fold_name(" ".join(words[-n:]) + "." + ext)
                         for n in range(min(rel.REF_WORDS, len(words)), 0, -1)]
                if tries:
                    refs[i].append(tries)
            if len(refs[i]) >= REF_PER_FILE:
                break
    progress("reading", g.n, g.n)
    return refs


def _name_links(svc: "IndexerService", g: Graph) -> None:
    """Rare names two documents share. A name counts when 2..NAME_NODES_MAX
    files here mention it and it is rare across the archive (related.name_cap)."""
    cands = [t for t, n in enumerate(g.term_files) if 2 <= n <= NAME_NODES_MAX]
    df = svc.store.term_docs(g.terms[t] for t in cands)
    cap = link_cap(svc.store)
    good = {t for t in cands if 0 < df.get(g.terms[t], 0) <= cap}
    members: dict[int, list[int]] = defaultdict(list)
    for i, ids in enumerate(g.node_terms):
        kept = array("i", (t for t in ids if t in good))
        g.node_terms[i] = kept
        for t in kept:
            members[t].append(i)
    tri: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for t, nodes in members.items():
        m = len(nodes)
        if m < 2:
            continue
        if m not in tri:
            tri[m] = np.triu_indices(m, 1)
        idx = np.array(nodes, dtype=np.int64)
        g.add(idx[tri[m][0]], idx[tri[m][1]], _strength(m), B_NAMES)


def _version_links(svc: "IndexerService", ws: str, fs: str, g: Graph, stop) -> None:
    """The same base name (versions stripped), or half the text in common."""
    by_base: dict[str, list[int]] = defaultdict(list)
    for i, nd in enumerate(g.nodes):
        if len(nd.base) >= rel.BASE_MIN and nd.base not in rel.GENERIC_BASES:
            by_base[nd.base].append(i)
    for base, idx in by_base.items():
        if len(idx) < 2 or (len(idx) > BASE_GROUP_MAX and len(base) < rel.SPECIFIC_BASE):
            continue
        idx.sort(key=lambda i: -(rel._mtime_s(g.nodes[i].rep["mtime"]) or 0))
        idx = idx[:BASE_GROUP_MAX]
        dates = [rel.name_dates(_name(g.nodes[i].rep["path"])) for i in idx]
        pairs = [(idx[x], idx[y]) for x in range(len(idx)) for y in range(x + 1, len(idx))
                 if not rel.dates_differ(dates[x], dates[y])]       # the minutes of two meetings aren't versions
        if pairs:
            g.add(np.array([p[0] for p in pairs], dtype=np.int64), np.array([p[1] for p in pairs], dtype=np.int64),
                  S_VERSION, B_VERSION)
    _check(stop)
    node_of = {}
    for i, nd in enumerate(g.nodes):
        for r in nd.files:
            node_of[r["file_id"]] = i
    pairs = array("q")
    for fids in svc.store.shared_hash_files(ws, fs, VERSION_SPREAD * 2):
        ns = sorted({node_of[f] for f in fids if f in node_of})
        if 2 <= len(ns) <= VERSION_SPREAD:
            for x in range(len(ns)):
                for y in range(x + 1, len(ns)):
                    pairs.append(ns[x] * g.n + ns[y])
    _check(stop)
    if not pairs:
        return
    keys, counts = np.unique(np.frombuffer(pairs, dtype=np.int64), return_counts=True)
    distinct = svc.store.distinct_passages(ws, fs)
    own = np.array([distinct.get(nd.rep["file_id"], 0) for nd in g.nodes], dtype=np.int64)
    a, b = keys // g.n, keys % g.n
    smaller = np.maximum(1, np.minimum(own[a], own[b]))
    ok = (counts / smaller >= rel.VERSION_SHARE) & ((counts >= 2) | (smaller <= 2))
    g.add(a[ok], b[ok], S_VERSION, B_VERSION)


def _reference_links(svc: "IndexerService", ws: str, fs: str, g: Graph, refs: list[list[list[str]]]) -> None:
    """A document that names another ("see Budget 2024.xlsx"): the longest
    run of words before the extension that is a file name here."""
    folds = {f for mentions in refs for tries in mentions for f in tries}
    if not folds:
        return
    node_of = {}
    for i, nd in enumerate(g.nodes):
        for r in nd.files:
            node_of[r["file_id"]] = i
    by_fold: dict[str, set[int]] = defaultdict(set)
    folds = sorted(folds)
    for x in range(0, len(folds), 2000):
        for r in svc.store.files_named(folds[x:x + 2000]):
            if (r["workspace_id"], r["filespace"]) == (ws, fs) and r["file_id"] in node_of:
                by_fold[r["name_fold"]].add(node_of[r["file_id"]])
    src, dst = [], []
    for i, mentions in enumerate(refs):
        for tries in mentions:
            hit = next((by_fold[f] for f in tries if f in by_fold), None)   # the longest real name
            if hit and len(hit) <= rel.REF_PER_NAME:
                src += [i] * len(hit)
                dst += sorted(hit)
    if not src:
        return
    a, b = np.array(src, dtype=np.int64), np.array(dst, dtype=np.int64)
    g.add(a, b, S_REFERENCE, np.where(a < b, B_REF_AB, B_REF_BA))


def nearest(vecs: np.ndarray, k: int, stop=None, pause: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """(indices, similarities) of each row's k closest other rows, best first,
    in BLOCK × CHUNK steps so memory stays at a few tens of MB."""
    n = len(vecs)
    k = min(k, max(0, n - 1))
    idx = np.zeros((n, k), dtype=np.int64)
    sim = np.zeros((n, k), dtype=np.float32)
    if not k:
        return idx, sim
    for i0 in range(0, n, BLOCK):
        _check(stop)
        t0 = time.monotonic()
        q = vecs[i0:i0 + BLOCK].astype(np.float32)
        best_s = np.full((len(q), k), -np.inf, dtype=np.float32)
        best_i = np.zeros((len(q), k), dtype=np.int64)
        for j0 in range(0, n, CHUNK):
            s = q @ vecs[j0:j0 + CHUNK].astype(np.float32).T
            rows = np.arange(len(q))
            own = rows + i0 - j0
            inside = (own >= 0) & (own < s.shape[1])
            s[rows[inside], own[inside]] = -np.inf
            cand_s = np.concatenate([best_s, s], axis=1)
            cand_i = np.concatenate([best_i, np.broadcast_to(np.arange(j0, j0 + s.shape[1]), s.shape)], axis=1)
            top = np.argpartition(-cand_s, k - 1, axis=1)[:, :k]
            best_s = np.take_along_axis(cand_s, top, axis=1)
            best_i = np.take_along_axis(cand_i, top, axis=1)
        order = np.argsort(-best_s, axis=1, kind="stable")
        idx[i0:i0 + len(q)] = np.take_along_axis(best_i, order, axis=1)
        sim[i0:i0 + len(q)] = np.take_along_axis(best_s, order, axis=1)
        if pause:
            time.sleep(min(1.0, (time.monotonic() - t0) * pause))
    return idx, sim


def _meaning_links(svc: "IndexerService", g: Graph, stop, progress, pause: float) -> None:
    """Mutual nearest neighbours by meaning (file vectors)."""
    keys = [(n.rep["workspace_id"], n.rep["filespace"], n.rep["file_id"]) for n in g.nodes]
    pos, vecs = svc.file_index.vectors(keys)
    if len(pos) < 2:
        return
    progress("comparing", 0, len(pos))
    near_i, near_s = nearest(vecs, MEANING_K, stop, pause)
    del vecs
    m, k = near_i.shape
    rank = np.broadcast_to(np.arange(k), (m, k))
    rows = np.broadcast_to(np.arange(m)[:, None], (m, k))
    ok = near_s >= MEANING_MIN
    a, b, r = rows[ok], near_i[ok], rank[ok]
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    key = lo * m + hi
    order = np.argsort(key, kind="stable")
    key, r = key[order], r[order]
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    counts = np.diff(np.r_[starts, len(key)])
    worst = np.maximum.reduceat(r, starts)
    mutual = counts == 2
    key, worst = key[starts][mutual], worst[mutual]
    p = np.array(pos, dtype=np.int64)
    s = np.where(worst < MEANING_CLOSE, S_MEANING_CLOSE, S_MEANING)
    g.add(p[key // m], p[key % m], s, B_MEANING)
    progress("comparing", len(pos), len(pos))


def build_graph(svc: "IndexerService", ws: str, fs: str, stop=None,
                progress: Optional[Callable] = None, pause: float = 0.0) -> Graph:
    progress = progress or (lambda *a: None)
    g = Graph(fold_nodes(svc.store.discover_files(ws, fs)))
    if g.n < GROUP_MIN:
        return g
    refs = _texts_pass(svc, ws, fs, g, stop, progress)
    progress("linking", 0, 4)
    if svc.store.fts_ready:
        _name_links(svc, g)
    progress("linking", 1, 4)
    _version_links(svc, ws, fs, g, stop)
    progress("linking", 2, 4)
    _reference_links(svc, ws, fs, g, refs)
    del refs
    progress("linking", 3, 4)
    _meaning_links(svc, g, stop, progress, pause)
    g.combine()
    progress("linking", 4, 4)
    return g


# ---------------------------------------------------------------- communities
def louvain(adj: dict[int, dict[int, float]], resolution: float = 1.0) -> list[list[int]]:
    """Communities of a weighted graph by Louvain modularity: move each node to
    the neighbouring community that gains most, merge communities into nodes,
    repeat until nothing moves. Deterministic (sorted order)."""
    member: dict[int, list[int]] = {u: [u] for u in adj}
    graph = {u: dict(nb) for u, nb in adj.items()}
    for _ in range(20):
        comm, moved = _one_level(graph, resolution)
        if not moved:
            break
        new_graph: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        new_member: dict[int, list[int]] = defaultdict(list)
        for u, c in comm.items():
            new_member[c] += member[u]
            new_graph[c]
            for v, w in graph[u].items():
                new_graph[c][comm[v]] += w
        graph = {c: dict(nb) for c, nb in new_graph.items()}
        member = dict(new_member)
    return [sorted(m) for m in member.values()]


def _one_level(graph: dict[int, dict[int, float]], gamma: float) -> tuple[dict[int, int], bool]:
    k = {u: sum(nb.values()) for u, nb in graph.items()}
    m2 = sum(k.values())
    comm = {u: u for u in graph}
    if m2 <= 0:
        return comm, False
    tot = dict(k)
    moved_any = False
    order = sorted(graph)
    for _ in range(50):
        moved = False
        for u in order:
            cu = comm[u]
            links: dict[int, float] = defaultdict(float)
            for v, w in graph[u].items():
                if v != u:
                    links[comm[v]] += w
            tot[cu] -= k[u]
            best, gain = cu, links.get(cu, 0.0) - gamma * tot[cu] * k[u] / m2
            for c in sorted(links):
                g = links[c] - gamma * tot[c] * k[u] / m2
                if g > gain + 1e-12:
                    best, gain = c, g
            tot[best] += k[u]
            if best != cu:
                comm[u] = best
                moved = moved_any = True
        if not moved:
            break
    return comm, moved_any


def communities(g: Graph) -> list[list[int]]:
    """Louvain communities, each over GROUP_MAX split again at finer resolutions."""
    adj = g.adjacency()
    out: list[list[int]] = []

    def split(nodes: list[int], level: int) -> None:
        if len(nodes) <= GROUP_MAX:
            out.append(nodes)
            return
        if level >= len(RESOLUTIONS):
            # Still too big: its most connected GROUP_MAX files.
            inside = set(nodes)
            deg = {u: sum(w for v, w in adj[u].items() if v in inside) for u in nodes}
            out.append(sorted(sorted(nodes, key=lambda u: (-deg[u], u))[:GROUP_MAX]))
            return
        inside = set(nodes)
        sub = {u: {v: w for v, w in adj[u].items() if v in inside} for u in nodes}
        parts = louvain(sub, RESOLUTIONS[level])
        if len(parts) == 1:
            split(nodes, len(RESOLUTIONS))
            return
        for p in parts:
            split(p, level + 1)

    for c in louvain(adj, RESOLUTIONS[0]):
        split(c, 1)
    return out


# ---------------------------------------------------------------- groups
def _iso(r: dict) -> Optional[str]:
    return rel._mtime_iso(r["mtime"])


def _top_folder(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    return parts[0] if len(parts) > 1 else "/"


def describe(n_files: int, folders: list[dict], first: Optional[str], last: Optional[str]) -> str:
    """"23 files across Projects, Finance and Mail, 2023–2025"."""
    text = f"{n_files} file{'s' if n_files != 1 else ''}"
    names = [f["folder"] for f in folders if f["folder"] != "/"]
    if len(names) == 1:
        text += f" in {names[0]}"
    elif len(names) <= 3:
        text += " across " + rel._list_names(names) if names else ""
    else:
        text += f" across {', '.join(names[:3])} and {len(names) - 3} more"
    years = sorted({d[:4] for d in (first, last) if d})
    if years:
        text += ", " + "–".join(years)
    return text


def build_group(g: Graph, nodes: list[int]) -> Optional[dict]:
    """A community → the stored group (None when it is not a collection)."""
    docs = {g.nodes[i].base or _name(g.nodes[i].rep["path"]).casefold() for i in nodes}
    if len(docs) < GROUP_MIN:
        return None
    inside = set(nodes)
    strength: dict[int, float] = defaultdict(float)
    best: dict[int, tuple[float, int, int]] = {}
    internal = 0.0
    for i in nodes:
        for j in nodes:
            if j <= i:
                continue
            s, bits = g.link(i, j)
            if not s:
                continue
            internal += s
            strength[i] += s
            strength[j] += s
            if s > best.get(i, (0.0,))[0]:
                best[i] = (s, j, bits)
            if s > best.get(j, (0.0,))[0]:
                best[j] = (s, i, bits)
    count = Counter(t for i in nodes for t in set(g.node_terms[i]))
    need = max(2, math.ceil(GROUP_NAME_SHARE * len(nodes)))
    names = sorted((t for t, c in count.items() if c >= need), key=lambda t: (-count[t], g.term_files[t], t))
    names = names[:GROUP_NAMES]
    group_terms = set(names)
    files = []
    for i in sorted(nodes, key=lambda i: (-strength[i], g.nodes[i].rep["path"])):
        nd = g.nodes[i]
        mine = set(g.node_terms[i])
        shared = [g.shown[t] for t in names if t in mine][:3]
        files.append({
            "fileId": nd.rep["file_id"], "path": nd.rep["path"], "name": _name(nd.rep["path"]),
            "size": nd.rep["size"], "mtime": _iso(nd.rep), "reason": _reason(g, i, shared, best.get(i)),
            "copies": nd.copies, "names": [g.shown[t] for t in names if t in mine],
            "strength": round(strength[i], 3),
        })
    dates = sorted(f["mtime"] for f in files if f["mtime"])
    folders = Counter(_top_folder(f["path"]) for f in files)
    folder_list = [{"folder": f, "files": n} for f, n in sorted(folders.items(), key=lambda t: (-t[1], t[0]))]
    keys = sorted({r["file_id"] for i in nodes for r in g.nodes[i].files})
    score = internal / len(nodes) * math.log2(1 + len(docs))
    return {
        "keys": keys, "score": round(score, 4), "size": len(files),
        "data": {"files": files, "names": [{"name": g.shown[t], "files": count[t]} for t in names],
                 "folders": folder_list, "first": dates[0] if dates else None,
                 "last": dates[-1] if dates else None, "documents": len(docs)},
    }


def _reason(g: Graph, i: int, shared: list[str], best: Optional[tuple]) -> str:
    if shared:
        return f"Mentions {rel._list_names(shared)}"
    if best is None:
        return "Linked to the others"
    s, j, bits = best
    other = _name(g.nodes[j].rep["path"])
    if bits & B_VERSION:
        text = f"A version of {other}"
    elif bits & B_REF_AB and i < j or bits & B_REF_BA and i > j:
        text = f"Names {other}"
    elif bits & (B_REF_AB | B_REF_BA):
        text = f"Named in {other}"
    elif bits & B_NAMES:
        common = sorted(set(g.node_terms[i]) & set(g.node_terms[j]), key=lambda t: g.term_files[t])[:3]
        text = f"Mentions {rel._list_names([g.shown[t] for t in common])} like {other}" if common else \
            f"Shares names with {other}"
    else:
        text = f"About the same thing as {other}"
    near = rel._near(g.nodes[i].rep, g.nodes[j].rep)
    return rel._with_near(text, near)


def auto_name(data: dict) -> str:
    """A name before the AI has one: the shared names, else the folder."""
    names = [n["name"] for n in data.get("names", [])[:2]]
    if names:
        return " · ".join(names)[:NAME_MAX]
    paths = [f["path"] for f in data.get("files", [])]
    if paths:
        common = os.path.commonpath(paths) if len(paths) > 1 else paths[0].rsplit("/", 1)[0]
        last = [p for p in common.split("/") if p]
        if last:
            return last[-1][:NAME_MAX]
        return _name(paths[0]).rsplit(".", 1)[0][:NAME_MAX] or "Related files"
    return "Related files"


# ---------------------------------------------------------------- runs
def _overlaps(prior: list[dict], keys: list[str]) -> list[tuple[float, dict]]:
    mine = set(keys)
    out = []
    for p in prior:
        theirs = p["_keys"]
        common = len(mine & theirs)
        if common:
            out.append((common / len(mine | theirs), p))
    out.sort(key=lambda t: -t[0])
    return out


def reconcile(store, ws: str, fs: str, groups: list[dict]) -> tuple[list[dict], list[str], dict]:
    """New groups against the stored ones: a group with MATCH_SAME of its files
    in a dismissed, kept or merged group is not proposed again; one matching a
    proposal takes over its id (and name, unless it changed a lot); proposals
    that match nothing are dropped. Returns (rows to write, ids to drop, counts)."""
    prior = []
    for r in store.discover_group_rows(ws, fs):
        r["_keys"] = set(json.loads(r["keys"]))
        prior.append(r)
    by_key: dict[str, list[dict]] = defaultdict(list)
    for p in prior:
        for k in p["_keys"]:
            by_key[k].append(p)
    used: set[str] = set()
    rows, counts = [], Counter()
    now = now_iso()
    for grp in sorted(groups, key=lambda x: -x["score"]):
        near = {id(p): p for k in grp["keys"] for p in by_key.get(k, ())}
        match = next(((j, p) for j, p in _overlaps(list(near.values()), grp["keys"])
                      if j >= MATCH_SAME and p["id"] not in used), None)
        row = {"workspace_id": ws, "filespace": fs, "status": "proposed", "summary": None,
               "score": grp["score"], "size": grp["size"], "keys": json.dumps(grp["keys"]),
               "data": json.dumps(grp["data"]), "merged_into": None, "updated_at": now}
        if match is not None:
            j, p = match
            used.add(p["id"])
            if p["status"] != "proposed":
                counts["still_" + p["status"]] += 1
                continue
            keep = p["named_by"] == "you" or (p["named_by"] == "ai" and j >= MATCH_KEEP_NAME)
            row.update(id=p["id"], created_at=p["created_at"],
                       name=p["name"] if keep else auto_name(grp["data"]),
                       named_by=p["named_by"] if keep else "auto",
                       summary=p["summary"] if keep else None)
            counts["updated"] += 1
        else:
            row.update(id=uuid.uuid4().hex[:12], created_at=now, name=auto_name(grp["data"]), named_by="auto")
            counts["new"] += 1
        rows.append(row)
    drop = [p["id"] for p in prior if p["status"] == "proposed" and p["id"] not in used]
    counts["dropped"] = len(drop)
    return rows, drop, dict(counts)


def run(svc: "IndexerService", ws: str, fs: str, stop: Optional[threading.Event] = None,
        progress: Optional[Callable] = None, pause: float = 0.0) -> dict:
    """One discovery run over a source: link, group, reconcile, store. Stats back."""
    t0 = time.monotonic()
    progress = progress or (lambda *a: None)
    with svc.store.read_view():
        g = build_graph(svc, ws, fs, stop, progress, pause)
        _check(stop)
        progress("grouping", 0, 1)
        groups = [grp for c in (communities(g) if len(g.keys) else []) if (grp := build_group(g, c))]
    rows, drop, counts = reconcile(svc.store, ws, fs, groups)
    svc.store.discover_save(ws, fs, rows, drop)
    progress("grouping", 1, 1)
    stats = {"files": sum(len(n.files) for n in g.nodes), "documents": g.n, "links": int(len(g.keys)),
             "groups": len(groups), "seconds": round(time.monotonic() - t0, 1), **counts}
    logger.info("discover [%s/%s]: %s", ws, fs, stats)
    return stats


# ---------------------------------------------------------------- naming
def _prompt(svc: "IndexerService", row: dict, data: dict, used: Iterable[str] = ()) -> str:
    names = ", ".join(n["name"] for n in data.get("names", [])) or "(none)"
    folders = ", ".join(f"{f['folder']} ({f['files']})" for f in data.get("folders", [])[:5])
    dates = " to ".join(d[:10] for d in (data.get("first"), data.get("last")) if d) or "unknown"
    files = data.get("files", [])
    lines = [f"Names that recur: {names}", f"Top folders: {folders}", f"Dates: {dates}",
             f"Files ({len(files)}):"]
    lines += [f"- {f['path']}" for f in files[:LLM_FILES]]
    lines.append("Excerpts:")
    for f in files[:3]:
        texts = svc.store.file_texts(row["workspace_id"], row["filespace"], f["fileId"], limit=1)
        if texts:
            lines.append(f"[{f['name']}] {' '.join(texts[0].split())[:EXCERPT]}")
    used = list(used)[:USED_NAMES]
    if used:
        lines.append("Already used for other groups: " + "; ".join(used))
    return "\n".join(lines)


_JSON = re.compile(r"\{.*\}", re.S)


def parse_name(answer: str) -> tuple[str, Optional[str]]:
    """The AI's {"name", "summary"}; ValueError when there is no usable name."""
    m = _JSON.search(answer or "")
    obj = json.loads(m.group()) if m else {}
    name = " ".join(str(obj.get("name") or "").split()).strip("\"'“”‘’ .")
    if not name:
        raise ValueError("no name in the answer")
    summary = " ".join(str(obj.get("summary") or "").split()) or None
    return name[:NAME_MAX], (summary[:SUMMARY_MAX] if summary else None)


def name_group(svc: "IndexerService", row: dict) -> bool:
    """Ask the local AI for one group's name; True when it got one."""
    data = json.loads(row["data"])
    used = [r["name"] for r in svc.store.discover_group_rows(row["workspace_id"], row["filespace"])
            if r["named_by"] != "auto" and r["id"] != row["id"]]
    answer = svc.llm.chat(NAME_SYSTEM, _prompt(svc, row, data, used), temperature=0.2)
    try:
        name, summary = parse_name(answer)
    except (ValueError, json.JSONDecodeError):
        logger.warning("discover: no usable name for group %s: %r", row["id"], (answer or "")[:200])
        return False
    current = svc.store.discover_group_row(row["id"])
    if current is None or current["named_by"] != "auto":
        return False                            # renamed or dropped meanwhile
    return svc.store.discover_update(row["id"], name=name, summary=summary, named_by="ai")


# ---------------------------------------------------------------- views
def group_out(svc: "IndexerService", row: dict, files: bool = False) -> dict:
    data = json.loads(row["data"])
    out = {"id": row["id"], "workspaceId": row["workspace_id"], "filespace": row["filespace"],
           "location": svc.locations().get((row["workspace_id"], row["filespace"])),
           "status": row["status"], "name": row["name"], "namedBy": row["named_by"], "summary": row["summary"],
           "description": describe(row["size"], data.get("folders", []), data.get("first"), data.get("last")),
           "files": row["size"], "documents": data.get("documents"), "score": row["score"],
           "names": data.get("names", []), "folders": data.get("folders", []),
           "first": data.get("first"), "last": data.get("last"),
           "createdAt": row["created_at"], "updatedAt": row["updated_at"]}
    if row.get("merged_into"):
        out["mergedInto"] = row["merged_into"]
    if files:
        out.update(_detail(svc, row, data))
    return out


def _detail(svc: "IndexerService", row: dict, data: dict) -> dict:
    """Files (still live, with why each belongs), the timeline and the collection to save."""
    ws, fs = row["workspace_id"], row["filespace"]
    live = svc.store.live_rows((ws, fs, f["fileId"]) for f in data.get("files", []))
    kinds, locs = svc.kinds(), svc.locations()
    files = []
    for f in data.get("files", []):
        r = live.get((ws, fs, f["fileId"]))
        if r is None:
            continue
        name = _name(r["path"])
        files.append({"workspaceId": ws, "filespace": fs, "kind": kinds.get((ws, fs), rel.KIND_LUCIDLINK),
                      "location": locs.get((ws, fs)), "path": r["path"], "name": name, "size": r["size"],
                      "mtime": rel._mtime_iso(r["mtime"]), "type": rel.type_of(name), "reason": f["reason"],
                      "copies": f.get("copies", 0), "names": f.get("names", [])})
    months: Counter = Counter(f["mtime"][:7] for f in files if f["mtime"])
    timeline = [{"month": m, "files": n} for m, n in sorted(months.items())]
    by_time = sorted(files, key=lambda f: (f["mtime"] or "", f["path"]))
    return {"items": files, "timeline": timeline, "byTime": [f["path"] for f in by_time],
            "missing": len(data.get("files", [])) - len(files),
            "collection": {"name": row["name"][:NAME_MAX],
                           "pinned": [{"workspaceId": ws, "filespace": fs, "path": f["path"]}
                                      for f in files][:500]}}


def merge(svc: "IndexerService", ids: list[str], name: Optional[str]) -> dict:
    """One proposal from several of the same location; the originals are marked merged."""
    rows = [svc.store.discover_group_row(i) for i in dict.fromkeys(ids)]
    if any(r is None for r in rows) or len(rows) < 2:
        raise LookupError("merge needs two or more known groups")
    if len({(r["workspace_id"], r["filespace"]) for r in rows}) > 1:
        raise ValueError("groups from different locations cannot be merged")
    if any(r["status"] not in ("proposed", "kept") for r in rows):
        raise ValueError("only proposed or kept groups can be merged")
    files, keys, names = {}, set(), Counter()
    first, last, docs = [], [], 0
    for r in sorted(rows, key=lambda r: -r["score"]):
        d = json.loads(r["data"])
        for f in d.get("files", []):
            files.setdefault(f["fileId"], f)
        keys |= set(json.loads(r["keys"]))
        for n in d.get("names", []):
            names[n["name"]] += n["files"]
        first += [d["first"]] if d.get("first") else []
        last += [d["last"]] if d.get("last") else []
        docs += d.get("documents") or 0
    flist = list(files.values())
    folders = Counter(_top_folder(f["path"]) for f in flist)
    data = {"files": flist, "names": [{"name": n, "files": c} for n, c in names.most_common(GROUP_NAMES)],
            "folders": [{"folder": f, "files": n} for f, n in sorted(folders.items(), key=lambda t: (-t[1], t[0]))],
            "first": min(first) if first else None, "last": max(last) if last else None, "documents": docs}
    now = now_iso()
    gid = uuid.uuid4().hex[:12]
    new = {"id": gid, "workspace_id": rows[0]["workspace_id"], "filespace": rows[0]["filespace"],
           "status": "proposed", "name": (name or "").strip()[:NAME_MAX] or auto_name(data),
           "named_by": "you" if (name or "").strip() else "auto", "summary": None,
           "score": max(r["score"] for r in rows), "size": len(flist), "keys": json.dumps(sorted(keys)),
           "data": json.dumps(data), "merged_into": None, "created_at": now, "updated_at": now}
    svc.store.discover_save(new["workspace_id"], new["filespace"], [new])
    for r in rows:
        svc.store.discover_update(r["id"], status="merged", merged_into=gid)
    return group_out(svc, new, files=True)


# ---------------------------------------------------------------- suggestions
def suggest(svc: "IndexerService", members: list[dict], excluded: set[tuple], scopes: "set[Scope]",
            k: int = SUGGEST_K, since: Optional[float] = None) -> dict:
    """Files outside a collection that link strongly to it: rare names several
    of its files mention, versions of its files, and meaning close to its
    centre. members: ledger rows. Returns {"files", "names"}."""
    store = svc.store
    key = rel._key
    inside = {key(r) for r in members} | excluded
    copy_inside = {rel._copy_key(r) for r in members}
    newest = sorted(members, key=lambda r: -(rel._mtime_s(r["mtime"]) or 0))
    score: dict[tuple, float] = defaultdict(float)     # log of (1 - s), summed
    why: dict[tuple, list] = defaultdict(list)

    def add(k_, s, reason):
        if k_ in inside:
            return
        score[k_] += math.log1p(-min(s, 0.999))
        why[k_].append((s, reason))

    # Names several members mention.
    per_member: Counter = Counter()
    shown: dict[str, Counter] = defaultdict(Counter)
    sample = [r for r in newest if r["status"] == "indexed"][:SUGGEST_MEMBERS]
    for r in sample:
        tf, _, sh = rel.scan_names(store.file_texts(*key(r), limit=TEXT_CHUNKS))
        per_member.update(tf.keys())
        for f in tf:
            shown[f].update(sh[f])
    need = 1 if len(sample) == 1 else max(2, math.ceil(GROUP_NAME_SHARE * len(sample)))
    cands = [f for f, c in per_member.items() if c >= need]
    df = store.term_docs(cands) if cands and store.fts_ready else {}
    cap = rel.name_cap(store)
    terms = sorted((f for f in cands if 0 < df.get(f, 0) <= cap), key=lambda f: (-per_member[f], df[f], f))
    terms = terms[:GROUP_NAMES]
    names_out = [shown[f].most_common(1)[0][0] for f in terms]
    for f, written in zip(terms, names_out):
        hits = store.match_files(scopes, _quote(f), SUGGEST_NAME_HITS)
        s = _strength(len(hits)) or 0.3
        for k_ in hits:
            add(k_, s, ("names", written))
    # Versions of the newest members.
    for r in newest[:SUGGEST_VERSION_MEMBERS]:
        for v, by_name, share in rel._versions(svc, r, scopes):
            add(key(v), S_VERSION, ("version", _name(r["path"])))
    # Meaning: close to the collection's centre, as close as its own files are.
    pos, vecs = svc.file_index.vectors([key(r) for r in members])
    if len(pos):
        v = vecs.astype(np.float32)
        centre = v.mean(axis=0)
        norm = float(np.linalg.norm(centre))
        if norm > 0:
            centre /= norm
            own = v @ centre
            bar = max(MEANING_MIN, float(np.percentile(own, 25)) if len(own) > 1 else MEANING_MIN)
            close = max(bar, float(np.median(own)) if len(own) > 1 else bar)
            for k_, sim in svc.file_index.search(centre, 4 * k + len(members), scopes):
                if sim >= bar:
                    add(k_, S_MEANING_CLOSE if sim >= close else S_MEANING, ("meaning", None))
    strong = {k_: 1.0 - math.exp(v) for k_, v in score.items() if 1.0 - math.exp(v) >= LINK_MIN}
    rows = store.live_rows(strong)
    out_items = []
    for k_, s in sorted(strong.items(), key=lambda t: -t[1]):
        r = rows.get(k_)
        if r is None or rel._copy_key(r) in copy_inside:
            continue
        if since is not None and (rel._mtime_s(r["mtime"]) or 0) < since:
            continue
        out_items.append((r, s, _suggest_reason(why[k_])))
    collapsed = rel._collapse(out_items, k)
    out = rel._Out(svc, newest[0] if newest else {"workspace_id": "", "filespace": "", "path": "/", "mtime": 0})
    return {"names": names_out,
            "files": [out.file(r, rel._copies(reason, n), copies=n, strength=round(s, 3))
                      for (r, s, reason), n in collapsed]}


def _suggest_reason(evidence: list[tuple]) -> str:
    names = [x for _, (kind, x) in sorted(evidence, key=lambda t: -t[0]) if kind == "names"]
    if names:
        return f"Mentions {rel._list_names(list(dict.fromkeys(names))[:3])}, like this collection"
    for _, (kind, x) in evidence:
        if kind == "version":
            return f"Another version of {x}"
    return "About the same thing as this collection"
