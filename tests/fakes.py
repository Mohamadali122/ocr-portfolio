"""Test doubles for the protocols.

That a fake engine is ~20 lines, imports no paddle, and satisfies `OcrEngine`
via `isinstance` is the payoff of the protocol seam. The pipeline is testable in
milliseconds; a real PP-OCR run is seconds per page.
"""

from app.llm.base import LlmError, LlmTransientError
from app.ocr.base import OcrError, OcrLine, OcrPage, OcrResult


class FakeOcrEngine:
    """Implements `OcrEngine`. Returns one line per page image."""

    def __init__(self, *, text: str = "PATIENT NAME DOE JANE", confidence: float = 0.95) -> None:
        self._text = text
        self._confidence = confidence
        self.calls = 0

    @property
    def version(self) -> str:
        return "fake-ocr/1.0"

    def recognize(self, page_images: list[bytes]) -> OcrResult:
        self.calls += 1
        pages = [
            OcrPage(
                page_number=index,
                lines=[
                    OcrLine(
                        text=self._text,
                        confidence=self._confidence,
                        box=((0.0, 0.0), (100.0, 0.0), (100.0, 20.0), (0.0, 20.0)),
                    )
                ],
            )
            for index, _ in enumerate(page_images, start=1)
        ]
        return OcrResult(pages=pages, engine_version=self.version)


class ExplodingOcrEngine:
    """Implements `OcrEngine`. Always fails, the way a corrupt scan does."""

    @property
    def version(self) -> str:
        return "exploding-ocr/1.0"

    def recognize(self, page_images: list[bytes]) -> OcrResult:
        raise OcrError("simulated engine failure")


class FakeLlmProvider:
    """Implements `LlmProvider`. Returns a canned JSON response.

    The default `"{}"` validates cleanly against `RawPatientExtraction` --
    every field has a default -- so tests that don't care about extraction
    content can use this fake and still reach a COMPLETED document.
    """

    def __init__(self, *, response: str = "{}") -> None:
        self._response = response
        self.calls = 0

    @property
    def version(self) -> str:
        return "fake-llm/1.0"

    def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str:
        self.calls += 1
        return self._response


class ExplodingLlmProvider:
    """Implements `LlmProvider`. Always fails, the way an unreachable LM Studio does."""

    @property
    def version(self) -> str:
        return "exploding-llm/1.0"

    def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str:
        raise LlmError("simulated provider failure")


class TransientlyFailingLlmProvider:
    """Implements `LlmProvider`. Raises `LlmTransientError` -- a dropped
    connection or LM Studio restarting -- the failure mode Celery's bounded
    retry (`app/worker.py`) exists to ride out, distinct from `ExplodingLlmProvider`'s
    permanent `LlmError`."""

    @property
    def version(self) -> str:
        return "transiently-failing-llm/1.0"

    def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str:
        raise LlmTransientError("simulated transport failure")
