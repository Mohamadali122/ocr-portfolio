"""The OCR seam.

`services/` imports this file. It must never import `paddle_engine.py`. Swapping
PP-OCRv4 for v5, or for Tesseract, is a new adapter and one line in
`api/deps.py` -- not a change to any use-case.

The engine receives PNG bytes, not a path and not a numpy array. Bytes keep the
rasterizer independent of whatever the engine wants internally, and make the
protocol testable with a fake that never imports paddle.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# (x, y) corners, clockwise from top-left, in pixels of the rendered page.
BoundingBox = tuple[tuple[float, float], ...]


class OcrError(Exception):
    """The engine could not process the page."""


@dataclass(frozen=True, slots=True)
class OcrLine:
    text: str
    confidence: float
    box: BoundingBox

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "confidence": self.confidence, "box": self.box}


@dataclass(frozen=True, slots=True)
class OcrPage:
    page_number: int  # 1-based, matching what a human sees
    lines: list[OcrLine] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def mean_confidence(self) -> float:
        # A page with no recognised text is not high-confidence emptiness; it is
        # zero-confidence. Returning 1.0 here would mark a blank scan as clean.
        if not self.lines:
            return 0.0
        return sum(line.confidence for line in self.lines) / len(self.lines)


@dataclass(frozen=True, slots=True)
class OcrResult:
    pages: list[OcrPage]
    engine_version: str

    @property
    def raw_text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)

    @property
    def mean_confidence(self) -> float:
        """Weighted by line count, not by page.

        A one-line cover page must not drag the average as hard as a dense face
        sheet. Averaging page means would let it.
        """
        lines = [line for page in self.pages for line in page.lines]
        if not lines:
            return 0.0
        return sum(line.confidence for line in lines) / len(lines)


@runtime_checkable
class OcrEngine(Protocol):
    def recognize(self, page_images: list[bytes]) -> OcrResult:
        """Read text from PNG-encoded page images, in order. Raises `OcrError`."""
        ...

    @property
    def version(self) -> str:
        """Recorded on every result, so a re-run can be compared to an old one."""
        ...
