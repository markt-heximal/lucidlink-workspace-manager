"""Per-link cache sizes for `Workspace.link_filespace(id, cache=...)` (SDK 1.0).

Shared by the file service (ll_core) and the indexer (indexer/source.py), which
run the same image with their own Client each.

SDK 1.0 sizes the caches per linked filespace and grows them on demand up to a
maximum. Left unset, the maximum is the filespace-wide one or the SDK default
(5 GiB data + 1 GiB metadata; Beta 0.14 defaulted to 25 GiB + 5 GiB), so it is
set explicitly here and is visible in the deployment:

    LUCIDLINK_CACHE_DATA_GB      default 5   (rounded down to a whole MiB)
    LUCIDLINK_CACHE_METADATA_GB  default 1

Each linked filespace gets its own caches, so the disk worst case for one
service is MAX_CLIENTS x MAX_LINKED_FILESPACES x (data + metadata). The values
are GiB and may be fractional (0.5 = 512 MiB). An unset, unparsable or
non-positive value falls back to the default, with a warning.
"""
from __future__ import annotations

import logging
import math
import os

logger = logging.getLogger("lucidlink_api")

GIB = 1024 ** 3
MIB = 1024 ** 2
DEFAULT_DATA_GB = 5.0
DEFAULT_METADATA_GB = 1.0


def _gb(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not (math.isfinite(value) and value > 0):
        logger.warning("%s=%r is not a positive number of GiB; using %s", name, raw, default)
        return default
    return value


def cache_sizes() -> tuple[int, int]:
    """(data_size, metadata_size) in bytes. The data size is a whole number of
    MiB (the SDK refuses anything else) and at least 1 MiB."""
    data = int(_gb("LUCIDLINK_CACHE_DATA_GB", DEFAULT_DATA_GB) * GIB) // MIB * MIB
    meta = int(_gb("LUCIDLINK_CACHE_METADATA_GB", DEFAULT_METADATA_GB) * GIB)
    return max(MIB, data), max(MIB, meta)


def cache_config():
    """A `lucidlink.CacheConfig` with the configured maximum sizes."""
    import lucidlink
    data, meta = cache_sizes()
    return lucidlink.CacheConfig(data_size=data, metadata_size=meta)
