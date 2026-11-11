import pytest

from app.services.file_validation import (
    CorruptFileError,
    DetectedType,
    UnsupportedFileTypeError,
    assert_declared_type_matches,
    count_pdf_pages,
    sniff_type,
)
from tests.factories import make_pdf


class TestSniffing:
    @pytest.mark.parametrize(
        ("head", "expected"),
        [
            (b"%PDF-1.7", DetectedType.PDF),
            (b"\x89PNG\r\n\x1a\n", DetectedType.PNG),
            (b"\xff\xd8\xff\xe0", DetectedType.JPEG),
            (b"II*\x00abcd", DetectedType.TIFF),
            (b"MM\x00*abcd", DetectedType.TIFF),
        ],
    )
    def test_recognised_formats(self, head: bytes, expected: DetectedType) -> None:
        assert sniff_type(head) is expected

    @pytest.mark.parametrize(
        "head",
        [
            b"PK\x03\x04",  # zip / docx
            b"<?xml ver",
            b"#!/bin/sh",
            b"\x7fELF\x02\x01",
            b"",
        ],
    )
    def test_everything_else_is_refused(self, head: bytes) -> None:
        with pytest.raises(UnsupportedFileTypeError):
            sniff_type(head)

    def test_an_executable_renamed_to_pdf_is_still_refused(self) -> None:
        # The extension and the declared type are both attacker-controlled. Only
        # the bytes are not.
        with pytest.raises(UnsupportedFileTypeError):
            sniff_type(b"MZ\x90\x00")


class TestDeclaredType:
    def test_matching_declaration_passes(self) -> None:
        assert_declared_type_matches("application/pdf", DetectedType.PDF)

    def test_declaration_with_charset_parameter_passes(self) -> None:
        assert_declared_type_matches("image/png; charset=binary", DetectedType.PNG)

    def test_absent_declaration_passes(self) -> None:
        # An absent claim is not a false one. Browsers routinely send this.
        assert_declared_type_matches("application/octet-stream", DetectedType.PDF)
        assert_declared_type_matches(None, DetectedType.PDF)

    def test_a_lie_is_refused(self) -> None:
        with pytest.raises(UnsupportedFileTypeError):
            assert_declared_type_matches("image/png", DetectedType.PDF)


class TestPageCount:
    @pytest.mark.parametrize("pages", [1, 3, 12])
    def test_counts_pages(self, pages: int) -> None:
        assert count_pdf_pages(make_pdf(pages)) == pages

    def test_garbage_claiming_to_be_a_pdf_raises_corrupt_file(self) -> None:
        # It passed the magic-byte check and is still not a PDF. This must fail
        # before pypdfium or PaddleOCR sees it.
        with pytest.raises(CorruptFileError):
            count_pdf_pages(b"%PDF-1.4\nnot actually a pdf")

    def test_truncated_pdf_raises_corrupt_file(self) -> None:
        with pytest.raises(CorruptFileError):
            count_pdf_pages(make_pdf(1)[:120])
