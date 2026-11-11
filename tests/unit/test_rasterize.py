import io

import pytest

from app.ocr.base import OcrError
from app.ocr.rasterize import rasterize, render_page
from tests.factories import make_jpeg, make_pdf, make_png

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class TestPdf:
    @pytest.mark.parametrize("pages", [1, 3])
    def test_one_png_per_page(self, pages: int) -> None:
        images = rasterize(make_pdf(pages), "application/pdf", dpi=72)
        assert len(images) == pages
        assert all(image.startswith(PNG_MAGIC) for image in images)

    def test_dpi_scales_the_rendered_pixels(self) -> None:
        # 150 DPI is 2x the linear scale of 75, so ~4x the pixels and a
        # substantially larger PNG. This is the memory bound made visible.
        small = rasterize(make_pdf(1), "application/pdf", dpi=75)[0]
        large = rasterize(make_pdf(1), "application/pdf", dpi=150)[0]
        assert len(large) > len(small)

    def test_unrenderable_pdf_raises_ocr_error(self) -> None:
        # Passed the magic-byte check, and PDFium still cannot render it.
        with pytest.raises(OcrError):
            rasterize(b"%PDF-1.4\nnot a real pdf", "application/pdf")


class TestImages:
    @pytest.mark.parametrize(
        ("data", "content_type"),
        [(make_png(4, 4), "image/png"), (make_jpeg(), "image/jpeg")],
    )
    def test_images_are_normalized_to_a_single_png(self, data: bytes, content_type: str) -> None:
        images = rasterize(data, content_type)
        assert len(images) == 1
        assert images[0].startswith(PNG_MAGIC)

    def test_undecodable_image_raises_ocr_error(self) -> None:
        with pytest.raises(OcrError):
            rasterize(b"\x89PNG\r\n\x1a\ntruncated", "image/png")

    def test_an_oversized_image_is_capped_for_ocr(self) -> None:
        from PIL import Image

        big = make_jpeg(5000, 100)  # longest side well past MAX_OCR_DIMENSION
        [capped] = rasterize(big, "image/jpeg")
        with Image.open(io.BytesIO(capped)) as image:
            assert max(image.size) <= 4000

    def test_render_page_never_caps_the_reviewer_facing_image(self) -> None:
        from PIL import Image

        big = make_jpeg(5000, 100)
        uncapped = render_page(big, "image/jpeg", page=0)
        with Image.open(io.BytesIO(uncapped)) as image:
            assert max(image.size) == 5000


class TestRenderPage:
    def test_renders_a_single_requested_pdf_page(self) -> None:
        png = render_page(make_pdf(3), "application/pdf", page=1, dpi=72)
        assert png.startswith(PNG_MAGIC)

    def test_pdf_page_out_of_range_raises(self) -> None:
        with pytest.raises(OcrError):
            render_page(make_pdf(2), "application/pdf", page=5, dpi=72)

    def test_image_returns_its_only_page(self) -> None:
        assert render_page(make_png(4, 4), "image/png", page=0).startswith(PNG_MAGIC)

    def test_image_page_other_than_zero_raises(self) -> None:
        with pytest.raises(OcrError):
            render_page(make_jpeg(), "image/jpeg", page=1)
