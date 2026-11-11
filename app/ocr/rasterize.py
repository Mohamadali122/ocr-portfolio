"""Turn an uploaded document into PNG page images.

PDFs are rendered by pypdfium2 (PDFium, the renderer in Chrome). Images pass
through re-encoded as PNG so that the engine receives exactly one format.

Two things here are not conveniences:

- The DPI is a memory bound. A page rendered at 600 DPI is four times the pixels
  of 300 DPI. At 300 DPI a US Letter page is ~8.4 megapixels, ~25 MB as RGB.
  A 50-page document at 600 DPI would be 5 GB of decoded bitmap.
- pypdfium2 and Pillow are C libraries parsing attacker-controlled input. The
  page cap in `file_validation` runs before anything reaches them.
"""

import io

from app.core.logging import get_logger
from app.ocr.base import OcrError

logger = get_logger(__name__)

# A4/Letter at 300 DPI is the floor for reliable OCR of 8-10pt body text, which
# is what insurance tables on a face sheet are set in. Below ~200 DPI, PP-OCR's
# recognition accuracy on small print falls off a cliff.
DEFAULT_DPI = 300
PDF_BASE_DPI = 72  # PDF user-space unit; also used by app/ocr/pdf_text.py to
# scale embedded-text boxes into this same rendered-pixel space.

# Memory bound on the OCR path only, same reasoning as the DPI comment above --
# not a blanket quality downscale. The phone-photo attachments in
# sample/ANALYSIS.md are already ~2448x3264, right at the 300-DPI-equivalent
# floor PP-OCR needs for small print; shrinking every image on principle would
# cost the accuracy this effort exists to improve. This only clips a photo
# that's unusually large (a panorama, an oversized flatbed scan) before it
# decodes to an unbounded amount of RGB.
MAX_OCR_DIMENSION = 4000


def rasterize(data: bytes, content_type: str, *, dpi: int = DEFAULT_DPI) -> list[bytes]:
    """Return one PNG per page, for the OCR engine. Raises `OcrError` on
    anything unreadable."""
    if content_type == "application/pdf":
        return _rasterize_pdf(data, dpi=dpi)
    return [_normalize_image(data, cap_dimension=MAX_OCR_DIMENSION)]


def render_page(data: bytes, content_type: str, *, page: int, dpi: int = DEFAULT_DPI) -> bytes:
    """Render exactly one page (0-based) to PNG -- for the review UI's overlay,
    which needs the same raster the OCR boxes were measured in but only one page
    at a time. Rendering all 50 pages to show one is the memory bound this avoids.
    `page` must be 0 for a single-frame image.

    Never capped: a reviewer looks at this image directly, and `rasterize`'s
    OCR-only size cap has no place degrading what a human sees.
    """
    if content_type == "application/pdf":
        return _render_pdf_page(data, page=page, dpi=dpi)
    if page != 0:
        raise OcrError("page out of range")
    return _normalize_image(data)


def _page_to_png(page: object, scale: float) -> bytes:
    bitmap = page.render(scale=scale)  # type: ignore[attr-defined]
    buffer = io.BytesIO()
    bitmap.to_pil().convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def _render_pdf_page(data: bytes, *, page: int, dpi: int) -> bytes:
    import pypdfium2 as pdfium

    scale = dpi / PDF_BASE_DPI
    try:
        document = pdfium.PdfDocument(data)
    except Exception as exc:
        logger.warning("pdf_rasterization_failed", page_index=page)
        raise OcrError("could not render PDF page") from exc
    try:
        if page < 0 or page >= len(document):
            raise OcrError("page out of range")
        pdf_page = document[page]
        try:
            return _page_to_png(pdf_page, scale)
        finally:
            pdf_page.close()
    except OcrError:
        raise
    except Exception as exc:
        logger.warning("pdf_rasterization_failed", page_index=page)
        raise OcrError("could not render PDF page") from exc
    finally:
        document.close()


def _rasterize_pdf(data: bytes, *, dpi: int) -> list[bytes]:
    import pypdfium2 as pdfium

    scale = dpi / PDF_BASE_DPI
    pages: list[bytes] = []
    try:
        # Construction inside the guard: a file that passed the magic-byte check
        # but PDFium still rejects raises PdfiumError here, which must surface as
        # OcrError like every other unreadable-input path.
        document = pdfium.PdfDocument(data)
    except Exception as exc:
        logger.warning("pdf_rasterization_failed", page_index=0)
        raise OcrError("could not render PDF page") from exc
    try:
        for index in range(len(document)):
            page = document[index]
            try:
                pages.append(_page_to_png(page, scale))
            finally:
                page.close()
    except Exception as exc:
        # The page count already passed pypdf. A failure here means PDFium and
        # pypdf disagree about the file, which is itself suspicious.
        logger.warning("pdf_rasterization_failed", page_index=len(pages))
        raise OcrError("could not render PDF page") from exc
    finally:
        document.close()

    if not pages:
        raise OcrError("PDF contains no renderable pages")
    return pages


def _normalize_image(data: bytes, *, cap_dimension: int | None = None) -> bytes:
    """Re-encode any accepted image as RGB PNG.

    Multi-page TIFFs collapse to their first frame. The pilot accepts TIFF
    because the proposal lists it; a fax-derived multi-page TIFF would need the
    same page loop as a PDF, and no sample has one yet.

    `cap_dimension` shrinks the image, preserving aspect ratio, only if its
    longest side exceeds it -- a no-op for every sample in this corpus, a
    backstop for whatever arrives that isn't.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            rgb = image.convert("RGB")
            if cap_dimension and max(rgb.size) > cap_dimension:
                rgb.thumbnail((cap_dimension, cap_dimension), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            rgb.save(buffer, format="PNG")
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise OcrError("could not decode image") from exc
