"""Extraction (tiny OOXML / PDF fixtures built in-test) and chunkText parity."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from indexer.chunker import chunk_text
from indexer.extract import extract

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def _zip(parts: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        for name, xml in parts.items():
            z.writestr(name, xml)
    return buf.getvalue()


def make_pdf(text: str) -> bytes:
    """A minimal one-page PDF with a Helvetica text run and a correct xref."""
    stream = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def test_docx():
    doc = _zip({"word/document.xml":
                f'<w:document {W}><w:body>'
                '<w:p><w:r><w:t>Quarterly budget</w:t></w:r></w:p>'
                '<w:p><w:r><w:t xml:space="preserve">Line one </w:t></w:r><w:r><w:tab/><w:t>tabbed</w:t></w:r></w:p>'
                '</w:body></w:document>'})
    r = extract("plan.docx", doc)
    assert r.skip_reason is None
    assert r.text == "Quarterly budget\nLine one \ttabbed"


def test_pptx_slides_in_numeric_order():
    def slide(t):
        return (f'<p:sld {P} {A}><p:cSld><p:spTree><p:sp><p:txBody>'
                f'<a:p><a:r><a:t>{t}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>')
    deck = _zip({"ppt/slides/slide10.xml": slide("Ten"), "ppt/slides/slide2.xml": slide("Two"),
                 "ppt/slides/slide1.xml": slide("One")})
    r = extract("deck.pptx", deck)
    assert r.text == "Slide 1\nOne\n\nSlide 2\nTwo\n\nSlide 3\nTen"


def test_xlsx_shared_and_inline_strings():
    book = _zip({
        "xl/sharedStrings.xml": f'<sst {S}><si><t>Name</t></si><si><t>Amount</t></si><si><t>Rent</t></si></sst>',
        "xl/worksheets/sheet1.xml":
            f'<worksheet {S}><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>1200</v></c></row>'
            '<row r="3"><c r="A3" t="inlineStr"><is><t>Power</t></is></c><c r="B3"><v>80</v></c></row>'
            '</sheetData></worksheet>'})
    r = extract("costs.xlsx", book)
    assert r.text == "Sheet 1\nName\tAmount\nRent\t1200\nPower\t80"


def test_pdf():
    r = extract("report.pdf", make_pdf("Hello indexed PDF world"))
    assert r.skip_reason is None
    assert "Hello indexed PDF world" in r.text


def test_html_strips_tags_scripts_and_entities():
    html = ("<html><head><title>T</title><style>p{color:red}</style><script>var x=1;</script></head>"
            "<body><h1>Heading</h1><p>Fish &amp; chips<br>second line</p><div>tail</div></body></html>")
    r = extract("page.html", html.encode())
    assert "var x" not in r.text and "color" not in r.text and "<" not in r.text
    assert "Fish & chips" in r.text
    assert [ln for ln in r.text.splitlines() if ln] == ["T", "Heading", "Fish & chips", "second line", "tail"]


def test_skip_reasons():
    assert extract("a.txt", b"\x00\x00binary").skip_reason == "binary content"
    assert extract("a.mp4", b"....").skip_reason == "unsupported type (.mp4)"
    assert extract("a.docx", b"not a zip").skip_reason.startswith("unextractable")
    assert extract("a.pdf", b"%PDF-garbage").skip_reason.startswith("unextractable")
    assert extract("a.md", b"   \n  ").skip_reason == "no extractable text"


def test_text_decoding_fallbacks():
    assert extract("a.csv", "naïve,café".encode("cp1252")).text == "naïve,café"
    assert extract("a.txt", "﻿bom text".encode("utf-8")).text == "bom text"
    assert extract("a.txt", "utf16 text".encode("utf-16")).text == "utf16 text"


def test_chunk_text_parity_with_web_app():
    """Expected chunks were produced by running the web app's chunkText under node."""
    cases = json.loads((Path(__file__).parent / "chunk_parity.json").read_text())
    assert len(cases) >= 5
    for case in cases:
        assert chunk_text(case["input"]) == case["chunks"]


def test_chunk_text_shape():
    text = ". ".join(f"Sentence {i} is here" for i in range(300))
    chunks = chunk_text(text)
    assert all(40 < len(c) <= 1200 for c in chunks)
    assert chunk_text("short") == []
    # Overlap: each chunk starts inside the previous one.
    for a, b in zip(chunks, chunks[1:]):
        assert b[:20] in a
