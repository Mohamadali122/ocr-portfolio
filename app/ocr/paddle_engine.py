"""PaddleOCR (PP-OCRv5, paddleocr 3.x) adapter. Implements `OcrEngine`.

Two properties of the underlying library shape this file.

The predictor is expensive to construct -- it loads several models -- so it is
built once, lazily, on first use rather than at import. Building it at import
would make `alembic upgrade head` and `pytest` pay for it. `numpy` is part of
the same `[ocr]` extra as paddleocr/opencv (see `pyproject.toml`) and is
imported lazily for the same reason -- `from __future__ import annotations`
defers the `np.ndarray` type hints below to strings so the module itself
still imports without it; only calling `_decode`/`_parse` needs it installed.

The predictor is not thread-safe. FastAPI runs `def` endpoints and
BackgroundTasks in a threadpool, so concurrent uploads would enter `predict()` on
the same C++ predictor from two threads. The lock serialises them. On a CPU-only
host this costs nothing that matters: inference is already the bottleneck, and
paddle saturates the cores internally.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from app.core.logging import get_logger
from app.ocr.base import OcrError, OcrLine, OcrPage, OcrResult

if TYPE_CHECKING:
    import numpy as np

logger = get_logger(__name__)

_ENGINE_VERSION = "paddleocr-3.1/PP-OCRv5"


class PaddleOcrEngine:
    """Implements `OcrEngine`."""

    def __init__(self, *, lang: str = "en", use_textline_orientation: bool = True) -> None:
        self._lang = lang
        self._use_textline_orientation = use_textline_orientation
        self._predictor: Any | None = None
        self._lock = threading.Lock()

    @property
    def version(self) -> str:
        return _ENGINE_VERSION

    def _get_predictor(self) -> Any:
        # Double-checked: the fast path takes no lock once warm.
        if self._predictor is None:
            with self._lock:
                if self._predictor is None:
                    from paddleocr import PaddleOCR

                    logger.info("ocr_model_loading", lang=self._lang)
                    # 3.x renamed use_angle_cls -> use_textline_orientation and
                    # dropped show_log (logging is configured globally now).
                    self._predictor = PaddleOCR(
                        use_textline_orientation=self._use_textline_orientation,
                        lang=self._lang,
                    )
                    logger.info("ocr_model_loaded")
        return self._predictor

    def recognize(self, page_images: list[bytes]) -> OcrResult:
        predictor = self._get_predictor()
        pages: list[OcrPage] = []

        for index, png in enumerate(page_images, start=1):
            array = self._decode(png)
            with self._lock:  # the predictor is not re-entrant across threads
                try:
                    raw = predictor.predict(array)
                except Exception as exc:
                    # Never log the page bytes or any recognised text: it is PHI.
                    # exc_info=True is safe here -- the traceback is Paddle's own
                    # internals (shapes, model names), not page content -- and is
                    # the only way to see *why* a page failed instead of just that
                    # it did.
                    logger.error(
                        "ocr_page_failed",
                        page_number=index,
                        error_type=type(exc).__name__,
                        exc_info=True,
                    )
                    raise OcrError(f"OCR failed on page {index}") from exc
            pages.append(OcrPage(page_number=index, lines=self._parse(raw)))

        return OcrResult(pages=pages, engine_version=self.version)

    @staticmethod
    def _decode(png: bytes) -> np.ndarray:
        import cv2
        import numpy as np

        array = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
        if array is None:
            raise OcrError("could not decode page image")
        return array

    @staticmethod
    def _parse(raw: Any) -> list[OcrLine]:
        """Flatten paddleocr 3.x's result into lines.

        `predict(array)` returns `[OCRResult]` (one dict-like per image). The
        parallel lists we need:
            rec_texts   list[str]
            rec_scores  list[float]
            rec_polys   list of 4-point (x, y) boxes  (falls back to dt_polys)
        A page with no detected text yields empty lists, not None.
        """
        import numpy as np

        if not raw:
            return []
        page = raw[0]

        texts = page.get("rec_texts") or []
        scores = page.get("rec_scores") or []
        polys = page.get("rec_polys")
        if polys is None:
            polys = page.get("dt_polys") or []

        lines: list[OcrLine] = []
        for text, score, poly in zip(texts, scores, polys, strict=False):
            if not text:
                continue
            lines.append(
                OcrLine(
                    text=text,
                    confidence=float(score),
                    box=tuple((float(x), float(y)) for x, y in np.asarray(poly).tolist()),
                )
            )
        return lines
