"""Deterministic claim-level field merge.

A claim's member documents each already carry a grounded, per-document
`Extraction` (`app/services/extraction_service.py`) -- this is not a second
extraction pass. It is the direct generalization of
`app/services/patient_grouping_service.py`'s `MismatchFlag` from four
denormalized columns to the full 77-field schema: for every field, look at
every member document's typed value, pick the best one by a fixed precedence
table plus grounding confidence, and record a `FieldConflict` whenever two
documents disagreed on a non-null value.

Not a second LLM pass over the concatenated text: a merged multi-document
claim can run well past the completion budget a single call already needs
headroom for (see commit `fe32373`, "Fix LLM extraction failures on large
documents"), and a deterministic merge gives per-field provenance and free
conflict detection that a second model call could not -- exactly the
evidence the `Pt Demos Correction` and `PCS correction` tags need.

Nothing here is persisted. Like `flags.suggest()` and `PatientGroupingService`,
this runs at read/tag-time over already-stored `Extraction` rows: a precedence
change applies to every existing claim instantly, with no migration and no
second copy of PHI on disk.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.models.document import DocumentType
from app.models.extraction import Extraction
from app.schemas.extraction import (
    FIELD_NAMES,
    INSURANCE_FIELD_NAMES,
    MAX_INSURANCE_SLOTS,
    ExtractedField,
    FieldSource,
    PatientExtraction,
)


@dataclass(frozen=True, slots=True)
class MergedField:
    value: str | None
    typed_value: str | None
    confidence: float
    source: FieldSource
    document_id: uuid.UUID | None
    source_page: int | None
    source_bbox: list[list[float]] | None


_ABSENT_FIELD = MergedField(
    value=None,
    typed_value=None,
    confidence=0.0,
    source=FieldSource.ABSENT,
    document_id=None,
    source_page=None,
    source_bbox=None,
)


@dataclass(frozen=True, slots=True)
class FieldConflict:
    """Two or more member documents had different non-null typed values for
    `field`. `values_by_document` is ordered by document `position`."""

    field: str
    values_by_document: tuple[tuple[uuid.UUID, str], ...]


@dataclass(frozen=True, slots=True)
class MergedInsuranceSlot:
    insurance_name: MergedField
    policy_id: MergedField
    group_number: MergedField
    subscriber_name: MergedField
    relationship_to_subscriber: MergedField
    effective_date: MergedField
    verified: MergedField
    payer_address: MergedField
    payer_phone: MergedField


@dataclass(frozen=True, slots=True)
class MergedExtraction:
    fields: dict[str, MergedField]  # keyed by app.schemas.extraction.FIELD_NAMES
    insurance: tuple[MergedInsuranceSlot, ...]
    conflicts: tuple[FieldConflict, ...]


@dataclass(frozen=True, slots=True)
class MemberDocument:
    """One claim member, as `claim_merge.merge` needs it. `extraction` is
    `None` for a document that hasn't reached a completed AI stage yet --
    it contributes no field candidates, but its `doc_type` still counts for
    `has_doc_type`/`missing_doc_type` tag rules (`app/services/tagging.py`)."""

    position: int
    document_id: uuid.UUID
    doc_type: DocumentType
    extraction: Extraction | None


# Which document type's value wins for a field, most preferred first. A field
# not in any of these four groups (there are none today -- every FIELD_NAMES
# entry is classified) falls back to `_DEMOGRAPHIC_FIELDS`'s order.
#
# Matches the prompt hints in extraction_service._DOC_TYPE_HINTS: a face sheet
# carries identity/insurance, an ePCR carries the trip and clinical picture, a
# PCS carries the physician/medical-necessity detail, a booking summary
# carries the notifier. One table, not a rule per field.
_DEMOGRAPHIC_FIELDS = frozenset(
    {
        "last_name",
        "first_name",
        "suffix",
        "dob",
        "gender",
        "ssn",
        "phone",
        "phone_mobile",
        "phone_home",
        "phone_work",
        "email",
        "address_line1",
        "address_line2",
        "city",
        "state",
        "zip",
        "mrn",
        "account_number",
        "ethnicity",
        "race",
        "national_origin",
        "religion",
        "employment_status",
        "employer_name",
        "pcp_name",
        "marital_status",
        "preferred_pronouns",
        "legal_hold_status",
        "admission_type",
        "pt_class",
        "case_number",
        "weight_kg",
        "guarantor_last_name",
        "guarantor_first_name",
        "guarantor_relationship",
        "guarantor_dob",
        "guarantor_phone",
        "guarantor_address",
        "guarantor_ssn",
        "guarantor_occupation",
        "guardian_name",
        "guardian_relationship",
        "next_of_kin_name",
        "next_of_kin_phone",
    }
)

_TRIP_FIELDS = frozenset(
    {
        "run_number",
        "date_of_service",
        "pickup_address",
        "destination_facility",
        "transport_priority",
        "unit_number",
        "actual_mileage",
        "actual_distance",
        "unit_en_route_datetime",
        "unit_arrived_scene_datetime",
        "arrived_at_patient_datetime",
        "unit_left_scene_datetime",
        "patient_arrived_destination_datetime",
        "unit_back_in_service_datetime",
        "chief_complaint",
        "primary_impression",
        "protocol_name",
        "response_urgency",
        "cms_service_level",
        "requested_level_of_service",
        "als_assessment_performed",
    }
)

_PCS_FIELDS = frozenset(
    {
        "authorizing_physician",
        "physician_title",
        "pcs_date_of_transfer",
        "pcs_date_of_sign",
        "facility_pay_reason",
        "diagnosis",
    }
)

_BOOKING_FIELDS = frozenset({"notifier_name", "notifier_phone"})

_DEMOGRAPHIC_PRECEDENCE = (
    DocumentType.HFS,
    DocumentType.EPCR,
    DocumentType.PCS,
    DocumentType.BOOKING_SUMMARY,
)
_TRIP_PRECEDENCE = (
    DocumentType.EPCR,
    DocumentType.BOOKING_SUMMARY,
    DocumentType.HFS,
    DocumentType.PCS,
)
_PCS_PRECEDENCE = (
    DocumentType.PCS,
    DocumentType.HFS,
    DocumentType.EPCR,
    DocumentType.BOOKING_SUMMARY,
)
_BOOKING_PRECEDENCE = (
    DocumentType.BOOKING_SUMMARY,
    DocumentType.EPCR,
    DocumentType.HFS,
    DocumentType.PCS,
)
# Insurance is demographic-shaped: extraction_service's own prompt hints say an
# ambulance company's paperwork rarely fills in its own insurance section, so a
# face sheet is the preferred source whenever one is present.
_INSURANCE_PRECEDENCE = _DEMOGRAPHIC_PRECEDENCE


def _precedence(field_name: str) -> tuple[DocumentType, ...]:
    if field_name in _TRIP_FIELDS:
        return _TRIP_PRECEDENCE
    if field_name in _PCS_FIELDS:
        return _PCS_PRECEDENCE
    if field_name in _BOOKING_FIELDS:
        return _BOOKING_PRECEDENCE
    return _DEMOGRAPHIC_PRECEDENCE  # covers _DEMOGRAPHIC_FIELDS and anything unclassified


def _rank(doc_type: DocumentType, precedence: tuple[DocumentType, ...]) -> int:
    try:
        return precedence.index(doc_type)
    except ValueError:
        return len(precedence)  # UNKNOWN, or a type not in this field's table


@dataclass(frozen=True, slots=True)
class _Candidate:
    sort_key: tuple[int, int, int, float, int]
    field: ExtractedField
    document_id: uuid.UUID


def _sort_key(
    field: ExtractedField,
    doc_type: DocumentType,
    position: int,
    precedence: tuple[DocumentType, ...],
) -> tuple[int, int, int, float, int]:
    # Ascending: lower sorts first, i.e. wins.
    #
    # 1. A reviewer's correction always wins (0 < 1).
    # 2. A real, page-grounded value always outranks an SOP-default filler
    #    (the CBO address, the 01/01/1900 DOB, the UNKNOWN name alias --
    #    normalization.py) from a different document, *regardless* of that
    #    document's doc-type precedence. An SOP default is what
    #    normalization.py emits when a field's own document has nothing --
    #    "missing", not "answered". If a claim's other member documents have
    #    the real value, that value is what a prebiller needs, not the
    #    precedence-preferred document's placeholder text.
    # 3. Only once both documents' values are equally "real" (or equally
    #    fabricated) does the field's own doc-type precedence decide.
    # 4. Then higher confidence (negated so it sorts first).
    # 5. Then upload order, the final, fully deterministic tiebreak.
    corrected = 0 if field.source is FieldSource.CORRECTED else 1
    sop_default = 1 if field.source is FieldSource.SOP_DEFAULT else 0
    return (corrected, sop_default, _rank(doc_type, precedence), -field.confidence, position)


def _to_merged(candidate: _Candidate | None) -> MergedField:
    if candidate is None:
        return _ABSENT_FIELD
    f = candidate.field
    return MergedField(
        value=f.value,
        typed_value=f.typed_value,
        confidence=f.confidence,
        source=f.source,
        document_id=candidate.document_id,
        source_page=f.source_page,
        source_bbox=f.source_bbox,
    )


def merge(members: Sequence[MemberDocument]) -> MergedExtraction:
    ordered = sorted(members, key=lambda m: m.position)
    parsed = [
        (
            m.position,
            m.document_id,
            m.doc_type,
            PatientExtraction.model_validate(m.extraction.fields),
        )
        for m in ordered
        if m.extraction is not None
    ]

    fields: dict[str, MergedField] = {}
    conflicts: list[FieldConflict] = []
    for name in FIELD_NAMES:
        precedence = _precedence(name)
        candidates: list[_Candidate] = []
        distinct_values: dict[str, list[uuid.UUID]] = {}
        for position, document_id, doc_type, extraction in parsed:
            field: ExtractedField = getattr(extraction, name)
            if not field.typed_value:
                continue
            distinct_values.setdefault(field.typed_value, []).append(document_id)
            candidates.append(
                _Candidate(
                    sort_key=_sort_key(field, doc_type, position, precedence),
                    field=field,
                    document_id=document_id,
                )
            )
        best = min(candidates, key=lambda c: c.sort_key) if candidates else None
        fields[name] = _to_merged(best)
        if len(distinct_values) > 1:
            conflicts.append(
                FieldConflict(
                    field=name,
                    values_by_document=tuple(
                        (document_id, value)
                        for value, document_ids in distinct_values.items()
                        for document_id in document_ids
                    ),
                )
            )

    insurance = tuple(_merge_insurance_slot(index, parsed) for index in range(MAX_INSURANCE_SLOTS))
    return MergedExtraction(fields=fields, insurance=insurance, conflicts=tuple(conflicts))


def _merge_insurance_slot(
    slot_index: int,
    parsed: list[tuple[int, uuid.UUID, DocumentType, PatientExtraction]],
) -> MergedInsuranceSlot:
    fields: dict[str, MergedField] = {}
    for name in INSURANCE_FIELD_NAMES:
        candidates: list[_Candidate] = []
        for position, document_id, doc_type, extraction in parsed:
            if slot_index >= len(extraction.insurance):
                continue
            field: ExtractedField = getattr(extraction.insurance[slot_index], name)
            if not field.typed_value:
                continue
            candidates.append(
                _Candidate(
                    sort_key=_sort_key(field, doc_type, position, _INSURANCE_PRECEDENCE),
                    field=field,
                    document_id=document_id,
                )
            )
        best = min(candidates, key=lambda c: c.sort_key) if candidates else None
        fields[name] = _to_merged(best)
    return MergedInsuranceSlot(**fields)
