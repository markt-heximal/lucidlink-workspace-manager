"""Hand-built iWork fixtures: protobuf encoded by hand, a literal-only snappy
encoder, Apple's .iwa chunk framing, and the zip / package layouts."""
from __future__ import annotations

import gzip
import io
import zipfile
from typing import Iterable, Union

Value = Union[int, str, bytes]


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def field(num: int, value: Value) -> bytes:
    if isinstance(value, int):
        return varint(num << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return varint(num << 3 | 2) + varint(len(value)) + value


def msg(*fields: bytes) -> bytes:
    return b"".join(fields)


def snappy_literal(data: bytes) -> bytes:
    """Raw snappy block made of literal elements only (valid, uncompressed)."""
    out = bytearray(varint(len(data)))
    for i in range(0, len(data), 65536):
        piece = data[i:i + 65536]
        n = len(piece) - 1
        if n < 60:
            out.append(n << 2)
        elif n < 256:
            out += bytes([60 << 2, n])
        else:
            out += bytes([61 << 2]) + n.to_bytes(2, "little")
        out += piece
    return bytes(out)


def iwa_chunk(block: bytes) -> bytes:
    return b"\x00" + len(block).to_bytes(3, "little") + block


def iwa(payloads: Iterable[bytes], chunks: int = 2) -> bytes:
    """An .iwa file: [varint len][ArchiveInfo][payload] records, snappy'd and
    split into `chunks` framed chunks."""
    stream = bytearray()
    for ident, payload in enumerate(payloads, 1):
        info = msg(field(1, ident), field(2, msg(field(1, 2001), field(3, len(payload)))))
        stream += varint(len(info)) + info + payload
    stream = bytes(stream)
    step = max(1, -(-len(stream) // chunks))
    return b"".join(iwa_chunk(snappy_literal(stream[i:i + step])) for i in range(0, len(stream), step))


def storage(text: str) -> bytes:
    """A TSWP.StorageArchive-like message: kind, a stylesheet reference, text."""
    return msg(field(1, 0), field(2, msg(field(1, 77))), field(3, text),
               field(5, msg(field(1, msg(field(1, 0), field(2, msg(field(1, 88))))))))


NOISE = msg(
    field(1, "Helvetica-Bold"), field(2, "8F1C2D3E-1111-2222-3333-444455556666"),
    field(3, "com.apple.iWork.Pages"), field(4, "Heading 1"), field(5, "en_US"),
    field(6, "kSFWPDefaultStyle"), field(7, "HelveticaNeue"), field(8, "ArialMT"),
    field(9, "a1b2c3d4e5f60718"), field(10, "image/jpeg"), field(11, "pasted-image-1.png"),
    field(12, "Table Style 2"), field(13, "12.5"), field(14, "TimesNewRomanPSMT"))


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def pages_members(*paragraphs: str) -> dict[str, bytes]:
    body = " ".join(paragraphs)
    return {
        "Index/Document.iwa": iwa([NOISE, storage(body)]),
        "Index/DocumentStylesheet.iwa": iwa([msg(field(1, "Body"), field(2, "Stylesheet Only Words"))]),
        "Index/Metadata.iwa": iwa([msg(field(1, "Metadata Only Words"))]),
        "Metadata/Properties.plist": b"<plist/>",
        "preview.jpg": b"\xff\xd8\xff\xe0 not really a jpeg",
        "Data/big-image.png": b"\x89PNG" + bytes(4096),
    }


def numbers_members(strings: list[str], sheet: str = "Quarterly Sheet") -> dict[str, bytes]:
    # TST.TableDataList-like: entries {1: key, 2: refcount, 3: string}.
    datalist = msg(field(1, 1), field(2, 1), *[
        field(3, msg(field(1, i + 1), field(2, 1), field(3, s))) for i, s in enumerate(strings)])
    tile = msg(field(1, 0), field(3, bytes([0x05, 0x00, 0x02, 0x00, 0x00, 0x00, 0xA0, 0x40, 0x00, 0x00])))
    return {
        "Index/Document.iwa": iwa([msg(field(1, msg(field(1, sheet))), NOISE)]),
        "Index/Tables/DataList-875.iwa": iwa([datalist]),
        "Index/Tables/Tile-876.iwa": iwa([tile]),
        "Index/CalculationEngine.iwa": iwa([msg(field(1, "Calculation Only Words"))]),
    }


def keynote_members(slides: list[tuple[str, str, str]]) -> dict[str, bytes]:
    """slides: (title, body, presenter notes)."""
    members = {"Index/Document.iwa": iwa([msg(field(1, "Show Title Placeholder Words"))]),
               "Index/MasterSlide-1.iwa": iwa([msg(field(1, "Master Only Words"))])}
    for n, (title, body, notes) in enumerate(slides, 1):
        members[f"Index/Slide-{n * 100}.iwa"] = iwa([storage(title), storage(body), storage(notes)])
    return members


def pages09_members(paragraphs: list[str]) -> dict[str, bytes]:
    ns = 'xmlns:sl="http://developer.apple.com/namespaces/sl" xmlns:sf="http://developer.apple.com/namespaces/sf"'
    paras = "".join(f"<sf:p sf:style='p1'>{p}</sf:p>" for p in paragraphs)
    xml = (f"<?xml version='1.0'?><sl:document {ns}><sl:stylesheet><sf:paragraphstyle sf:name='Body'>"
           f"Stylesheet Only Words</sf:paragraphstyle></sl:stylesheet>"
           f"<sf:text-storage><sf:text-body><sf:section>{paras}</sf:section></sf:text-body></sf:text-storage>"
           f"</sl:document>")
    return {"index.xml.gz": gzip.compress(xml.encode()), "QuickLook/Thumbnail.jpg": b"\xff\xd8"}


def write_package(root, members: dict[str, bytes]) -> None:
    """Lay `members` out as a package directory at pathlib `root`."""
    for name, data in members.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
