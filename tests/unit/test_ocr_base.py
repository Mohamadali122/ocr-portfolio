import pytest

from app.ocr.base import OcrEngine, OcrLine, OcrPage, OcrResult
from tests.fakes import ExplodingOcrEngine, FakeOcrEngine

BOX = ((0.0, 0.0), (10.0, 0.0), (10.0, 5.0), (0.0, 5.0))


def _page(number: int, *confidences: float) -> OcrPage:
    return OcrPage(
        page_number=number,
        lines=[OcrLine(text=f"line{i}", confidence=c, box=BOX) for i, c in enumerate(confidences)],
    )


class TestProtocolConformance:
    @pytest.mark.parametrize("engine", [FakeOcrEngine(), ExplodingOcrEngine()])
    def test_fakes_satisfy_the_protocol(self, engine: object) -> None:
        assert isinstance(engine, OcrEngine)


class TestConfidence:
    def test_empty_page_is_zero_confidence_not_perfect(self) -> None:
        # Returning 1.0 for a page with no text would mark a blank or failed scan
        # as clean, which is the worst possible default on a billing document.
        assert OcrPage(page_number=1, lines=[]).mean_confidence == 0.0

    def test_result_with_no_lines_is_zero_confidence(self) -> None:
        result = OcrResult(pages=[OcrPage(page_number=1, lines=[])], engine_version="x")
        assert result.mean_confidence == 0.0

    def test_confidence_is_weighted_by_line_not_by_page(self) -> None:
        # A 1-line cover page at 0.5 and a 9-line face sheet at 1.0.
        # Averaging pages gives 0.75. Averaging lines gives 0.95.
        # The face sheet is the document; it must dominate.
        result = OcrResult(
            pages=[_page(1, 0.5), _page(2, *([1.0] * 9))],
            engine_version="x",
        )
        assert result.mean_confidence == pytest.approx(0.95)


class TestText:
    def test_page_text_joins_lines_with_newlines(self) -> None:
        assert _page(1, 0.9, 0.9).text == "line0\nline1"

    def test_raw_text_separates_pages_with_a_blank_line(self) -> None:
        result = OcrResult(pages=[_page(1, 0.9), _page(2, 0.9)], engine_version="x")
        assert result.raw_text == "line0\n\nline0"

    def test_fake_engine_returns_one_page_per_image(self) -> None:
        result = FakeOcrEngine().recognize([b"png1", b"png2", b"png3"])
        assert [p.page_number for p in result.pages] == [1, 2, 3]
        assert result.engine_version == "fake-ocr/1.0"
