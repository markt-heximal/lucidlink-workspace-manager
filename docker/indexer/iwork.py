"""Apple iWork (Pages, Numbers, Keynote) text, in pure Python.

A document arrives as zip bytes: either the single-file form as stored, or an
in-memory zip the scanner builds from the members of a package directory (see
`PACKAGE_MEMBERS`). Three layers are tried in order:

1. Modern iWork (2013+): `Index/*.iwa` (possibly inside a nested `Index.zip`).
   Each .iwa is Apple's variant of snappy framing — chunks of
   `[0x00][3-byte little-endian length][raw snappy block]`, no CRC — and
   decompresses to a stream of `[varint length][ArchiveInfo][payloads…]`
   records. Payloads are protobuf messages whose schemas we do not carry, so a
   generic wire-format walker pulls out every length-delimited field that reads
   as human text (TSWP body text, Numbers string tables, Keynote slide text and
   notes), in document order, and drops identifiers, UUIDs, font and style
   names (`human_text`, the one place those heuristics live).
2. iWork '09: `index.xml(.gz)` / `index.apxl(.gz)`, tags stripped.
3. `QuickLook/Preview.pdf` through the PDF extractor.

Everything is bounded: snappy output per block and per document, protobuf
recursion depth and fields visited, XML inflation, and the text itself.
"""
from __future__ import annotations

import gzip
import io
import logging
import re
import zipfile
from typing import Callable, Iterable, Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger("indexer.iwork")

IWORK_EXTS = frozenset({"pages", "numbers", "key"})

# Decompressed bytes allowed for one snappy block / one .iwa / one document.
SNAPPY_MAX_BLOCK = 64 * 1024 * 1024
IWA_MAX_BYTES = 128 * 1024 * 1024
DOC_MAX_INFLATE = 256 * 1024 * 1024
# Protobuf walk bounds.
PB_MAX_DEPTH = 40
PB_MAX_FIELDS = 5_000_000

NO_TEXT = "iWork document with no readable text"
NOT_IWORK = "unextractable: not a valid iWork document (zip)"


class NoText(Exception):
    """Nothing readable; the message is the skip reason."""


class SnappyError(ValueError):
    pass


def is_iwork_name(name: str) -> bool:
    i = name.rfind(".")
    return i > 0 and name[i + 1:].lower() in IWORK_EXTS


# ---------------------------------------------------------------- package members
def package_member_wanted(rel: str) -> bool:
    """Members of a package directory the extractor needs (`rel` is relative,
    '/'-separated). Previews, images and Data/ are never read."""
    low = rel.lower()
    if "/" not in rel:
        return low in ("index.xml", "index.xml.gz", "index.apxl", "index.apxl.gz", "index.zip")
    return rel.startswith("Index/") or rel == "QuickLook/Preview.pdf"


# ---------------------------------------------------------------- snappy
def _varint(buf, pos: int, end: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= end or shift > 63:
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def snappy_decompress(block: bytes, max_out: int = SNAPPY_MAX_BLOCK) -> bytes:
    """Decode one raw snappy block (literal and copy-1/2/4 elements).

    Raises SnappyError on malformed input or when the declared or produced
    size exceeds `max_out`.
    """
    n = len(block)
    try:
        want, pos = _varint(block, 0, n)
    except ValueError:
        raise SnappyError("bad snappy preamble")
    if want > max_out:
        raise SnappyError(f"snappy block declares {want} bytes (> {max_out})")
    out = bytearray()
    while pos < n:
        tag = block[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:                                   # literal
            length = tag >> 2
            if length >= 60:
                extra = length - 59
                if pos + extra > n:
                    raise SnappyError("truncated literal length")
                length = int.from_bytes(block[pos:pos + extra], "little")
                pos += extra
            length += 1
            if pos + length > n:
                raise SnappyError("literal runs past end of block")
            if len(out) + length > want:
                raise SnappyError("output exceeds declared length")
            out += block[pos:pos + length]
            pos += length
            continue
        if kind == 1:                                   # copy, 1-byte offset
            if pos + 1 > n:
                raise SnappyError("truncated copy-1")
            length = 4 + ((tag >> 2) & 7)
            offset = ((tag >> 5) << 8) | block[pos]
            pos += 1
        elif kind == 2:                                 # copy, 2-byte offset
            if pos + 2 > n:
                raise SnappyError("truncated copy-2")
            length = (tag >> 2) + 1
            offset = int.from_bytes(block[pos:pos + 2], "little")
            pos += 2
        else:                                           # copy, 4-byte offset
            if pos + 4 > n:
                raise SnappyError("truncated copy-4")
            length = (tag >> 2) + 1
            offset = int.from_bytes(block[pos:pos + 4], "little")
            pos += 4
        if offset == 0 or offset > len(out):
            raise SnappyError("copy offset out of range")
        if len(out) + length > want:
            raise SnappyError("output exceeds declared length")
        start = len(out) - offset
        if offset >= length:
            out += out[start:start + length]
        else:                                           # overlapping: repeat the pattern
            pattern = bytes(out[start:])
            reps, rest = divmod(length, offset)
            out += pattern * reps + pattern[:rest]
    if len(out) != want:
        raise SnappyError(f"snappy block produced {len(out)} bytes, declared {want}")
    return bytes(out)


def iwa_decompress(data: bytes, max_out: int = IWA_MAX_BYTES) -> bytes:
    """Concatenate the decompressed chunks of one .iwa file."""
    out = bytearray()
    pos, n = 0, len(data)
    while pos < n:
        if pos + 4 > n:
            raise SnappyError("truncated iwa chunk header")
        kind = data[pos]
        length = int.from_bytes(data[pos + 1:pos + 4], "little")
        body = data[pos + 4:pos + 4 + length]
        if len(body) != length:
            raise SnappyError("iwa chunk runs past end of file")
        pos += 4 + length
        if kind == 0x00:
            out += snappy_decompress(body, min(SNAPPY_MAX_BLOCK, max_out - len(out)))
        elif kind == 0x01:                              # uncompressed (not seen from Apple; tolerated)
            if len(out) + length > max_out:
                raise SnappyError("iwa exceeds size limit")
            out += body
        elif 0x80 <= kind <= 0xFE:                      # skippable / padding
            continue
        else:
            raise SnappyError(f"unknown iwa chunk type 0x{kind:02x}")
    return bytes(out)


# ---------------------------------------------------------------- protobuf
class _Budget:
    def __init__(self, fields: int = PB_MAX_FIELDS):
        self.fields = fields

    def spend(self) -> None:
        self.fields -= 1
        if self.fields < 0:
            raise _Exhausted()


class _Exhausted(Exception):
    pass


def pb_fields(buf: bytes, budget: Optional[_Budget] = None) -> Optional[list[tuple[int, int, object]]]:
    """Flat parse of a protobuf message: [(field, wire_type, value)], or None
    when `buf` is not a well-formed message (must consume it exactly; groups
    and wire types 3/4/6/7 are refused)."""
    out = []
    pos, n = 0, len(buf)
    try:
        while pos < n:
            if budget:
                budget.spend()
            key, pos = _varint(buf, pos, n)
            field, wt = key >> 3, key & 7
            if field == 0 or field > 0x1FFFFFFF:
                return None
            if wt == 0:
                v, pos = _varint(buf, pos, n)
            elif wt == 1:
                if pos + 8 > n:
                    return None
                v, pos = None, pos + 8
            elif wt == 5:
                if pos + 4 > n:
                    return None
                v, pos = None, pos + 4
            elif wt == 2:
                ln, pos = _varint(buf, pos, n)
                if pos + ln > n:
                    return None
                v, pos = buf[pos:pos + ln], pos + ln
            else:
                return None
            out.append((field, wt, v))
    except ValueError:
        return None
    return out


# Normalise Apple's text separators: paragraph / line separators and the
# control characters TSWP uses for breaks become newlines; the object
# replacement character (attachments, footnote marks) disappears.
_SEPARATORS = str.maketrans({" ": "\n", " ": "\n", "\u000b": "\n", "\u000c": "\n",
                             "\u0004": "\n", "\u0005": "\n", "\u000e": "\n", "￼": "", " ": " "})

_UUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_HEX = re.compile(r"(0x)?[0-9A-Fa-f]{8,}")
_DOTTED = re.compile(r"[a-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)+")      # com.apple.x, public.jpeg
_MIME = re.compile(r"[a-z]+/[A-Za-z0-9.+-]+")
_LOCALE = re.compile(r"[a-z]{2,3}[_-][A-Z][A-Za-z]{1,3}")
_FILENAME = re.compile(r".+\.(jpe?g|png|gif|tiff?|heic|pdf|iwa|plist|mov|mp4|m4a|svg|bmp|webp|zip)", re.I)
_FONT_STYLES = ("Regular|Bold|Italic|Light|Medium|Semibold|SemiBold|Demibold|DemiBold|Heavy|Black|Thin|"
                "UltraLight|ExtraLight|ExtraBold|UltraBold|Oblique|Roman|Book|Condensed|Narrow|Wide|"
                "BoldItalic|BoldOblique|LightItalic|MediumItalic|SemiboldItalic|ThinItalic|HeavyItalic|"
                "BlackItalic|CondensedBold|CondensedLight|CondensedBlack|Display|Text")
_FONT = re.compile(rf"\.?[A-Z][A-Za-z0-9]*-({_FONT_STYLES})+|[A-Z][A-Za-z]{{2,}}(PSMT|MT)")
_FONT_FAMILIES = frozenset({
    "Helvetica", "HelveticaNeue", "Helvetica Neue", "Arial", "Times", "Times New Roman", "TimesNewRoman",
    "Courier", "Courier New", "Menlo", "Monaco", "Georgia", "Verdana", "Avenir", "Avenir Next", "AvenirNext",
    "Futura", "Gill Sans", "GillSans", "Optima", "Palatino", "Baskerville", "Didot", "Cochin", "Hoefler Text",
    "Lucida Grande", "LucidaGrande", "San Francisco", "SF Pro", "SF Pro Text", "SF Pro Display", "Calibri",
    "Cambria", "Garamond", "Tahoma", "Trebuchet MS", "Geneva", "Charter", "Seravek", "Superclarendon",
    "American Typewriter", "Chalkboard", "Marker Felt", "Noteworthy", "Rockwell", "Impact", "Symbol",
    "Apple Color Emoji", "AppleColorEmoji", "Zapf Dingbats", "Wingdings"})
# Exact strings iWork itself writes into every document (style and placeholder names).
_STYLE_NAMES = frozenset({
    "Body", "Body Small", "Title", "Subtitle", "Heading", "Heading 1", "Heading 2", "Heading 3", "Heading 4",
    "Heading 5", "Heading 6", "Heading Red", "Caption", "Caption Red", "Free Form", "Normal", "None",
    "Default", "Bullet", "Bullet Big", "Harvey", "Header", "Footer", "Footnote Text", "Label", "Quote",
    "Attribution", "Body Level One", "Body Level Two", "Body Level Three", "Body Level Four",
    "Body Level Five", "Slide Title", "Slide Subtitle", "Slide Number", "Notes", "Table Style",
    "Text Box", "Basic", "Numbered", "Lettered", "Dash", "Image", "Photo", "Chart", "Table",
    "Title & Subtitle", "Title & Bullets", "Title, Bullets & Photo", "Title - Center", "Title - Top",
    "Bullets", "Section", "Agenda", "Statement", "Big Fact", "Blank", "Photo - Horizontal",
    "Photo - Vertical", "Photo - 3 Up", "Header Row", "Header Column", "Footer Row", "Table Name",
    "Table Style 1", "Paragraph Style", "Character Style", "List Style", "Shape", "Line", "Text"})
_STYLE_PATTERN = re.compile(r"(Table|Chart|Shape|Line|Image|Text|Paragraph|Character|List|Cell) Style( \d+)?"
                            r"|(Heading|Body|Caption|Title) \d+")


def human_text(s: str) -> bool:
    """Does a protobuf string look like document text rather than plumbing?

    The single tunable home for every heuristic: printable, mostly letters /
    digits / spaces / punctuation, at least one letter, and not a UUID, hex or
    reverse-DNS identifier, MIME type, locale, file name, font or style name.
    """
    t = s.strip()
    if len(t) < 2:
        return False
    letters = sum(1 for ch in t if ch.isalpha())
    if not letters:
        return False                                   # numbers, punctuation, version strings
    good = 0
    for ch in t:
        o = ord(ch)
        if o < 32 and ch not in "\t\n\r":
            return False
        if ch.isalnum() or ch.isspace() or (32 <= o < 127) or ch in "–—‘’“”…•€£¥°·«»¿¡":
            good += 1
    if good / len(t) < 0.9:
        return False
    if t in _STYLE_NAMES or t in _FONT_FAMILIES or _STYLE_PATTERN.fullmatch(t):
        return False
    if any(ch.isspace() for ch in t):
        return True                                    # multi-word: text
    # A single token: reject identifiers and plumbing.
    if t.startswith(("http://", "https://", "mailto:")) or ("@" in t and "." in t.split("@")[-1]):
        return True
    if (_UUID.search(t) or _HEX.fullmatch(t) or _DOTTED.fullmatch(t) or _MIME.fullmatch(t)
            or _LOCALE.fullmatch(t) or _FILENAME.fullmatch(t) or _FONT.fullmatch(t)):
        return False
    if "_" in t or len(t) > 40:
        return False                                   # snake_case keys, base64, hashes
    if re.search(r"[a-z][A-Z].*[a-z][A-Z]", t):
        return False                                   # camelCaseWithSeveralHumps
    if t[0] == "k" and len(t) > 3 and t[1].isupper():
        return False                                   # kSFWPSomething constants
    return True


def _as_text(v: bytes) -> Optional[str]:
    try:
        s = v.decode("utf-8")
    except UnicodeDecodeError:
        return None
    s = s.translate(_SEPARATORS)
    return s if human_text(s) else None


def _walk(fields, depth: int, budget: _Budget, out: list[str]) -> None:
    for _field, wt, v in fields:
        if wt != 2 or not v:
            continue
        text = _as_text(v)
        sub = pb_fields(v, budget) if depth < PB_MAX_DEPTH else None
        if sub:
            found: list[str] = []
            _walk(sub, depth + 1, budget, found)
            # A buffer that is both valid protobuf and plausible text ("Hi" is
            # field 9 = 105): the nested reading wins only when it holds text.
            if found or text is None:
                out.extend(found)
                continue
        if text is not None:
            out.append(text)


def pb_strings(message: bytes, budget: Optional[_Budget] = None) -> list[str]:
    """Human text found anywhere inside one protobuf message, in field order."""
    budget = budget or _Budget()
    out: list[str] = []
    try:
        fields = pb_fields(message, budget)
        if fields:
            _walk(fields, 1, budget, out)
    except _Exhausted:
        pass
    return out


def iwa_messages(stream: bytes) -> Iterable[bytes]:
    """Payload messages of a decompressed .iwa stream, ArchiveInfo skipped."""
    pos, n = 0, len(stream)
    while pos < n:
        try:
            ln, pos = _varint(stream, pos, n)
        except ValueError:
            return
        info = pb_fields(stream[pos:pos + ln]) if pos + ln <= n else None
        if info is None:
            logger.debug("iwa: malformed ArchiveInfo at %d", pos)
            return
        pos += ln
        for field, wt, v in info:
            if field != 2 or wt != 2:
                continue                                 # 1 = identifier, 3 = should_merge
            mi = pb_fields(v) or []
            length = next((x for f, w, x in mi if f == 3 and w == 0), 0)
            if pos + length > n:
                return
            yield stream[pos:pos + length]
            pos += length


def _tidy(parts: Iterable[str], max_chars: int) -> str:
    out: list[str] = []
    total = 0
    prev = None
    for p in parts:
        p = re.sub(r"[ \t\r]+", " ", p)
        p = re.sub(r" *\n *", "\n", p)
        p = re.sub(r"\n{3,}", "\n\n", p).strip()
        if not p or p == prev:
            continue
        prev = p
        out.append(p)
        total += len(p) + 1
        if total >= max_chars:
            break
    return "\n".join(out)[:max_chars]


# ---------------------------------------------------------------- IWA documents
# Archives that only ever hold styles, fonts, view state or metadata.
_IWA_SKIP = ("Stylesheet", "ViewState", "Metadata", "CalculationEngine", "AnnotationAuthorStorage",
             "MasterSlide", "TemplateSlide", "Properties")


def _natural(name: str):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def iwa_order(names: list[str]) -> list[str]:
    """Document.iwa first, then the rest in natural name order; noise skipped."""
    keep = [n for n in names if not any(s in n.rsplit("/", 1)[-1] for s in _IWA_SKIP)]
    return sorted(keep, key=lambda n: (n.rsplit("/", 1)[-1] != "Document.iwa", _natural(n)))


def iwa_text(blobs: Iterable[tuple[str, bytes]], max_chars: int) -> str:
    budget = _Budget()
    inflated = 0

    def parts():
        nonlocal inflated
        for name, blob in blobs:
            try:
                stream = iwa_decompress(blob, min(IWA_MAX_BYTES, DOC_MAX_INFLATE - inflated))
            except SnappyError as e:
                logger.info("iwa %s unreadable: %s", name, e)
                continue
            inflated += len(stream)
            for msg in iwa_messages(stream):
                try:
                    yield from pb_strings(msg, budget)
                except _Exhausted:
                    return
                if budget.fields <= 0:
                    return

    return _tidy(parts(), max_chars)


# ---------------------------------------------------------------- iWork '09 XML
def _xml_text(data: bytes, max_chars: int) -> str:
    root = ET.fromstring(data)
    parts: list[str] = []
    para = {"p", "li", "cell", "t", "title", "notes", "page-start", "br", "lnbr", "pgbr", "tab"}

    def visit(el):
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in ("stylesheet", "styles", "anon-styles", "metadata", "calc-engine", "theme",
                   "master-slides", "data", "unfiltered", "headers", "footers"):
            return
        if tag == "ct":                                  # Numbers '09 text cell: <sf:ct sfa:s="…"/>
            for k, v in el.attrib.items():
                if k.rsplit("}", 1)[-1] == "s" and v:
                    parts.append("\n" + v)
        if el.text:
            parts.append(el.text)
        for child in el:
            visit(child)
            if child.tail:
                parts.append(child.tail)
        if tag in para:
            parts.append("\n")

    visit(root)
    text = "".join(parts)
    return _tidy(text.split("\n"), max_chars)


def _bounded_gunzip(data: bytes, limit: int) -> bytes:
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
        out = g.read(limit + 1)
    if len(out) > limit:
        raise NoText("unextractable: iWork '09 index inflates beyond limit")
    return out


# ---------------------------------------------------------------- entry point
def _read_member(z: zipfile.ZipFile, info: zipfile.ZipInfo, budget: list[int]) -> bytes:
    if info.file_size > budget[0]:
        raise NoText("unextractable: archive inflates beyond limit")
    with z.open(info) as f:
        data = f.read(budget[0] + 1)
    budget[0] -= len(data)
    if budget[0] < 0:
        raise NoText("unextractable: archive inflates beyond limit")
    return data


def extract_text(data: bytes, pdf: Callable[[bytes], str], max_chars: int) -> str:
    """Text of an iWork document given as zip bytes; NoText(reason) otherwise."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError):
        raise NoText(NOT_IWORK)
    budget = [DOC_MAX_INFLATE]
    infos = {i.filename: i for i in z.infolist() if not i.is_dir()}

    # 1. Modern IWA (possibly inside a nested Index.zip).
    iwa: dict[str, bytes] = {}
    for name in iwa_order([n for n in infos if n.endswith(".iwa") and
                           (n.startswith("Index/") or "/Index/" in n)]):
        iwa[name] = _read_member(z, infos[name], budget)
    nested = next((n for n in infos if n.rsplit("/", 1)[-1] == "Index.zip"), None)
    if nested:
        try:
            nz = zipfile.ZipFile(io.BytesIO(_read_member(z, infos[nested], budget)))
            ninfos = {i.filename: i for i in nz.infolist() if not i.is_dir()}
            for name in iwa_order([n for n in ninfos if n.endswith(".iwa")]):
                iwa.setdefault("Index.zip:" + name, _read_member(nz, ninfos[name], budget))
        except zipfile.BadZipFile:
            logger.info("iwork: nested Index.zip is not a zip")
    if iwa:
        ordered = iwa_order(list(iwa))
        text = iwa_text(((n, iwa[n]) for n in ordered), max_chars)
        if text.strip():
            return text

    # 2. iWork '09 XML.
    lower = {n.lower(): n for n in infos}
    for candidate in ("index.xml.gz", "index.xml", "index.apxl.gz", "index.apxl"):
        name = lower.get(candidate)
        if not name:
            continue
        raw = _read_member(z, infos[name], budget)
        if candidate.endswith(".gz"):
            raw = _bounded_gunzip(raw, min(budget[0], DOC_MAX_INFLATE))
        try:
            text = _xml_text(raw, max_chars)
        except ET.ParseError as e:
            logger.info("iwork '09 xml unreadable: %s", e)
            continue
        if text.strip():
            return text

    # 3. QuickLook preview.
    name = lower.get("quicklook/preview.pdf")
    if name:
        try:
            text = pdf(_read_member(z, infos[name], budget))
        except NoText:
            raise
        except Exception as e:
            logger.info("iwork preview pdf unreadable: %s", e)
            text = ""
        if text and text.strip():
            return text[:max_chars]
    raise NoText(NO_TEXT)
