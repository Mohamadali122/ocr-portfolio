"""Best-effort document-type classification from OCR/embedded text.

`doc_type` is a caller-supplied upload form field (`app/api/v1/documents.py`)
that defaults to `DocumentType.UNKNOWN`, and nothing upstream of this pipeline
verifies it -- `sample/ANALYSIS.md` found nothing in a real upload flow would
reliably populate it per-file. `classify()` is the fallback `PipelineService`
runs when the stored type is `UNKNOWN`, so an extraction at least gets the
right prompt hint (`extraction_service._DOC_TYPE_HINTS`) and the right
required-field set (`extraction_validation._REQUIRED_FIELDS`) instead of the
generic one.

Keyword rules only, not a model: a handful of characteristic phrases is all
the confidence this pipeline can actually claim, and it's a pure function of
text the same way `OcrEngine`/`LlmProvider` are pure functions of bytes --
testable with a string literal, no fixture.
"""

import re

from app.models.document import DocumentType

# Order matters: an ePCR's own "Patient Insurance" section is often blank
# (extraction_service._DOC_TYPE_HINTS), so a face-sheet-shaped word can appear
# inside an otherwise-EPCR document. Check the most distinctive markers --
# a literal report title, a literal product URL -- before the broader ones.
_EPCR_RE = re.compile(r"patient care report|\bpcr#|\bepcr\b", re.IGNORECASE)
_BOOKING_RE = re.compile(r"booking\s*summary|transport\s+details|transport\.net", re.IGNORECASE)
_PCS_RE = re.compile(r"physician certification statement|medical necessity", re.IGNORECASE)
_HFS_RE = re.compile(r"face\s*sheet|guarantor|next of kin|admission type", re.IGNORECASE)


def classify(raw_text: str) -> DocumentType:
    """Return the best-guess type from recognized text, or `UNKNOWN` if no
    rule matches -- the same fallback the caller already had."""
    if _EPCR_RE.search(raw_text):
        return DocumentType.EPCR
    if _BOOKING_RE.search(raw_text):
        return DocumentType.BOOKING_SUMMARY
    if _PCS_RE.search(raw_text):
        return DocumentType.PCS
    if _HFS_RE.search(raw_text):
        return DocumentType.HFS
    return DocumentType.UNKNOWN
