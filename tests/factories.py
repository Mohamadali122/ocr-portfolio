"""Real files, not hand-rolled byte strings.

A hand-written `%PDF-1.4 ... trailer` has no xref table and no `%%EOF`, so pypdf
rejects it -- correctly. Tests that assert on parsing must feed the parser
something a parser accepts, or they only test the error path.
"""

import io
import struct
import zlib


def make_pdf(pages: int = 1) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)  # US Letter
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def make_png(width: int = 1, height: int = 1) -> bytes:
    """A valid 1x1 greyscale PNG, built by hand to avoid a Pillow dependency."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def make_pdf_with_text(text: str) -> bytes:
    """A real single-page PDF with an actual text layer -- for
    app/ocr/pdf_text.py, which needs PDFium to find real content, not a blank
    page like `make_pdf`. Hand-rolled with a proper xref table and trailer (a
    stub PDFium would reject, same discipline as `make_jpeg` above), using the
    standard Helvetica font so no font file needs embedding.
    """
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    stream = f"BT /F1 18 Tf 72 700 Td ({escaped}) Tj ET".encode()

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]  # object 0 is the free-list head, xref entry only
    for index, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")

    xref_offset = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF".encode()
    )
    return out.getvalue()


def make_jpeg(width: int = 4, height: int = 4) -> bytes:
    """A genuinely decodable JPEG.

    Phase 3 only sniffed the magic bytes, so a JFIF header stub sufficed. Phase 4
    hands this to Pillow and PDFium, which will reject a stub -- correctly. A
    fixture that only exercises the error path tests nothing.
    """
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(255, 255, 255)).save(buffer, format="JPEG")
    return buffer.getvalue()
