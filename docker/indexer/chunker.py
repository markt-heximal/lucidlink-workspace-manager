"""Text chunking — a line-for-line port of the web app's `chunkText`.

Kept identical (1200 chars, 150 overlap, break on the last newline or ". "
when it falls past the halfway mark, drop pieces of 40 chars or fewer) so
chunks indexed here match what the browser pipeline produced. One known
difference: JS measures strings in UTF-16 code units, Python in code points,
so text with astral-plane characters (emoji) can break a few chars apart.
"""
from __future__ import annotations

import re

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150

# ECMAScript String.prototype.trim(): WhiteSpace + LineTerminator.
_JS_WS = ("\t\n\v\f\r          "
          "        　﻿")
_MANY_NL = re.compile(r"\n{3,}")


def _js_trim(s: str) -> str:
    return s.strip(_JS_WS)


def _last_index_of(s: str, sub: str, position: int) -> int:
    """JS `s.lastIndexOf(sub, position)`: last match starting at or before position."""
    return s.rfind(sub, 0, max(0, position) + len(sub))


def chunk_text(text: str) -> list[str]:
    clean = _js_trim(_MANY_NL.sub("\n\n", text.replace("\r\n", "\n")))
    if not clean:
        return []
    out: list[str] = []
    n = len(clean)
    i = 0
    while i < n:
        end = min(i + CHUNK_CHARS, n)
        if end < n:
            brk = _last_index_of(clean, "\n", end)
            dot = _last_index_of(clean, ". ", end)
            cut = max(brk, dot)
            if cut > i + CHUNK_CHARS * 0.5:
                end = cut + 1
        piece = _js_trim(clean[i:end])
        if len(piece) > 40:
            out.append(piece)
        if end >= n:
            break
        i = max(end - CHUNK_OVERLAP, i + 1)
    return out
