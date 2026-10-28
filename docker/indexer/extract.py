"""Turn file bytes into plain text, or say why not.

Formats: the text family the web app indexed (md, txt, csv, json, …), HTML with
tags stripped, DOCX / PPTX / XLSX read straight from their OOXML parts with
`zipfile` + ElementTree (no python-docx etc. in the image), PDF via pypdf, and
Apple iWork (Pages / Numbers / Keynote, see iwork.py) from their zip form.

Anything else, anything binary, or anything that fails to parse comes back as
a skip with a human-readable reason that ends up in the ledger and in status.
Extraction never raises.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Optional
from xml.etree import ElementTree as ET

from . import iwork

logger = logging.getLogger("indexer.extract")

TEXT_EXTS = {"md", "markdown", "txt", "log", "csv", "tsv", "json", "xml", "yml", "yaml", "rtf"}
HTML_EXTS = {"html", "htm"}
OFFICE_EXTS = {"docx", "pptx", "xlsx"}
PDF_EXTS = {"pdf"}
IWORK_EXTS = set(iwork.IWORK_EXTS)
SUPPORTED_EXTS = TEXT_EXTS | HTML_EXTS | OFFICE_EXTS | PDF_EXTS | IWORK_EXTS

# Extracted text beyond this is truncated (a 25 MB CSV is mostly noise for RAG).
MAX_TEXT_CHARS = 2_000_000
# Refuse OOXML archives that inflate beyond this (zip bombs).
MAX_UNZIPPED_BYTES = 256 * 1024 * 1024


@dataclass
class Extracted:
    text: Optional[str] = None
    skip_reason: Optional[str] = None


def ext_of(name: str) -> str:
    i = name.rfind(".")
    return name[i + 1:].lower() if i != -1 else ""


def is_supported(name: str) -> bool:
    return ext_of(name) in SUPPORTED_EXTS


def extract(name: str, data: bytes) -> Extracted:
    ext = ext_of(name)
    try:
        if ext in TEXT_EXTS:
            text = _decode_text(data)
            if text is None:
                return Extracted(skip_reason="binary content")
            if ext == "rtf":
                text = _strip_rtf(text)
        elif ext in HTML_EXTS:
            text = _decode_text(data)
            if text is None:
                return Extracted(skip_reason="binary content")
            text = html_to_text(text)
        elif ext in OFFICE_EXTS:
            text = _office(ext, data)
        elif ext in PDF_EXTS:
            text = _pdf(data)
        elif ext in IWORK_EXTS:
            try:
                text = iwork.extract_text(data, _pdf, MAX_TEXT_CHARS)
            except iwork.NoText as e:
                return Extracted(skip_reason=str(e))
        else:
            return Extracted(skip_reason=f"unsupported type (.{ext})" if ext else "unsupported type")
    except _Skip as s:
        return Extracted(skip_reason=str(s))
    except Exception as e:  # malformed file; never let one file stop a scan
        logger.info("extract failed for %s: %s", name, e)
        return Extracted(skip_reason=f"unextractable: {type(e).__name__}: {e}"[:300])
    text = (text or "")[:MAX_TEXT_CHARS]
    if not text.strip():
        return Extracted(skip_reason="no extractable text")
    return Extracted(text=text)


class _Skip(Exception):
    pass


# ---------------------------------------------------------------- text
def _decode_text(data: bytes) -> Optional[str]:
    """Decode a text file, or None when it is really binary."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Legacy exports (Windows-1252 CSVs). Mostly-control bytes means binary.
        text = data.decode("cp1252", errors="replace")
        sample = text[:8192]
        ctrl = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\t\n\r\f")
        return None if sample and ctrl / len(sample) > 0.1 else text


_RTF_HEX = re.compile(r"\\'([0-9a-fA-F]{2})")
_RTF_PAR = re.compile(r"\\(par|line|sect|page)\b ?")
_RTF_DEST = re.compile(r"\{\\\*[^{}]*\}")
_RTF_CTRL = re.compile(r"\\[a-zA-Z]+-?\d* ?")


def _strip_rtf(text: str) -> str:
    text = _RTF_DEST.sub("", text)
    text = _RTF_PAR.sub("\n", text)
    text = _RTF_HEX.sub(lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"), text)
    text = _RTF_CTRL.sub("", text)
    text = text.replace("\\{", "{").replace("\\}", "}").replace("\\\\", "\\")
    return text.replace("{", "").replace("}", "")


# ---------------------------------------------------------------- html
_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header",
               "footer", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "hr", "title"}


class _HtmlText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _HtmlText()
    p.feed(html)
    p.close()
    text = unescape("".join(p.parts))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------- OOXML
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise _Skip("unextractable: not a valid OOXML (zip) file")
    if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED_BYTES:
        raise _Skip("unextractable: archive inflates beyond limit")
    return z


def _paragraph_text(root, para_tag: str = "p") -> list[str]:
    """Text of each paragraph element (w:p / a:p), honouring tabs and breaks."""
    out = []
    for el in root.iter():
        if _local(el.tag) != para_tag:
            continue
        buf = []
        for node in el.iter():
            name = _local(node.tag)
            if name == "t" and node.text:
                buf.append(node.text)
            elif name == "tab":
                buf.append("\t")
            elif name in ("br", "cr"):
                buf.append("\n")
        out.append("".join(buf))
    return out


def _numbered(names: list[str], pattern: str) -> list[str]:
    rx = re.compile(pattern)
    hits = [(int(m.group(1)), n) for n in names if (m := rx.fullmatch(n))]
    return [n for _, n in sorted(hits)]


def _office(ext: str, data: bytes) -> str:
    z = _open_zip(data)
    names = z.namelist()
    if ext == "docx":
        if "word/document.xml" not in names:
            raise _Skip("unextractable: missing word/document.xml")
        return "\n".join(_paragraph_text(ET.fromstring(z.read("word/document.xml"))))
    if ext == "pptx":
        slides = _numbered(names, r"ppt/slides/slide(\d+)\.xml")
        parts = []
        for n, slide in enumerate(slides, 1):
            paras = [p for p in _paragraph_text(ET.fromstring(z.read(slide))) if p.strip()]
            parts.append(f"Slide {n}\n" + "\n".join(paras))
        return "\n\n".join(parts)
    # xlsx
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
            if _local(si.tag) == "si":
                shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    parts = []
    for n, sheet in enumerate(_numbered(names, r"xl/worksheets/sheet(\d+)\.xml"), 1):
        rows = []
        for row in ET.fromstring(z.read(sheet)).iter():
            if _local(row.tag) != "row":
                continue
            cells = []
            for c in row:
                if _local(c.tag) != "c":
                    continue
                kind = c.get("t")
                if kind == "inlineStr":
                    cells.append("".join(t.text or "" for t in c.iter() if _local(t.tag) == "t"))
                    continue
                v = next((x.text for x in c if _local(x.tag) == "v"), None)
                if v is None:
                    continue
                if kind == "s":
                    try:
                        v = shared[int(v)]
                    except (ValueError, IndexError):
                        continue
                cells.append(v)
            if any(cells):
                rows.append("\t".join(cells))
        if rows:
            parts.append(f"Sheet {n}\n" + "\n".join(rows))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- pdf
def _pdf(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise _Skip("encrypted pdf")
        except _Skip:
            raise
        except Exception:
            raise _Skip("encrypted pdf")
    pages = []
    total = 0
    for page in reader.pages:
        t = page.extract_text() or ""
        pages.append(t)
        total += len(t)
        if total > MAX_TEXT_CHARS:
            break
    return "\n\n".join(pages)
