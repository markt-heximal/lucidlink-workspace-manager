"""iWork extraction: snappy, .iwa framing, the protobuf text walker, and the
fallbacks (iWork '09 XML, QuickLook preview), all on fixtures built in-test."""
from __future__ import annotations

import pytest

from iwork_fixtures import (NOISE, field, iwa, iwa_chunk, keynote_members, msg, numbers_members,
                            pages09_members, pages_members, snappy_literal, storage, varint, zip_bytes)
from test_idx_extract import make_pdf

from indexer import iwork
from indexer.extract import MAX_TEXT_CHARS, extract, is_supported
from indexer.iwork import SnappyError, human_text, iwa_decompress, pb_strings, snappy_decompress


# ------------------------------------------------------------ snappy
def _reference(elements) -> bytes:
    """Decode element tuples byte by byte (the obviously-correct way)."""
    out = bytearray()
    for kind, a, b in elements:
        if kind == "lit":
            out += a
        else:
            for _ in range(a):          # (length, offset)
                out.append(out[-b])
    return bytes(out)


def test_snappy_copy_ops_hand_made_block():
    elements = [("lit", b"Hello ", None), ("copy", 6, 6), ("copy", 8, 3), ("copy", 5, 20),
                ("lit", b"!", None), ("copy", 40, 1)]
    expected = _reference(elements)
    block = bytearray(varint(len(expected)))
    block += bytes([(6 - 1) << 2]) + b"Hello "                         # literal
    block += bytes([((6 >> 8) << 5) | ((6 - 4) << 2) | 1, 6])          # copy-1: len 6, offset 6
    block += bytes([((8 - 1) << 2) | 2]) + (3).to_bytes(2, "little")   # copy-2: len 8, offset 3 (overlap)
    block += bytes([((5 - 1) << 2) | 3]) + (20).to_bytes(4, "little")  # copy-4: len 5, offset 20
    block += bytes([0 << 2]) + b"!"
    block += bytes([((40 - 1) << 2) | 2]) + (1).to_bytes(2, "little")  # run-length via offset 1
    assert snappy_decompress(bytes(block)) == expected
    assert expected.startswith(b"Hello Hello lo lo loHello!!!")


def test_snappy_literal_round_trip_incl_long_literals():
    data = bytes(range(256)) * 300 + b"tail"
    assert snappy_decompress(snappy_literal(data)) == data
    assert snappy_decompress(snappy_literal(b"")) == b""


@pytest.mark.parametrize("block", [
    varint(10) + bytes([(3 - 1) << 2]) + b"abc" + bytes([((4 - 4) << 2) | 1, 9]),   # offset beyond output
    varint(10) + bytes([(3 - 1) << 2]) + b"abc" + bytes([((4 - 4) << 2) | 1, 0]),   # offset 0
    varint(5) + bytes([(9 - 1) << 2]) + b"abc",                                      # literal past end
    varint(3) + bytes([(3 - 1) << 2]) + b"abc" + bytes([((4 - 4) << 2) | 1, 3]),     # exceeds declared size
    varint(8) + bytes([(3 - 1) << 2]) + b"abc",                                      # short of declared size
    b"\xff\xff\xff",                                                                  # truncated preamble
])
def test_snappy_malformed_rejected(block):
    with pytest.raises(SnappyError):
        snappy_decompress(block)


def test_snappy_bomb_rejected():
    # A tiny block that declares (and would produce) far more than allowed.
    bomb = varint(1 << 40) + bytes([0]) + b"x" + bytes([((64 - 1) << 2) | 2]) + (1).to_bytes(2, "little")
    with pytest.raises(SnappyError):
        snappy_decompress(bomb)
    run = varint(10_000) + bytes([0]) + b"x" + (bytes([((64 - 1) << 2) | 2]) + (1).to_bytes(2, "little")) * 200
    with pytest.raises(SnappyError):
        snappy_decompress(run, max_out=4096)
    with pytest.raises(SnappyError):
        iwa_decompress(iwa_chunk(run) * 3, max_out=8192)
    with pytest.raises(SnappyError):
        iwa_decompress(b"\x00\xff\xff\x00abc")       # chunk longer than the file


# ------------------------------------------------------------ protobuf walker
def test_walker_keeps_text_in_order_and_drops_noise():
    m = msg(NOISE, field(20, msg(field(1, 5), field(2, "First real sentence here."))),
            field(21, "Second real sentence."), field(22, "Hi"),
            field(23, msg(field(1, msg(field(3, "Deeply nested words"))))))
    assert pb_strings(m) == ["First real sentence here.", "Second real sentence.", "Hi",
                             "Deeply nested words"]


@pytest.mark.parametrize("s", ["8F1C2D3E-1111-2222-3333-444455556666", "Helvetica-Bold", "HelveticaNeue",
                               "ArialMT", "Avenir Next", "com.apple.iWork.Keynote", "en_US", "image/png",
                               "kSFWPDefault", "deadbeefcafe0011", "IMG_1234.jpg", "Heading 1", "Body",
                               "Table Style 3", "42", "3.14", "x", "snake_case_key", "someCamelCaseIdent"])
def test_human_text_rejects_plumbing(s):
    assert not human_text(s)


@pytest.mark.parametrize("s", ["Budget", "Q3 revenue grew 12%", "Café au lait", "Hi", "KPIs",
                               "https://example.com/report", "Grüße aus München", "東京の会議"])
def test_human_text_keeps_words(s):
    assert human_text(s)


def test_walker_bounds_depth_and_work():
    deep = field(1, "Bottom words")
    for _ in range(200):
        deep = field(1, deep)
    assert pb_strings(deep) == []                     # depth-capped: gives up, never recurses forever
    shallow = field(1, "Bottom words")
    for _ in range(20):
        shallow = field(1, shallow)
    assert pb_strings(shallow) == ["Bottom words"]
    big = msg(*[field(1, "Some words") for _ in range(1000)])
    assert len(pb_strings(big)) == 1000
    assert len(pb_strings(big, iwork._Budget(50))) < 1000   # budget exhaustion stops quietly


# ------------------------------------------------------------ documents
def test_pages_zip_text_and_noise():
    data = zip_bytes(pages_members("Quarterly board minutes.", "Revenue grew in the north region."))
    assert is_supported("Minutes.pages")
    r = extract("Minutes.pages", data)
    assert r.skip_reason is None
    assert r.text == "Quarterly board minutes.\nRevenue grew in the north region."
    for noise in ("Helvetica", "8F1C2D3E", "com.apple", "Stylesheet Only", "Metadata Only", "en_US"):
        assert noise not in r.text


def test_pages_nested_index_zip():
    inner = zip_bytes({"Index/Document.iwa": iwa([storage("Text inside the nested index archive")])})
    r = extract("Old.pages", zip_bytes({"Index.zip": inner, "preview.jpg": b"\xff\xd8"}))
    assert r.text == "Text inside the nested index archive"


def test_numbers_strings():
    data = zip_bytes(numbers_members(["Region", "North America", "Gross margin"]))
    r = extract("Plan.numbers", data)
    assert r.skip_reason is None
    lines = r.text.split("\n")
    assert lines[0] == "Quarterly Sheet"
    assert lines[1:] == ["Region", "North America", "Gross margin"]
    assert "Calculation Only" not in r.text


def test_keynote_slides_and_notes_in_slide_order():
    slides = [("Opening title", "Why we are here today", "Speaker note: smile"),
              ("Second slide title", "Market overview body", "Remember the chart")]
    members = keynote_members(slides)
    # Natural order: Slide-200 must come after Slide-100 even though "2" > "1" either way;
    # add a Slide-1000 to make lexical and natural order differ.
    members["Index/Slide-1000.iwa"] = iwa([storage("Closing thoughts slide")])
    r = extract("Deck.key", zip_bytes(members))
    assert r.text.split("\n") == ["Show Title Placeholder Words", "Opening title", "Why we are here today",
                                  "Speaker note: smile", "Second slide title", "Market overview body",
                                  "Remember the chart", "Closing thoughts slide"]
    assert "Master Only" not in r.text


def test_consecutive_repeats_collapse_and_text_is_capped():
    r = extract("Rep.pages", zip_bytes({"Index/Document.iwa": iwa([storage("Same line again")] * 5)}))
    assert r.text == "Same line again"
    long = ("All work and no play makes a dull document. " * 2000)
    payloads = [storage(long + str(i)) for i in range(40)]
    r = extract("Big.pages", zip_bytes({"Index/Document.iwa": iwa(payloads, chunks=8)}))
    assert len(r.text) <= MAX_TEXT_CHARS


def test_iwork09_index_xml_gz_fallback():
    r = extract("Letter.pages", zip_bytes(pages09_members(["Dear board,", "The audit is complete."])))
    assert r.skip_reason is None
    assert r.text == "Dear board,\nThe audit is complete."


def test_iwork09_numbers_cells_in_attributes():
    xml = ("<ls:document xmlns:ls='urn:ls' xmlns:sf='urn:sf' xmlns:sfa='urn:sfa'><sf:tabular-model>"
           "<sf:t><sf:ct sfa:s='Cell words one'/></sf:t><sf:t><sf:ct sfa:s='Cell words two'/></sf:t>"
           "</sf:tabular-model></ls:document>")
    r = extract("Old.numbers", zip_bytes({"index.xml": xml.encode()}))
    assert r.text == "Cell words one\nCell words two"


def test_preview_pdf_fallback():
    members = {"Index/Document.iwa": iwa([NOISE]), "QuickLook/Preview.pdf": make_pdf("Preview only text"),
               "QuickLook/Thumbnail.jpg": b"\xff\xd8"}
    r = extract("Scan.key", zip_bytes(members))
    assert r.skip_reason is None and "Preview only text" in r.text


def test_no_text_and_garbage_are_skipped_with_reasons():
    assert extract("Empty.pages", zip_bytes({"Index/Document.iwa": iwa([NOISE]),
                                             "preview.jpg": b"\xff"})).skip_reason == iwork.NO_TEXT
    assert extract("Bad.numbers", b"not a zip at all").skip_reason == iwork.NOT_IWORK
    broken = zip_bytes({"Index/Document.iwa": b"\x00\xff\x00\x00garbage"})
    assert extract("Broken.key", broken).skip_reason == iwork.NO_TEXT
