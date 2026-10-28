"""Our own audit findings per release, fed to the summarizer.

Release notes for LucidLink packages are often a line or nothing, so an LLM
asked "what matters here?" can only answer "not stated". These notes are what
we learned by reading the package's code (docs/UPDATES.md, "Audit notes"): the
API changes that hit this app, what the upgrade involves, and its status here.

audit.json ships in the image; a file at UPDATES_AUDIT_FILE (default
/data/updates-audit.json) is merged over it per component and version, so a new
finding needs no rebuild. Everything is optional: no notes, no change.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("lucidlink_api.updates")

PACKAGED = Path(__file__).with_name("audit.json")
DEFAULT_OVERRIDE = "/data/updates-audit.json"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("update audit notes %s unreadable: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def load(override: Optional[str] = None) -> dict:
    """component -> version -> {status, notes}, packaged merged with the override."""
    merged: dict = {}
    paths = [PACKAGED, Path(override or os.environ.get("UPDATES_AUDIT_FILE", DEFAULT_OVERRIDE))]
    for path in paths:
        for comp, versions in _read(path).items():
            if comp.startswith("_") or not isinstance(versions, dict):
                continue
            slot = merged.setdefault(comp, {})
            for version, entry in versions.items():
                if isinstance(entry, dict):
                    slot[version] = {**slot.get(version, {}), **entry}
    return merged


def for_release(notes: dict, component: str, version: str) -> tuple[str, str]:
    """(audit text, status) for one release: version-specific notes after the
    component's general ones."""
    versions = notes.get(component) or {}
    general, specific = versions.get("*") or {}, versions.get(version) or {}
    text = "\n".join(t for t in (general.get("notes"), specific.get("notes")) if t)
    return text, specific.get("status") or ""


def fingerprint(*parts: str) -> str:
    """Identifies the context a summary was written from, so it is rewritten
    when the audit (or the prompt) changes rather than kept forever."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
