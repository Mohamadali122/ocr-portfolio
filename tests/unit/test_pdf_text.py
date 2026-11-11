from app.ocr.pdf_text import extract_text_layer
from tests.factories import make_pdf, make_pdf_with_text

_REAL_DOC_TEXT = "PATIENT CARE REPORT DOE JANE DOB 04/17/1958 " * 10  # clears MIN_CHARS_PER_PAGE


class TestExtractTextLayer:
    def test_a_real_text_layer_is_returned(self) -> None:
        result = extract_text_layer(make_pdf_with_text(_REAL_DOC_TEXT))
        assert result is not None
        assert "PATIENT CARE REPORT DOE JANE" in result.raw_text
        assert result.engine_version == "pdf-text-layer/pdfium"

    def test_lines_are_exact_confidence(self) -> None:
        result = extract_text_layer(make_pdf_with_text(_REAL_DOC_TEXT))
        assert result is not None
        assert all(line.confidence == 1.0 for page in result.pages for line in page.lines)

    def test_a_blank_page_pdf_has_no_layer(self) -> None:
        # make_pdf() writes genuinely blank pages -- no text at all, the
        # scanned-PDF shape.
        assert extract_text_layer(make_pdf(1)) is None

    def test_a_too_thin_layer_is_rejected(self) -> None:
        # sample/near_blank_booking_1.pdf: a real near-blank booking-summary print,
        # just a header/footer under the MIN_CHARS_PER_PAGE floor.
        result = extract_text_layer(make_pdf_with_text("BookingSummary 1/1"))
        assert result is None

    def test_an_unparseable_pdf_returns_none_rather_than_raising(self) -> None:
        assert extract_text_layer(b"%PDF-1.4\nnot a real pdf") is None

    def test_box_scales_with_dpi_to_match_the_rendered_page(self) -> None:
        # PDF_BASE_DPI is 72; requesting 144 must double the box coordinates so
        # they land in the same pixel space render_page() would produce at
        # that DPI -- the review overlay's whole basis for correctness.
        text = "PATIENT CARE REPORT " * 20  # clear MIN_CHARS_PER_PAGE
        low = extract_text_layer(make_pdf_with_text(text), dpi=72)
        high = extract_text_layer(make_pdf_with_text(text), dpi=144)
        assert low is not None and high is not None
        low_box = low.pages[0].lines[0].box
        high_box = high.pages[0].lines[0].box
        assert high_box[2][0] == low_box[2][0] * 2
        assert high_box[2][1] == low_box[2][1] * 2
