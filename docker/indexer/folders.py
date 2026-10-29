"""Folders from the app: block storage connected without a terminal.

Compose mounts a few roots read-only under BLOCK_ROOT_BASE (/host): the Mini's
/Volumes (every mounted drive and network share), iCloud Drive and Desktop.
The owner browses them from the web app and picks folders; the choices live in
the index database (`block_folders`) and become block sources next to the ones
in BLOCK_SOURCES, with no restart. See docs/INDEXER.md, "Folders from the app".
"""
from __future__ import annotations

import os
import posixpath
from typing import Callable, Optional

from .block import PLACEHOLDER_NAMES, BlockSpec, LocalSource, PathRefused, system_name
from .config import Config
from .scanner import skip_dir

ROOTS: tuple[tuple[str, str], ...] = (
    ("volumes", "Drives and network shares"),
    ("icloud", "iCloud Drive"),
    ("desktop", "Desktop"),
    ("documents", "Documents"),
    ("cloud", "Cloud storage"),
)
# Top folders of ~/Library/CloudStorage, by sync app.
_PROVIDERS = (("GoogleDrive", "google-drive", "Google Drive"), ("Dropbox", "dropbox", "Dropbox"),
              ("OneDrive", "onedrive", "OneDrive"), ("Box", "box", "Box"), ("ShareFile", "sharefile", "ShareFile"))


def provider_of(root: str, folder: str) -> Optional[dict]:
    """The cloud service and account a folder in the `cloud` root belongs to."""
    if root != "cloud" or folder in ("", "/"):
        return None
    top = folder.strip("/").split("/", 1)[0]
    for prefix, pid, label in _PROVIDERS:
        if top == prefix or top.startswith(prefix + "-") or top.startswith(prefix + " "):
            rest = top[len(prefix):].lstrip("- ").strip()
            if rest.startswith("(") and rest.endswith(")"):
                rest = rest[1:-1]
            account = None if not rest or rest == prefix else rest
            return {"id": pid, "label": label, "account": account}
    return {"id": "other", "label": top, "account": None}
ROOT_LABELS = dict(ROOTS)
BROWSE_MAX = 500
NAME_MAX = 64
_BAD_NAME = set("/;=")


class FolderError(Exception):
    """Refused request: `status` is the HTTP status to answer with."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def root_path(cfg: Config, root: str) -> str:
    return posixpath.join(cfg.block_root_base, root)


def root_available(cfg: Config, root: str) -> bool:
    try:
        with os.scandir(root_path(cfg, root)) as it:
            return any(d.name not in PLACEHOLDER_NAMES for d in it)
    except OSError:
        return False


def roots(cfg: Config) -> list[dict]:
    return [{"id": rid, "label": label, "hostPath": cfg.block_root_hosts.get(rid) or None,
             "available": root_available(cfg, rid)} for rid, label in ROOTS]


def normalize_folder(path: str) -> str:
    """"/" or "/a/b" (posix, no trailing slash); '.' and '..' refused."""
    parts = [p for p in (path or "").replace("\\", "/").split("/") if p]
    if any(p in (".", "..") or "\x00" in p for p in parts):
        raise FolderError(400, "That folder path isn't valid.")
    return "/" + "/".join(parts)


def host_path(cfg: Config, root: str, folder: str) -> Optional[str]:
    base = cfg.block_root_hosts.get(root)
    if not base:
        return None
    return base.rstrip("/") + ("" if folder == "/" else folder)


def check_root(cfg: Config, root: str) -> None:
    if root not in ROOT_LABELS:
        raise FolderError(404, f"Unknown place {root!r}.")
    if not root_available(cfg, root):
        raise FolderError(503, f"{ROOT_LABELS[root]} isn't shared with the engine on the Mini yet.")


def resolve(cfg: Config, root: str, folder: str) -> tuple[str, str]:
    """(normalized folder, absolute container path) of an existing folder in a root."""
    check_root(cfg, root)
    folder = normalize_folder(folder)
    try:
        _rel, full = LocalSource(root_path(cfg, root))._resolve(folder)
    except PathRefused:
        raise FolderError(400, "That folder is outside the place you're browsing, or is a link.") from None
    if not os.path.isdir(full):
        raise FolderError(400, f"{folder} isn't a folder (or isn't there any more).")
    return folder, full


def browse(cfg: Config, root: str, folder: str, connected: Callable[[str, str, str, Optional[str]], Optional[str]]
           ) -> dict:
    """Subfolders of `folder`; `connected(root, folder, container path, host path)` names the source, if any."""
    folder, full = resolve(cfg, root, folder)
    names: list[str] = []
    try:
        with os.scandir(full) as it:
            for d in it:
                n = d.name
                if (n in PLACEHOLDER_NAMES or n.startswith(".") or system_name(n) or skip_dir(n)
                        or n.startswith("com.apple.")):
                    continue
                try:
                    if d.is_symlink() or not d.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                names.append(n)
    except OSError as e:
        raise FolderError(503, f"Couldn't open {folder}: {e.strerror or e}") from None
    names.sort(key=str.casefold)
    entries = []
    for n in names[:BROWSE_MAX]:
        p = ("" if folder == "/" else folder) + "/" + n
        host = host_path(cfg, root, p)
        entries.append({"name": n, "path": p, "hostPath": host, "provider": provider_of(root, p),
                        "connected": connected(root, p, os.path.join(full, n), host)})
    parent = None if folder == "/" else (posixpath.dirname(folder) or "/")
    return {"root": root, "path": folder, "parent": parent, "entries": entries,
            "truncated": len(names) > BROWSE_MAX}


def default_name(root: str, folder: str) -> str:
    base = posixpath.basename(folder) or ROOT_LABELS.get(root, root)
    if root == "icloud" and folder == "/":
        base = "iCloud"
    prov = provider_of(root, folder)
    if prov and folder.strip("/").count("/") == 0 and prov["id"] != "other":
        base = prov["label"] + (f" {prov['account'].split('@')[0]}" if prov["account"] else "")
    out = "".join(c if c.isalnum() or c in "._-" else "-" for c in base)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:NAME_MAX] or "Folder"


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise FolderError(400, "Give the folder a name.")
    if len(name) > NAME_MAX:
        raise FolderError(400, f"Names can be at most {NAME_MAX} characters.")
    if _BAD_NAME & set(name):
        raise FolderError(400, "Names can't contain / ; or =.")
    return name


def app_specs(cfg: Config, rows: list[dict]) -> list[BlockSpec]:
    """Block specs for the folders stored by the app."""
    out = []
    for r in rows:
        root, folder = r["root"], r["folder"]
        path = root_path(cfg, root) + ("" if folder == "/" else folder)
        out.append(BlockSpec(r["name"], path, None if root in ROOT_LABELS else f"unknown place {root!r}",
                             origin="app", host_path=host_path(cfg, root, folder), root=root, folder=folder,
                             provider=provider_of(root, folder),
                             download_online_only=bool(r.get("download_online_only"))))
    return out


def overlap(a: str, b: str) -> Optional[str]:
    """How host folder `a` relates to `b`: "same", "inside", "around" or None."""
    a, b = a.rstrip("/") or "/", b.rstrip("/") or "/"
    if a == b:
        return "same"
    if a.startswith(b.rstrip("/") + "/"):
        return "inside"
    if b.startswith(a.rstrip("/") + "/"):
        return "around"
    return None
