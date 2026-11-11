"""Builds a local OCR cache from every document under sample/p*/, for the
prompt eval harness. Mirrors PipelineService._run_ocr
(app/services/pipeline_service.py) minus the database and storage layers --
read bytes straight off disk, try the embedded text layer first, fall back to
rasterize + the real OCR engine. Written once; run.py never re-OCRs, since
OCR output does not change when a prompt does.

Run inside a container built from this repo's Dockerfile (has PaddleOCR):

    docker compose exec worker python -m scripts.prompt_eval.build_cache
    docker compose exec worker python -m scripts.prompt_eval.build_cache --force  # rebuild all
"""

import argparse
import json

from app.ocr import pdf_text, rasterize
from app.ocr.base import OcrEngine, OcrResult
from app.services.file_validation import UnsupportedFileTypeError, sniff_type
from scripts.prompt_eval._common import OCR_CACHE_DIR, iter_sample_documents


def _run_ocr(data: bytes, content_type: str, engine: OcrEngine) -> OcrResult:
    """Same fallback order as PipelineService._run_ocr: trust a PDF's
    embedded text layer before ever rasterizing and calling the engine."""
    result: OcrResult | None = None
    if content_type == "application/pdf":
        result = pdf_text.extract_text_layer(data, dpi=rasterize.DEFAULT_DPI)
    if result is None:
        page_images = rasterize.rasterize(data, content_type, dpi=rasterize.DEFAULT_DPI)
        result = engine.recognize(page_images)
    return result


def main(*, force: bool) -> None:
    from app.api.deps import get_ocr_engine

    engine = get_ocr_engine()
    built = skipped = 0
    for encounter, path in iter_sample_documents():
        out_path = OCR_CACHE_DIR / encounter / f"{path.stem}.json"
        if out_path.exists() and not force:
            skipped += 1
            continue

        data = path.read_bytes()
        try:
            content_type = sniff_type(data[:8]).value
        except UnsupportedFileTypeError:
            print(f"skip (unrecognized file type): {encounter}/{path.name}")
            continue

        result = _run_ocr(data, content_type, engine)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {
                    "source_file": path.name,
                    "engine_version": result.engine_version,
                    "raw_text": result.raw_text,
                    "page_texts": [
                        {
                            "page": page.page_number,
                            "lines": [line.as_dict() for line in page.lines],
                        }
                        for page in result.pages
                    ],
                }
            )
        )
        built += 1
        print(
            f"cached: {encounter}/{path.name} ({len(result.pages)} pages, {result.engine_version})"
        )

    print(f"\n{built} built, {skipped} already cached (--force to rebuild)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="rebuild every doc, even if cached")
    args = parser.parse_args()
    main(force=args.force)
