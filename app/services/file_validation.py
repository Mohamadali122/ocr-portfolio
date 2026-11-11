"""Upload validation.

The declared `Content-Type` is a claim by the client and is never trusted. Type
is decided by inspecting the leading bytes, and the declared value is only
checked for consistency afterwards.

Everything downstream -- PaddleOCR, pypdfium, PIL -- is a native-code parser
being handed attacker-controlled input. Cheap checks here are the difference
between rejecting a malformed file and handing it to a C++ decoder.
"""

from enum import StrEnum

from app.core.exceptions import AppError

# 8 bytes is enough to discriminate every format we accept.
SNIFF_BYTES = 8


class DetectedType(StrEnum):
    PDF = "application/pdf"
    PNG = "image/png"
    JPEG = "image/jpeg"
    TIFF = "image/tiff"


class UnsupportedFileTypeError(AppError):
    code = "unsupported_file_type"
    status_code = 415
    message = "Only PDF, PNG, JPEG, and TIFF files are accepted."


class FileTooLargeError(AppError):
    code = "file_too_large"
    status_code = 413
    message = "The uploaded file exceeds the maximum permitted size."


class TooManyPagesError(AppError):
    code = "too_many_pages"
    status_code = 422
    message = "The uploaded PDF exceeds the maximum permitted page count."


class CorruptFileError(AppError):
    code = "corrupt_file"
    status_code = 422
    message = "The uploaded file could not be read."


_SIGNATURES: tuple[tuple[bytes, DetectedType], ...] = (
    (b"%PDF-", DetectedType.PDF),
    (b"\x89PNG\r\n\x1a\n", DetectedType.PNG),
    (b"\xff\xd8\xff", DetectedType.JPEG),
    (b"II*\x00", DetectedType.TIFF),  # little-endian
    (b"MM\x00*", DetectedType.TIFF),  # big-endian
)


def sniff_type(head: bytes) -> DetectedType:
    """Identify by magic bytes. Raises `UnsupportedFileTypeError`."""
    for signature, detected in _SIGNATURES:
        if head.startswith(signature):
            return detected
    raise UnsupportedFileTypeError()


def assert_declared_type_matches(declared: str | None, detected: DetectedType) -> None:
    """A mismatch means the client lied, or the browser guessed. Either way, stop.

    Browsers routinely send `application/octet-stream`, which is an absence of a
    claim rather than a false one, so it is permitted.
    """
    if not declared or declared == "application/octet-stream":
        return
    if declared.split(";")[0].strip().lower() != detected.value:
        raise UnsupportedFileTypeError(
            "The declared content type does not match the file's contents."
        )


def count_pdf_pages(data: bytes) -> int:
    """Page count without rendering anything.

    Imported lazily: pypdf is only needed for PDFs, and keeping it out of module
    import time means an image upload never touches it.
    """
    import io

    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        return len(PdfReader(io.BytesIO(data), strict=False).pages)
    # PyPdfError is the base: PdfReadError, PdfStreamError, and friends. The
    # non-pypdf exceptions come from the stdlib parsing primitives underneath it.
    except (PyPdfError, ValueError, OSError, RecursionError) as exc:
        raise CorruptFileError() from exc
