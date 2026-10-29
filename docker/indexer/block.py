"""Connected block storage (In Place, Phase A): a folder indexed where it lives.

A block source is a directory bind-mounted read-only into the container (a NAS
share, an external drive, a local disk). It is indexed like a filespace — same
scanner, ledger, embeddings, search and ask — through `LocalSource`, which:

* lists with `os.scandir` and never follows a symlink (symlinked entries are
  not listed at all, and a path whose real location differs from its spelled
  location is refused), so the walk can never leave its root;
* skips system folders and files (the scanner's skip list plus the NAS / macOS
  litter in `SYSTEM_NAMES`);
* reports `mtime` as `st_mtime_ns`;
* gives every file a path-based id, `"p:" + sha1(relative posix path)`, because
  inode numbers through SMB and OrbStack file sharing are not stable across
  restarts. A rename is a delete plus a new file; the embedding cache (keyed by
  chunk text) means it is re-read but not re-embedded.

Deletion safety is the scanner's: any listing error raises, the scan is marked
incomplete and nothing is deleted. An unmounted share usually shows up as a
missing or empty folder (or the `block-empty` placeholder holding only
`.gitkeep`); that is refused up front with `NotMounted`, and `list_dir("/")`
refuses it again so a mount that disappears mid-job cannot look like "every
file was deleted".

`RoutingSourceProvider` hands out a `LocalSource` for block keys and defers to
the filespace (SDK) provider for everything else.
"""
from __future__ import annotations

import hashlib
import logging
import os
import stat as stat_mod
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Iterator, Optional

from .scanner import skip_dir, skip_file
from .source import Entry, FilespaceSource, NotDownloaded, SourceError, SourceProvider

logger = logging.getLogger("indexer.block")

KIND_LUCIDLINK = "lucidlink"
KIND_BLOCK = "block"

NOT_MOUNTED = "block source path not mounted"
ONLINE_ONLY = "online only (not downloaded on the engine computer)"


def online_only(st: os.stat_result) -> bool:
    """A sync app's placeholder: it has a size but no blocks on disk yet."""
    return st.st_size > 0 and getattr(st, "st_blocks", 1) == 0

# System folders and files on NAS shares and macOS volumes, on top of the
# scanner's own skip list (which already drops every dot-name).
SYSTEM_NAMES = frozenset({".DS_Store", ".Spotlight-V100", ".Trashes", ".fseventsd", "#recycle", "@eaDir",
                          ".TemporaryItems", ".DocumentRevisions-V100", ".Trash", "#snapshot", "@Recycle",
                          "Thumbs.db", "desktop.ini", "Icon\r"})
# The placeholder folder mounted when BLOCK_SOURCE_PATH is unset.
PLACEHOLDER_NAMES = frozenset({".gitkeep"})


def system_name(name: str) -> bool:
    return name in SYSTEM_NAMES or name.startswith("._")


class NotMounted(SourceError):
    """The block source's folder is missing, empty, or the placeholder."""

    def __init__(self, msg: str = NOT_MOUNTED):
        super().__init__(msg)


class PathRefused(PermissionError):
    """A path that would leave the source's root or go through a symlink."""


def path_file_id(rel: str) -> str:
    """Stable id for a block file: "p:" + sha1 of its path relative to the root."""
    return "p:" + hashlib.sha1(rel.lstrip("/").encode("utf-8")).hexdigest()


def mounted(root: str) -> bool:
    """True when `root` is a directory holding something besides the placeholder."""
    try:
        with os.scandir(root) as it:
            return any(d.name not in PLACEHOLDER_NAMES for d in it)
    except OSError:
        return False


def _utf8(name: str) -> bool:
    try:
        name.encode("utf-8")
        return True
    except UnicodeEncodeError:   # undecodable bytes (surrogateescape): not storable as TEXT
        return False


class LocalSource(FilespaceSource):
    """A read-only directory tree as a FilespaceSource. Paths are posix, "/" = root."""

    def __init__(self, root: str, skip_online_only: bool = False):
        self.root = os.path.realpath(root)
        # Cloud sync apps (Google Drive, Dropbox…) keep online-only files as
        # placeholders that download when read; with this set they are refused.
        self.skip_online_only = skip_online_only

    def _resolve(self, path: str) -> tuple[str, str]:
        """(relative posix path, absolute path) for `path`, or PathRefused."""
        if not isinstance(path, str) or "\x00" in path:
            raise PathRefused(f"refused path {path!r}")
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        if any(p in (".", "..") for p in parts):
            raise PathRefused(f"refused path {path!r}: '.' and '..' are not allowed")
        full = os.path.join(self.root, *parts) if parts else self.root
        real = os.path.realpath(full)
        if real != self.root and not real.startswith(self.root.rstrip(os.sep) + os.sep):
            raise PathRefused(f"refused path {path!r}: outside the source root")
        if real != os.path.normpath(full):
            raise PathRefused(f"refused path {path!r}: goes through a symlink")
        return "/".join(parts), full

    def list_dir(self, path: str) -> list[Entry]:
        rel, full = self._resolve(path)
        out: list[Entry] = []
        raw = 0
        with os.scandir(full) as it:   # any OSError propagates: the scan becomes incomplete
            for d in it:
                if d.name not in PLACEHOLDER_NAMES:
                    raw += 1
                name = d.name
                if system_name(name) or not _utf8(name) or d.is_symlink():
                    continue
                try:
                    st = d.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue   # vanished between listing and stat: it is gone
                child = f"{rel}/{name}" if rel else name
                if stat_mod.S_ISDIR(st.st_mode):
                    if not skip_dir(name):
                        out.append(Entry(name, "dir", 0, st.st_mtime_ns, ""))
                elif stat_mod.S_ISREG(st.st_mode):
                    if not skip_file(name):
                        out.append(Entry(name, "file", st.st_size, st.st_mtime_ns, path_file_id(child)))
                # sockets, fifos, devices: never documents
        if not rel and raw == 0:
            # An empty root is an unmounted share far more often than an empty
            # one; refusing it keeps the scan from pruning everything.
            raise NotMounted()
        return out

    def read_file(self, path: str) -> bytes:
        _, full = self._resolve(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(full, flags)   # FileNotFoundError for a vanished file, like the SDK
        try:
            st = os.fstat(fd)
            if not stat_mod.S_ISREG(st.st_mode):
                raise IsADirectoryError(f"not a regular file: {path}")
            if self.skip_online_only and online_only(st):
                raise NotDownloaded(ONLINE_ONLY)
            with os.fdopen(fd, "rb", closefd=False) as f:
                return f.read()
        finally:
            os.close(fd)

    def stat(self, path: str) -> Optional[Entry]:
        rel, full = self._resolve(path)
        try:
            st = os.lstat(full)
        except (FileNotFoundError, NotADirectoryError):
            return None
        name = rel.rsplit("/", 1)[-1] if rel else ""
        if stat_mod.S_ISDIR(st.st_mode):
            return Entry(name, "dir", 0, st.st_mtime_ns, path_file_id(rel) if rel else "p:")
        if stat_mod.S_ISREG(st.st_mode):
            return Entry(name, "file", st.st_size, st.st_mtime_ns, path_file_id(rel))
        return None   # symlinks and special files do not exist as far as indexing goes


# ---------------------------------------------------------------- configuration
@dataclass(frozen=True)
class BlockSpec:
    name: str
    path: str
    error: Optional[str] = None
    # "engine": from BLOCK_SOURCES in the engine's .env; "app": connected from the web app (folders.py).
    origin: str = "engine"
    host_path: Optional[str] = None      # the folder on the Mini, when known
    root: Optional[str] = None           # app sources: root id ("volumes", "icloud", "desktop")
    folder: Optional[str] = None         # app sources: path inside the root, "/Projects"
    provider: Optional[dict] = None      # cloud root: {"id", "label", "account"}
    download_online_only: bool = False   # cloud root: read (and so download) online-only files


def parse_block_sources(value: str) -> list[BlockSpec]:
    """`name=/container/path;…` -> specs. Malformed entries carry an error."""
    out: list[BlockSpec] = []
    seen: set[str] = set()
    for part in (value or "").split(";"):
        part = part.strip()
        if not part:
            continue
        name, sep, path = part.partition("=")
        name, path = name.strip(), path.strip()
        if not sep or not name or not path:
            out.append(BlockSpec(name or part, path, "invalid entry; expected name=/container/path"))
        elif "/" in name:
            out.append(BlockSpec(name, path, "invalid name; '/' is not allowed"))
        elif not path.startswith("/"):
            out.append(BlockSpec(name, path, "invalid path; must be absolute inside the container"))
        elif name in seen:
            out.append(BlockSpec(name, path, "duplicate name; the first entry is used"))
        else:
            seen.add(name)
            out.append(BlockSpec(name, path))
    return out


# ---------------------------------------------------------------- types (AI Lab's classes)
# From the AI Lab prototype (lab_node/scan.py TYPE_EXT, recipes.KINDS), checked in this order.
_FOOTAGE = {".mov", ".mp4", ".m4v", ".mxf", ".avi", ".mkv", ".r3d", ".braw", ".ari"}
_PHOTOS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".dng", ".cr3", ".nef", ".arw", ".psd", ".exr", ".dpx"}
_AUDIO = {".wav", ".mp3", ".m4a", ".aif", ".aiff", ".flac", ".ogg"}
_REF = {".prproj", ".drp", ".fcpxml", ".edl", ".otio"}
TYPE_EXT: dict[str, frozenset[str]] = {
    "project": frozenset(_REF | {".aep", ".aaf", ".fcpbundle"}),
    "design": frozenset({".psd", ".psb", ".ai", ".indd", ".idml", ".sketch", ".fig", ".xd", ".afdesign",
                         ".afphoto", ".eps", ".svg"}),
    "video": frozenset(_FOOTAGE),
    "audio": frozenset(_AUDIO),
    "image": frozenset(_PHOTOS | {".gif", ".webp", ".bmp"}),
    "pdf": frozenset({".pdf"}),
    "document": frozenset({".doc", ".docx", ".txt", ".md", ".rtf", ".pages", ".odt", ".srt", ".vtt", ".html",
                           ".eml"}),
    "spreadsheet": frozenset({".xls", ".xlsx", ".csv", ".numbers", ".ods", ".tsv"}),
    "presentation": frozenset({".ppt", ".pptx", ".key", ".odp"}),
    "code": frozenset({".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml", ".sh", ".go", ".rs",
                       ".java", ".c", ".cpp", ".h", ".rb", ".php", ".css", ".sql", ".ipynb", ".swift", ".kt",
                       ".xml"}),
    "archive": frozenset({".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz", ".dmg", ".iso"}),
}
TYPES = ("document", "spreadsheet", "presentation", "pdf", "image", "design", "audio", "video", "code",
         "archive", "project", "other")


def type_of(name: str) -> str:
    ext = PurePosixPath(name).suffix.lower()
    return next((t for t, exts in TYPE_EXT.items() if ext in exts), "other")


# ---------------------------------------------------------------- provider
class RoutingSourceProvider(SourceProvider):
    """LocalSource for block keys, the filespace provider for everything else.

    `block_root(ws, fs)` returns the container path of a block target or None;
    `filespaces()` returns the current filespace provider (looked up per lease,
    so tests and the service can swap it).
    """

    def __init__(self, block_root: Callable[[str, str], Optional[str]],
                 filespaces: Callable[[], SourceProvider],
                 skip_online_only: Callable[[str, str], bool] = lambda ws, fs: False):
        self._block_root = block_root
        self._filespaces = filespaces
        self._skip_online_only = skip_online_only

    @contextmanager
    def lease(self, workspace_id: str, filespace: str) -> Iterator[FilespaceSource]:
        root = self._block_root(workspace_id, filespace)
        if root is None:
            with self._filespaces().lease(workspace_id, filespace) as source:
                yield source
            return
        if not mounted(root):
            raise NotMounted()
        yield LocalSource(root, skip_online_only=self._skip_online_only(workspace_id, filespace))
