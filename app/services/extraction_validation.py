"""The extraction completeness gate.

An LLM response that validates cleanly and grounds to nothing is not a
successful extraction -- it is the empty-JSON case `FakeLlmProvider`'s
default `"{}"` exists to exercise in tests, and the real case
`sample/near_blank_booking_1.pdf` produces (a near-blank booking-summary print,
see `sample/ANALYSIS.md`). Before this gate, `PipelineService` could not tell
the two apart: both reached `COMPLETED` and both showed up in Review.
`evaluate()` is the check that lets it, run once per document after
`ExtractionService.extract` and before the document is marked `COMPLETED`.

Required fields are checked for presence *and* confidence, not presence
alone: `normalization.normalize` can fill a required field's `typed_value`
from an SOP default (the DOB `01/01/1900` fallback, the `UNKNOWN` name alias)
when nothing was actually found on the page. Those carry the original
grounded field's confidence forward -- 0.0, since there was nothing to
ground -- so a document that only cleared the presence check via SOP filler
still fails the confidence one. Two things keep this check narrow on purpose:
only the required fields' confidence counts, not every field's, so an
unrelated optional field's SOP default (e.g. `address_line1`'s CBO fallback
on a document that never mentions an address) can't drag down a perfectly
good extraction; and it gates on the *minimum* confidence among required
fields, not their mean, so one fabricated required field can't hide behind
two or three well-grounded ones.

Not a repeat of the OCR-confidence philosophy this pipeline already settled
(`ocr_low_confidence_threshold` is surfaced to a reviewer, never used to
reject a scan -- a bad scan is still the only scan there is): this gate asks
"did extraction produce anything to review," not "is the scan good enough."
A low-confidence OCR result can still pass this gate and reach a reviewer,
exactly as today.
"""

from dataclasses import dataclass

from app.models.document import DocumentType
from app.schemas.extraction import FieldSource, PatientExtraction

# Picked from what each type's prompt hint (extraction_service._DOC_TYPE_HINTS)
# already promises it carries -- not an exhaustive schema, just what makes a
# document worth a reviewer's time. Insurance fields are deliberately excluded:
# an EPCR's own insurance section is routinely blank by design (the crew
# expects a face sheet to carry it), so requiring it would fail the majority
# of otherwise-good EPCR extractions.
# `_ground_field` only ever emits confidence 0.0 (absent/SOP-fabricated), 0.7
# (fuzzy match), or 1.0 (exact match) -- this just needs to sit above 0.0, so a
# required field that only got an SOP default (e.g. the DOB 01/01/1900
# fallback) can't pass as if it were read off the page. Not the same knob as
# `ocr_low_confidence_threshold`: that one is surfaced but never used to
# reject a scan; this one gates review-eligibility because a document with
# nothing extracted is not a scan to review at all.
MIN_CONFIDENCE = 0.5

_REQUIRED_FIELDS: dict[DocumentType, tuple[str, ...]] = {
    DocumentType.EPCR: ("last_name", "first_name", "run_number", "date_of_service"),
    DocumentType.HFS: ("last_name", "first_name", "dob"),
    DocumentType.PCS: ("last_name", "first_name", "authorizing_physician"),
    DocumentType.BOOKING_SUMMARY: ("last_name", "first_name"),
    DocumentType.UNKNOWN: ("last_name", "first_name"),
}


@dataclass(frozen=True, slots=True)
class Verdict:
    passed: bool
    # Field *names* only, never a value -- safe to store in `failure_reason`
    # and log directly, the same discipline as every other operator-facing
    # string in this pipeline.
    missing_fields: tuple[str, ...] = ()
    min_confidence: float = 0.0


def evaluate(
    doc_type: DocumentType,
    fields: PatientExtraction,
    *,
    min_confidence: float = MIN_CONFIDENCE,
) -> Verdict:
    """Required fields for `doc_type` must all have a `typed_value`, and every
    one of them must individually clear `min_confidence` -- gated on the
    minimum, not the mean. A mean would let one fabricated required field
    (an SOP default, confidence 0.0) hide behind two or three well-grounded
    ones and still average above the bar; the whole point of this check is
    that a fabricated required field can't pass as if it were read off the
    page, no matter how good its neighbors look.
    """
    required = _REQUIRED_FIELDS.get(doc_type, _REQUIRED_FIELDS[DocumentType.UNKNOWN])
    required_fields = [getattr(fields, name) for name in required]

    missing = tuple(
        name
        for name, field in zip(required, required_fields, strict=True)
        # A field whose typed_value was supplied by an SOP default (e.g. the
        # UNKNOWN name alias, the 01/01/1900 DOB fallback) counts as missing:
        # the SOP filled it in because nothing was found, not because something
        # was read off the page. Before this change the failure reason read
        # "missing=first_name" for a no-name document, because last_name was
        # filled with "UNKNOWN" at SOP_DEFAULT and appeared present.
        if not field.typed_value or field.source is FieldSource.SOP_DEFAULT
    )
    present = [field for field in required_fields if field.typed_value]
    confidences = [field.confidence for field in present]
    min_required_confidence = min(confidences) if confidences else 0.0

    return Verdict(
        passed=not missing and min_required_confidence >= min_confidence,
        missing_fields=missing,
        min_confidence=min_required_confidence,
    )
