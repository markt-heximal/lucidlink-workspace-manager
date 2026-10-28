"""What is tracked, and where its pinned version lives.

Data-driven so another component is one entry, not new code: add a dict to
DEFAULT_COMPONENTS, or point UPDATES_COMPONENTS_FILE at a JSON list of the same
shape (entries there replace defaults with the same `key`).

Each component names:
  key       stable id (used in the DB and in env overrides)
  label     human name for alerts
  source    "pypi" | "dockerhub"
  package   PyPI project name, or Docker Hub "namespace/repo"
  pin       how to find the version this repo pins:
              {"file": "requirements.txt", "kind": "requirements", "name": "lucidlink"}
              {"file": "docker-compose.yml", "kind": "image", "name": "lucidlink/lucidlink-api"}
              {"file": ".mcp.json", "kind": "uvx", "name": "lucidlink-mcp"}
            plus optional "installed": "<dist name>" to prefer the version
            actually installed in this interpreter (truth beats the file).
  relevance a sentence for the LLM about why this component matters here.
  public    reachable from the internet through the Funnel gateway (raises
            the urgency of a security fix).

The pinned version can always be forced with env UPDATES_PIN_<KEY> (key
upper-cased, `-` -> `_`), e.g. UPDATES_PIN_LUCIDLINK_API=1.5.0. That is the
escape hatch for files not present in the image.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Optional

logger = logging.getLogger("lucidlink_api.updates")

DEFAULT_COMPONENTS: list[dict] = [
    {
        "key": "lucidlink",
        "label": "LucidLink Python SDK",
        "source": "pypi",
        "package": "lucidlink",
        "pin": {"file": "requirements.txt", "kind": "requirements", "name": "lucidlink",
                "installed": "lucidlink"},
        "public": True,
        "relevance": "Runs inside the file service and the delta indexer: directory listing, "
                     "metadata (mtime, stable file/entry ids), streaming and Range reads, writes, "
                     "LucidLink Connect, cache sizing, thread-safety and client lifecycle.",
    },
    {
        "key": "lucidlink-api",
        "label": "LucidLink Management API image",
        "source": "dockerhub",
        "package": "lucidlink/lucidlink-api",
        "pin": {"file": "docker-compose.yml", "kind": "image", "name": "lucidlink/lucidlink-api"},
        "public": True,
        "relevance": "Proxied publicly at /api/v1/* (filespaces, members, permissions, guest links); "
                     "security fixes matter because the gateway is on the public internet.",
    },
    {
        "key": "lucidlink-mcp",
        "label": "LucidLink MCP server",
        "source": "pypi",
        "package": "lucidlink-mcp",
        "pin": {"file": ".mcp.json", "kind": "uvx", "name": "lucidlink-mcp"},
        "public": False,
        "relevance": "Used by coding agents against the filespaces via .mcp.json; not part of the "
                     "running engine.",
    },
]


@dataclass(frozen=True)
class Component:
    key: str
    label: str
    source: str
    package: str
    pin: dict = field(default_factory=dict)
    relevance: str = ""
    public: bool = False

    @property
    def env_override(self) -> str:
        return "UPDATES_PIN_" + re.sub(r"[^A-Z0-9]", "_", self.key.upper())


@dataclass(frozen=True)
class Pin:
    version: Optional[str]         # effective: env override > installed > file
    source: str                    # where `version` came from
    digest: Optional[str] = None   # for digest-pinned images
    declared: Optional[str] = None  # what the pin file says, when it was found
    declared_source: Optional[str] = None


def load_components() -> list[Component]:
    entries = {c["key"]: c for c in DEFAULT_COMPONENTS}
    extra = os.environ.get("UPDATES_COMPONENTS_FILE", "")
    if extra:
        try:
            for c in json.loads(Path(extra).read_text()):
                entries[c["key"]] = c
        except (OSError, ValueError, KeyError, TypeError) as e:
            logger.warning("ignoring UPDATES_COMPONENTS_FILE %s: %s", extra, e)
    out = []
    for c in entries.values():
        if c.get("source") not in ("pypi", "dockerhub"):
            logger.warning("component %s: unknown source %r, skipped", c.get("key"), c.get("source"))
            continue
        out.append(Component(key=c["key"], label=c.get("label", c["key"]), source=c["source"],
                             package=c["package"], pin=c.get("pin", {}),
                             relevance=c.get("relevance", ""), public=bool(c.get("public"))))
    return out


def pin_search_dirs() -> list[Path]:
    """Where pin files are looked for, first match wins.

    UPDATES_PIN_DIR (e.g. a read-only bind mount of the checkout), then /app
    (the image copies requirements.txt there), then this package's repo: the
    docker/ directory and the repo root above it (dev, tests, CI).
    """
    dirs: list[Path] = []
    env_dir = os.environ.get("UPDATES_PIN_DIR", "")
    if env_dir:
        dirs.append(Path(env_dir))
    here = Path(__file__).resolve().parent          # docker/updates
    dirs += [Path("/app/pins"), Path("/app"), here.parent, here.parent.parent]
    return dirs


def _find_file(name: str, dirs: list[Path]) -> Optional[Path]:
    for d in dirs:
        p = d / name
        if p.is_file():
            return p
    return None


def parse_requirements_pin(text: str, name: str) -> Optional[str]:
    pat = re.compile(rf"^\s*{re.escape(name)}\s*(?:\[[^\]]*\])?\s*==\s*([^\s#;]+)", re.IGNORECASE | re.MULTILINE)
    m = pat.search(text)
    return m.group(1) if m else None


def parse_image_pin(text: str, image: str) -> tuple[Optional[str], Optional[str]]:
    """(tag, digest) of `image: <image>:<tag>[@sha256:...]`; the first occurrence."""
    pat = re.compile(rf"image:\s*[\"']?{re.escape(image)}:(?P<tag>[^@\s\"']+)(?:@(?P<digest>sha256:[0-9a-f]{{64}}))?")
    m = pat.search(text)
    return (m.group("tag"), m.group("digest")) if m else (None, None)


def parse_uvx_pin(text: str, name: str) -> Optional[str]:
    m = re.search(rf"[\"']{re.escape(name)}(?:\[[^\]]*\])?==([^\"']+)[\"']", text)
    return m.group(1) if m else None


def _read_declared(component: Component, dirs: list[Path]) -> tuple[Optional[str], Optional[str], str]:
    """(version, digest, source) from the component's pin file."""
    spec = component.pin or {}
    fname = spec.get("file")
    if not fname:
        return None, None, "not configured"
    path = _find_file(fname, dirs)
    if path is None:
        return None, None, f"{fname} not found (set {component.env_override})"
    try:
        text = path.read_text()
    except OSError as e:
        return None, None, f"{path}: {e}"
    kind, name = spec.get("kind"), spec.get("name", component.package)
    if kind == "requirements":
        return parse_requirements_pin(text, name), None, str(path)
    if kind == "image":
        tag, digest = parse_image_pin(text, name)
        return tag, digest, str(path)
    if kind == "uvx":
        return parse_uvx_pin(text, name), None, str(path)
    return None, None, f"unknown pin kind {kind!r}"


def resolve_pin(component: Component, dirs: Optional[list[Path]] = None) -> Pin:
    """The version this deployment runs, and what the repo declares.

    They differ while a merged bump waits for redeploy; the API shows both.
    """
    declared, digest, declared_source = _read_declared(
        component, dirs if dirs is not None else pin_search_dirs())

    env_val = os.environ.get(component.env_override, "").strip()
    if env_val:
        return Pin(version=env_val, source=f"env {component.env_override}", digest=digest,
                   declared=declared, declared_source=declared_source)

    installed = (component.pin or {}).get("installed")
    if installed:
        try:
            return Pin(version=metadata.version(installed), source="installed", digest=digest,
                       declared=declared, declared_source=declared_source)
        except metadata.PackageNotFoundError:
            pass

    return Pin(version=declared, source=declared_source, digest=digest,
               declared=declared, declared_source=declared_source)
