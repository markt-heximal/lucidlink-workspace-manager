"""An in-memory stand-in for SDK 1.0.1's *native* layer.

Only the ctypes boundary is faked: `make_filespace()` wraps these fakes in the
SDK's own `lucidlink.FileSystem`, `lucidlink.connect.ConnectManager` (and so
`FsUtils`, `FileStream`, `FsEntry`, the data-store handles and
`HttpConnect_3_6`), so the code under test meets the real 1.0 Python API: real
`FsEntry` records from `read_dir`, properties instead of methods, typed errors,
`FsUtils`' isinstance check, the Connect argument validation.

The native records (dict keys, kind strings such as "S3DataStore") follow
lucidlink/_native/_file_system.py and _native/_connect.py of 1.0.1.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

from lucidlink import FileSystem
from lucidlink import exceptions as llx
from lucidlink.connect import ConnectManager

S3_KIND = "S3DataStore"
HTTP_KIND = "HttpLinkDataStore"


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] or "/"


def _norm(path: str) -> str:
    if not path.startswith("/"):
        raise llx.InvalidArgumentError(f"path must be absolute: {path!r}")
    return "/" + "/".join(p for p in path.split("/") if p)


class FakeNativeFs:
    """Native FileSystem: a flat {path: node} tree, handles, a ns clock."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self._next = 1
        self.clock = 1_700_000_000 * 10**9
        self.nodes: dict[str, dict] = {"/": self._node("dir")}
        self.handles: dict[int, str] = {}
        self.calls: list[str] = []

    # -------------------------------------------------------------- helpers
    def _op(self, name: str) -> None:
        self.calls.append(name)
        if self.fail:
            raise RuntimeError("disk on fire")

    def _node(self, type_: str, data: bytes = b"", external: Optional[dict] = None) -> dict:
        nid = f"e{self._next:04d}"
        self._next += 1
        self.clock += 10**9
        return {"type": type_, "id": nid, "inode": "i" + nid, "data": bytearray(data),
                "ctime": self.clock, "mtime": self.clock, "external": external}

    def _get(self, path: str) -> dict:
        node = self.nodes.get(_norm(path))
        if node is None:
            raise llx.FsNotFoundError._from_native("No such file or directory", path)
        return node

    def _need_parent_dir(self, path: str) -> None:
        parent = self.nodes.get(_parent(_norm(path)))
        if parent is None:
            raise llx.FsNotFoundError._from_native("No such file or directory", path)
        if parent["type"] != "dir":
            raise llx.FsNotADirectoryError._from_native("Not a directory", path)

    def _children(self, path: str) -> list[str]:
        p = _norm(path)
        return sorted(k for k in self.nodes if k != "/" and _parent(k) == p)

    def _record(self, path: str, node: dict) -> dict:
        t = node["type"]
        return {"name": "" if path == "/" else path.rsplit("/", 1)[-1],
                "size": len(node["data"]) if t == "file" else 0,
                "type": t, "is_file": t == "file", "is_dir": t == "dir", "is_link": t == "link",
                "entry_id": node["id"], "inode_id": node["inode"], "entry_id_external": 0,
                "creation_time": 0 if path == "/" else node["ctime"],
                "update_time": 0 if path == "/" else node["mtime"], "num_hard_links": 1}

    def add_file(self, path: str, data: bytes = b"", **ext) -> dict:
        """Test setup: create a file (and its parents) without going through open()."""
        p = _norm(path)
        parts = p.split("/")[1:-1]
        for i in range(1, len(parts) + 1):
            d = "/" + "/".join(parts[:i])
            self.nodes.setdefault(d, self._node("dir"))
        self.nodes[p] = self._node("file", data, ext or None)
        return self.nodes[p]

    # -------------------------------------------------------------- native API
    def read_dir(self, path):
        self._op("read_dir")
        node = self._get(path)
        if node["type"] != "dir":
            raise llx.FsNotADirectoryError._from_native("Not a directory", path)
        return [self._record(c, self.nodes[c]) for c in self._children(path)]

    def get_entry(self, path):
        self._op("get_entry")
        p = _norm(path)
        rec = self._record(p, self._get(p))
        rec.update(is_root=p == "/", is_share=False, snapshot_id=0, unix_uid=0, unix_gid=0,
                   unix_mode=0, windows_attributes=0)
        return rec

    def create_dir(self, path, *, parents=False):
        self._op("create_dir")
        p = _norm(path)
        if p in self.nodes:
            raise llx.FsExistsError._from_native("File exists", path)
        self._need_parent_dir(p)
        self.nodes[p] = self._node("dir")

    def delete_dir(self, path, recursive=False):
        self._op("delete_dir")
        p = _norm(path)
        node = self._get(p)
        if node["type"] != "dir":
            raise llx.FsNotADirectoryError._from_native("Not a directory", path)
        kids = [k for k in self.nodes if k.startswith(p.rstrip("/") + "/")]
        if kids and not recursive:
            raise llx.FsDirectoryNotEmptyError._from_native("Directory not empty", path)
        for k in kids:
            del self.nodes[k]
        del self.nodes[p]

    def delete(self, path):
        self._op("delete")
        node = self._get(path)
        if node["type"] == "dir":
            raise llx.FsIsADirectoryError._from_native("Is a directory", path)
        del self.nodes[_norm(path)]

    def move(self, src_path, dst_path, *, overwrite=False):
        self._op("move")
        s, d = _norm(src_path), _norm(dst_path)
        self._get(s)
        if d in self.nodes and not overwrite:
            raise llx.FsExistsError._from_native("File exists", src_path, dst_path)
        self._need_parent_dir(d)
        prefix = s + "/"
        for k in sorted(k for k in self.nodes if k == s or k.startswith(prefix)):
            self.nodes[d + k[len(s):]] = self.nodes.pop(k)

    def open(self, path, mode, lock_type="", *, parents=False):
        self._op("open")
        p = _norm(path)
        if p not in self.nodes:
            if mode == "r":
                raise llx.FsNotFoundError._from_native("No such file or directory", path)
            self._need_parent_dir(p)
            self.nodes[p] = self._node("file")
        hid = self._next
        self._next += 1
        self.handles[hid] = p
        return hid

    def _h(self, hid) -> dict:
        return self.nodes[self.handles[hid]]

    def read(self, handle_id, size=-1, offset=0):
        self._op("read")
        data = self._h(handle_id)["data"]
        end = len(data) if size < 0 else offset + size
        return bytes(data[offset:end])

    def read_range(self, handle_id, offset, size, chunk_size):
        return self.read(handle_id, size, offset)

    def write(self, handle_id, data, offset=0):
        self._op("write")
        node = self._h(handle_id)
        buf = node["data"]
        if len(buf) < offset:
            buf.extend(b"\0" * (offset - len(buf)))
        buf[offset:offset + len(data)] = data
        self.clock += 10**9
        node["mtime"] = self.clock
        return len(data)

    def close(self, handle_id):
        self.handles.pop(handle_id, None)

    def get_size_by_handle(self, handle_id, path=None):
        return len(self._h(handle_id)["data"])

    def set_end_of_file_by_handle(self, handle_id, size):
        node = self._h(handle_id)
        del node["data"][size:]

    def get_size(self):
        self._op("get_size")
        files = [n for n in self.nodes.values() if n["type"] == "file"]
        ext = [n for n in files if n["external"]]
        return {"entries": len(self.nodes) - 1,
                "data": sum(len(n["data"]) for n in files),
                "storage": sum(len(n["data"]) for n in files if not n["external"]),
                "external_files_size": sum(len(n["data"]) for n in ext),
                "external_files_count": len(ext)}


class FakeNativeConnect:
    """Native Connect over a FakeNativeFs: data stores and external files."""

    def __init__(self, fs: FakeNativeFs, available: bool = True):
        self.fs = fs
        self.available = available
        self.stores: dict[str, dict] = {}
        self.rekeys: list[tuple] = []

    def are_data_stores_available(self):
        return self.available

    def _store(self, name):
        s = self.stores.get(name)
        if s is None:
            raise llx.DataStoreNotFoundError(f"Data store {name!r} does not exist", name=name)
        return s

    def add_data_store(self, name, access_key, secret_key, bucket_name, region,
                       endpoint="", url_expiration_minutes=10080, use_virtual_addressing=False):
        if name in self.stores:
            raise llx.DataStoreExistsError(f"Data store {name!r} already exists", name=name)
        self.stores[name] = {"name": name, "kind": S3_KIND, "key_id": "kid-" + name,
                             "rekey_state": "no_rekey", "access_key": access_key,
                             "secret_key": secret_key, "bucket_name": bucket_name, "region": region,
                             "endpoint": endpoint, "url_expiration_minutes": url_expiration_minutes,
                             "use_virtual_addressing": use_virtual_addressing}

    def add_http_link_data_store(self, name):
        if name in self.stores:
            raise llx.DataStoreExistsError(f"Data store {name!r} already exists", name=name)
        self.stores[name] = {"name": name, "kind": HTTP_KIND, "key_id": "", "rekey_state": "no_rekey"}

    def _public(self, s):
        return {k: v for k, v in s.items() if k != "secret_key"}

    def list_data_stores(self):
        return [self._public(s) for s in self.stores.values()]

    def get_data_store(self, name):
        s = self.stores.get(name)
        return None if s is None else self._public(s)

    def remove_data_store(self, name):
        self._store(name)
        if self._files(name):
            raise llx.DataStoreInUseError(f"Data store {name!r} has linked files", name=name)
        del self.stores[name]

    def rekey_data_store(self, name, new_access_key, new_secret_key):
        s = self._store(name)
        s["access_key"], s["secret_key"] = new_access_key, new_secret_key
        self.rekeys.append((name, new_access_key))

    def _link(self, path, kind, store, **meta):
        p = _norm(path)
        if p in self.fs.nodes:
            raise llx.FsExistsError._from_native("File exists", path)
        self.fs._need_parent_dir(p)
        node = self.fs._node("file", b"", {"kind": kind, "store": store, **meta})
        self.fs.nodes[p] = node
        return {"kind": kind, "data_store_name": store, "entry_id": node["id"],
                "inode_id": node["inode"], "size": meta.get("size") or 0}

    def link_file(self, file_path, data_store_name, object_id, size=None, checksum=None):
        self._store(data_store_name)
        return self._link(file_path, S3_KIND, data_store_name, key=object_id, size=size)

    def link_http_file(self, file_path, url, data_store_name=None, size=None):
        if data_store_name:
            self._store(data_store_name)
        return self._link(file_path, HTTP_KIND, data_store_name, url=url, size=size)

    def get_external_file_info(self, file_path):
        node = self.fs._get(file_path)
        ext = node["external"]
        if node["type"] != "file" or not ext:
            raise llx.InvalidArgumentError("not an external file")
        return {"kind": ext["kind"], "data_store_name": ext["store"], "entry_id": node["id"],
                "inode_id": node["inode"], "size": len(node["data"])}

    def _files(self, store):
        return sorted((p, n) for p, n in self.fs.nodes.items()
                      if n["external"] and n["external"]["store"] == store)

    def get_external_file_path(self, entry_id):
        for p, n in self.fs.nodes.items():
            if n["id"] == entry_id:
                return p
        raise llx.FsNotFoundError._from_native("No such file or directory", entry_id)

    def count_external_files(self, data_store_name):
        self._store(data_store_name)
        return len(self._files(data_store_name))

    def list_external_files(self, data_store_name, limit=100, cursor=""):
        self._store(data_store_name)
        files = self._files(data_store_name)
        start = int(cursor or 0)
        page = files[start:start + limit]
        more = start + limit < len(files)
        return {"entries": [{"path": p, "entry_id": n["id"], "inode_id": n["inode"]} for p, n in page],
                "file_paths": [p for p, _ in page], "entry_ids_external": [0] * len(page),
                "has_more": more, "cursor": str(start + limit) if more else ""}


def make_filespace(version: int = 12, connect_available: bool = True, fail: bool = False):
    """A Filespace-shaped object built from the SDK's own FileSystem and
    ConnectManager over the fakes. `.native` / `.native_connect` expose the
    fakes for setup and assertions."""
    native = FakeNativeFs(fail=fail)
    fs = FileSystem(native)
    native_connect = FakeNativeConnect(native, available=connect_available)
    connect = ConnectManager(native_connect, fs, version)
    syncs: list[int] = []
    return SimpleNamespace(file_system=fs, connect=connect, version=version,
                           native=native, native_connect=native_connect,
                           syncs=syncs, sync=lambda: syncs.append(1))


def make_fs(fail: bool = False) -> FileSystem:
    """Just a real `lucidlink.FileSystem` over a FakeNativeFs (`fs._native`)."""
    return FileSystem(FakeNativeFs(fail=fail))
