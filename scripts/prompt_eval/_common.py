"""Shared paths and helpers for the prompt-correctness eval harness.

Distinct from scripts/eval_samples.py (the Phase 8 completeness/regression
harness, which drives the real HTTP API end-to-end and reports aggregate
non-PHI metrics only). This harness answers a narrower question -- is the
LLM extraction PROMPT itself correct, field by field, against a hand-checked
answer key -- which needs the actual extracted values to score, not just
counts. See README.md in this directory for the full workflow and the PHI
handling rules that follow from that difference.

Everything this package reads or writes under SAMPLE_DIR is PHI. `sample/`
is gitignored wholesale (see .gitignore); nothing here writes outside it.
"""

from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SAMPLE_DIR = REPO_ROOT / "sample"
EVAL_DIR = SAMPLE_DIR / "eval"
OCR_CACHE_DIR = EVAL_DIR / "ocr_cache"
ANSWER_KEY_DIR = EVAL_DIR / "answer_key"
RAW_RESPONSES_DIR = EVAL_DIR / "raw_responses"
REPORTS_DIR = EVAL_DIR / "reports"

_DOCUMENT_SUFFIXES = {".pdf", ".jpeg", ".jpg", ".png", ".tif", ".tiff"}


def iter_sample_documents() -> Iterator[tuple[str, Path]]:
    """Yield (encounter, path) for every source document directly under
    sample/p*/ -- skips sample/eval/ (this harness's own output) and
    anything without a document extension (e.g. ANALYSIS.md)."""
    for encounter_dir in sorted(SAMPLE_DIR.glob("p*")):
        if not encounter_dir.is_dir():
            continue
        for path in sorted(encounter_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in _DOCUMENT_SUFFIXES:
                yield encounter_dir.name, path
