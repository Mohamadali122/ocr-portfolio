"""Cross-document patient grouping and identity-mismatch flagging.

No `patients` table exists, by decision (see CLAUDE.md "Database conventions"):
identity resolution is a real dedup problem with competing match keys, and
creating a table now forces a premature answer search already avoids by using
denormalized indexed columns on `documents`. This is the same philosophy
applied one step further -- a read-time computation over those columns, not a
persisted grouping or match decision. Nothing here survives a correction to
one of the documents involved; the next read just sees the corrected value.

Concrete motivating case (`sample/ANALYSIS.md`): `sample/insurance_eligibility.pdf`,
an Availity insurance-eligibility response, contains the literal text "Either
the patient's ID, name, date of birth, or address in the response does not
match the information sent in the request" -- a real instance of exactly the
mismatch this flags.
"""

from dataclasses import dataclass

from app.models.document import Document
from app.repositories.document_repository import DocumentRepository

# The only fields denormalized onto `documents` (see `app/models/document.py`).
# `Extraction.fields` (SSN, etc.) isn't reachable in bulk without a JSONB
# query, and reaching for it here would be exactly the kind of premature
# identity-resolution scope creep this design avoids -- a real need, but a
# separate one.
_COMPARED_FIELDS = ("patient_last_name", "patient_first_name", "patient_dob", "run_number")


@dataclass(frozen=True, slots=True)
class MismatchFlag:
    field: str
    # Distinct non-null values seen for `field` across the anchor and its
    # related documents. A document missing the field entirely doesn't count
    # as disagreeing with one that has it -- only two actual values differing
    # does.
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RelatedDocumentsResult:
    related: tuple[Document, ...]
    mismatches: tuple[MismatchFlag, ...]


class PatientGroupingService:
    def __init__(self, documents: DocumentRepository) -> None:
        self._documents = documents

    def find_related(self, anchor: Document) -> RelatedDocumentsResult:
        related = tuple(
            self._documents.find_related(
                run_number=anchor.run_number,
                last_name=anchor.patient_last_name,
                first_name=anchor.patient_first_name,
                dob=anchor.patient_dob,
                exclude_id=anchor.id,
            )
        )
        return RelatedDocumentsResult(related=related, mismatches=_find_mismatches(anchor, related))


def _find_mismatches(anchor: Document, related: tuple[Document, ...]) -> tuple[MismatchFlag, ...]:
    if not related:
        return ()
    flags: list[MismatchFlag] = []
    for field_name in _COMPARED_FIELDS:
        values = {
            str(v) for doc in (anchor, *related) if (v := getattr(doc, field_name)) is not None
        }
        if len(values) > 1:
            flags.append(MismatchFlag(field=field_name, values=tuple(sorted(values))))
    return tuple(flags)
