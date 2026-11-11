"""Embedded-text extraction for digital PDFs.

Rasterizing and Paddle-OCR-ing a page is the wrong tool when the PDF already
carries a real text layer: pypdfium2 reads it byte-exact, in milliseconds,
with none of OCR's character-level noise. `sample/ANALYSIS.md` found this is
most of the corpus this pipeline actually sees -- every `auto_generated_epcr_*`
PDF, and the booking-summary and insurance-eligibility prints, are digital,
not scanned.

`extract_text_layer` returns an `OcrResult` built from that layer, or `None`
when there isn't one worth trusting -- a genuinely scanned PDF has no text
layer at all, and some digital prints have one that's real but too thin
(`sample/near_blank_booking_1.pdf`: a booking-summary print with only a page
header/footer, 89 characters across the whole document). Both cases fall
through to the normal rasterize-then-OCR path in `PipelineService._run_ocr`.
"""

from app.ocr.base import OcrLine, OcrPage, OcrResult
from app.ocr.rasterize import DEFAULT_DPI, PDF_BASE_DPI

ENGINE_VERSION = "pdf-text-layer/pdfium"

# Below this many characters per page (averaged over the whole document), the
# layer is either absent or too thin to be the sole source -- see
# sample/near_blank_booking_1.pdf (89 chars/1 page) and _2.pdf (89 chars/page)
# versus a real ePCR (1600-2300 chars/page) or the BCBS eligibility print
# (2535 chars/page). Chosen from the corpus that motivated this, not a
# universal constant -- revisit against Phase 8's numbers if it misfires.
MIN_CHARS_PER_PAGE = 200


def extract_text_layer(data: bytes, *, dpi: int = DEFAULT_DPI) -> OcrResult | None:
    """Return an `OcrResult` built from `data`'s embedded PDF text layer, or
    `None` if it has none, isn't a PDF PDFium can open, or is too thin to trust.

    `dpi` must match whatever DPI the review UI's page overlay renders at
    (`rasterize.render_page`, `settings.ocr_dpi`): `source_bbox` is measured in
    that pixel space, and a mismatch would make the overlay drift off the text.
    """
    import pypdfium2 as pdfium

    scale = dpi / PDF_BASE_DPI
    try:
        document = pdfium.PdfDocument(data)
    except Exception:
        return None

    try:
        pages: list[OcrPage] = []
        total_chars = 0
        for index in range(len(document)):
            page = document[index]
            try:
                width, height = page.get_size()
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range()
                finally:
                    textpage.close()
            finally:
                page.close()
            lines = _lines_from_text(text, width * scale, height * scale)
            total_chars += sum(len(line.text) for line in lines)
            pages.append(OcrPage(page_number=index + 1, lines=lines))
    except Exception:
        return None
    finally:
        document.close()

    if not pages or total_chars < MIN_CHARS_PER_PAGE * len(pages):
        return None
    return OcrResult(pages=pages, engine_version=ENGINE_VERSION)


def _lines_from_text(text: str, width: float, height: float) -> list[OcrLine]:
    """One `OcrLine` per non-blank source line, confidence 1.0 -- it's exact
    machine text, not a recognition guess.

    ponytail: box is the full page, not the line's actual position. Getting a
    per-line rect out of pypdfium2's char-box API is real work for a value this
    pipeline doesn't need yet: the text is already proven exact, so the
    hallucination guard and a reviewer both have less reason to want a tight
    box than they do for an OCR guess. Narrow it if a reviewer actually asks
    where on the page a value came from for one of these documents.
    """
    box = ((0.0, 0.0), (width, 0.0), (width, height), (0.0, height))
    return [
        OcrLine(text=line, confidence=1.0, box=box) for line in text.splitlines() if line.strip()
    ]
