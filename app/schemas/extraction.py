"""Field-extraction schemas.

Two shapes live here. `RawExtractedField`/`RawPatientExtraction` are what the
LLM is asked to produce -- just a value and the page it came from, since the
model never sees pixels and has no calibrated confidence to report.
`ExtractedField`/`PatientExtraction` are what `ExtractionService` produces
after grounding: confidence and `source_bbox` are computed from
`ocr_results.page_texts`, not asked of the model. See CLAUDE.md and
`app/services/extraction_service.py` for why.

One shared schema serves every document type this pipeline sees (HFS, EPCR,
PCS, Booking Summary, and whatever a page turns out to actually be): the
field names are a superset across all of them, and only which sections
appear on the page differs -- that is a prompt concern, not a schema
concern. A field that doesn't apply to a given document is simply left null.

Insurance is a list, not flat fields: real face sheets carry up to three
concurrent slots (primary/secondary/tertiary), each with its own policy,
group, and subscriber. A flat `insurance_name`/`policy_id` pair can only ever
represent one.

Every field here is PHI.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class FieldSource(StrEnum):
    """Where a field's `typed_value` came from -- so a reformatted value or a
    rule-supplied default is never mistaken for something read off the page.

    - `grounded`    typed value is the on-page text verbatim (or a substring of it)
    - `reformatted` deterministically transformed from the on-page text (e.g. date, phone)
    - `sop_default` supplied by an SOP rule because the field was absent (e.g. DOB 01/01/1900)
    - `skipped`     present on the page but the SOP says not to enter it (e.g. encrypted SSN)
    - `absent`      nothing found and no default applies
    - `corrected`   overwritten by a human reviewer -- never mistaken for a page reading
    """

    GROUNDED = "grounded"
    REFORMATTED = "reformatted"
    SOP_DEFAULT = "sop_default"
    SKIPPED = "skipped"
    ABSENT = "absent"
    CORRECTED = "corrected"


FIELD_NAMES = (
    # Identity
    "last_name",
    "first_name",
    "suffix",
    "dob",
    # Printed age -- distinct from `dob`, and useful precisely when `dob` is
    # ambiguous: a 2-digit-year DOB ("12/19/53") can't say for itself whether
    # it's 1953 or 2053, but a printed age pins the century (normalization.py's
    # `_resolve_two_digit_year`). Captured like any other field; never fed
    # into a billing decision on its own.
    "age",
    "gender",
    "ssn",
    # `phone` is the SOP's "unspecified type"; the typed variants exist because
    # the prebiller's priority ladder is mobile > unspecified > home > work.
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
    # Guarantor / financially-responsible party
    "guarantor_last_name",
    "guarantor_first_name",
    "guarantor_relationship",
    "guarantor_dob",
    "guarantor_phone",
    "guarantor_address",
    "guarantor_ssn",
    "guarantor_occupation",
    # Legal guardian / conservator -- distinct from guarantor: seen alongside
    # involuntary-admission and minor-patient documents, a different person
    # with a different legal relationship to the patient.
    "guardian_name",
    "guardian_relationship",
    # Next of kin -- distinct from guarantor and from the guardian: a contact
    # role, not a financial or legal one.
    "next_of_kin_name",
    "next_of_kin_phone",
    # Trip / EPCR
    "run_number",
    "date_of_service",
    "pickup_address",
    "destination_facility",
    "transport_priority",
    "unit_number",
    "actual_mileage",
    # Odometer distance from the trip's travel information -- the SOP's
    # mileage-review rule compares it against `actual_mileage`.
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
    # PCS (Physician Certification Statement) / Booking Summary
    "authorizing_physician",
    "physician_title",
    "pcs_date_of_transfer",
    "pcs_date_of_sign",
    "facility_pay_reason",
    "diagnosis",
    "notifier_name",
    "notifier_phone",
)

# One insurance slot's field names. A document carries 0-3 slots
# (`PatientExtraction.insurance`); each slot has this same shape.
INSURANCE_FIELD_NAMES = (
    "insurance_name",
    "policy_id",
    "group_number",
    "subscriber_name",
    "relationship_to_subscriber",
    "effective_date",
    "verified",
    # The payer's own contact block -- the SOP's unknown-insurance and
    # out-of-state-Medicaid rules key off the payer's address/phone.
    "payer_address",
    "payer_phone",
)

MAX_INSURANCE_SLOTS = 3


def _coerce_bare_null_fields(data: Any, *, field_names: tuple[str, ...]) -> Any:
    """Local models routinely emit a bare `"field": null` for an empty field
    instead of the `{"value": null, "source_page": null}` object shape every
    `RawExtractedField`-typed field actually requires -- observed live as a
    `model_type` validation error on every empty field simultaneously,
    surviving even the one repair retry (a deterministic prompt reproduces a
    systematic model habit, not a one-off glitch). `{}` still validates as an
    all-null `RawExtractedField`, so this is a no-op for every field the model
    already shaped correctly."""
    if isinstance(data, dict):
        for name in field_names:
            if data.get(name) is None and name in data:
                data[name] = {}
    return data


class RawExtractedField(BaseModel):
    """What the LLM is prompted to return for one field."""

    value: str | None = None
    source_page: int | None = None


class RawInsuranceSlot(BaseModel):
    """What the LLM is prompted to return for one insurance slot."""

    @model_validator(mode="before")
    @classmethod
    def _coerce_null_fields(cls, data: Any) -> Any:
        return _coerce_bare_null_fields(data, field_names=INSURANCE_FIELD_NAMES)

    insurance_name: RawExtractedField = RawExtractedField()
    policy_id: RawExtractedField = RawExtractedField()
    group_number: RawExtractedField = RawExtractedField()
    subscriber_name: RawExtractedField = RawExtractedField()
    relationship_to_subscriber: RawExtractedField = RawExtractedField()
    effective_date: RawExtractedField = RawExtractedField()
    verified: RawExtractedField = RawExtractedField()
    payer_address: RawExtractedField = RawExtractedField()
    payer_phone: RawExtractedField = RawExtractedField()


class RawPatientExtraction(BaseModel):
    """The LLM's raw JSON response, parsed and validated. Not yet grounded."""

    @model_validator(mode="before")
    @classmethod
    def _coerce_null_fields(cls, data: Any) -> Any:
        return _coerce_bare_null_fields(data, field_names=FIELD_NAMES)

    last_name: RawExtractedField = RawExtractedField()
    first_name: RawExtractedField = RawExtractedField()
    suffix: RawExtractedField = RawExtractedField()
    dob: RawExtractedField = RawExtractedField()
    age: RawExtractedField = RawExtractedField()
    gender: RawExtractedField = RawExtractedField()
    ssn: RawExtractedField = RawExtractedField()
    phone: RawExtractedField = RawExtractedField()
    phone_mobile: RawExtractedField = RawExtractedField()
    phone_home: RawExtractedField = RawExtractedField()
    phone_work: RawExtractedField = RawExtractedField()
    email: RawExtractedField = RawExtractedField()
    address_line1: RawExtractedField = RawExtractedField()
    address_line2: RawExtractedField = RawExtractedField()
    city: RawExtractedField = RawExtractedField()
    state: RawExtractedField = RawExtractedField()
    zip: RawExtractedField = RawExtractedField()
    mrn: RawExtractedField = RawExtractedField()
    account_number: RawExtractedField = RawExtractedField()
    ethnicity: RawExtractedField = RawExtractedField()
    race: RawExtractedField = RawExtractedField()
    national_origin: RawExtractedField = RawExtractedField()
    religion: RawExtractedField = RawExtractedField()
    employment_status: RawExtractedField = RawExtractedField()
    employer_name: RawExtractedField = RawExtractedField()
    pcp_name: RawExtractedField = RawExtractedField()
    marital_status: RawExtractedField = RawExtractedField()
    preferred_pronouns: RawExtractedField = RawExtractedField()
    legal_hold_status: RawExtractedField = RawExtractedField()
    admission_type: RawExtractedField = RawExtractedField()
    pt_class: RawExtractedField = RawExtractedField()
    case_number: RawExtractedField = RawExtractedField()
    weight_kg: RawExtractedField = RawExtractedField()
    guarantor_last_name: RawExtractedField = RawExtractedField()
    guarantor_first_name: RawExtractedField = RawExtractedField()
    guarantor_relationship: RawExtractedField = RawExtractedField()
    guarantor_dob: RawExtractedField = RawExtractedField()
    guarantor_phone: RawExtractedField = RawExtractedField()
    guarantor_address: RawExtractedField = RawExtractedField()
    guarantor_ssn: RawExtractedField = RawExtractedField()
    guarantor_occupation: RawExtractedField = RawExtractedField()
    guardian_name: RawExtractedField = RawExtractedField()
    guardian_relationship: RawExtractedField = RawExtractedField()
    next_of_kin_name: RawExtractedField = RawExtractedField()
    next_of_kin_phone: RawExtractedField = RawExtractedField()
    run_number: RawExtractedField = RawExtractedField()
    date_of_service: RawExtractedField = RawExtractedField()
    pickup_address: RawExtractedField = RawExtractedField()
    destination_facility: RawExtractedField = RawExtractedField()
    transport_priority: RawExtractedField = RawExtractedField()
    unit_number: RawExtractedField = RawExtractedField()
    actual_mileage: RawExtractedField = RawExtractedField()
    actual_distance: RawExtractedField = RawExtractedField()
    unit_en_route_datetime: RawExtractedField = RawExtractedField()
    unit_arrived_scene_datetime: RawExtractedField = RawExtractedField()
    arrived_at_patient_datetime: RawExtractedField = RawExtractedField()
    unit_left_scene_datetime: RawExtractedField = RawExtractedField()
    patient_arrived_destination_datetime: RawExtractedField = RawExtractedField()
    unit_back_in_service_datetime: RawExtractedField = RawExtractedField()
    chief_complaint: RawExtractedField = RawExtractedField()
    primary_impression: RawExtractedField = RawExtractedField()
    protocol_name: RawExtractedField = RawExtractedField()
    response_urgency: RawExtractedField = RawExtractedField()
    cms_service_level: RawExtractedField = RawExtractedField()
    requested_level_of_service: RawExtractedField = RawExtractedField()
    als_assessment_performed: RawExtractedField = RawExtractedField()
    authorizing_physician: RawExtractedField = RawExtractedField()
    physician_title: RawExtractedField = RawExtractedField()
    pcs_date_of_transfer: RawExtractedField = RawExtractedField()
    pcs_date_of_sign: RawExtractedField = RawExtractedField()
    facility_pay_reason: RawExtractedField = RawExtractedField()
    diagnosis: RawExtractedField = RawExtractedField()
    notifier_name: RawExtractedField = RawExtractedField()
    notifier_phone: RawExtractedField = RawExtractedField()

    insurance: list[RawInsuranceSlot] = Field(default_factory=list, max_length=MAX_INSURANCE_SLOTS)


class ExtractedField(BaseModel):
    """One field, in two forms.

    `value` is the grounded raw text: proven verbatim in the OCR (the guard sets
    `confidence` and `source_bbox`, both describing `value`). `typed_value` is what
    the prebiller enters into the billing system -- a deterministic transform of `value`,
    or an SOP default when `value` is absent. `source` says which, so a reformat
    or a rule-supplied default is never confused with a page reading."""

    value: str | None = None
    confidence: float = 0.0
    source_page: int | None = None
    source_bbox: list[list[float]] | None = None
    typed_value: str | None = None
    source: FieldSource = FieldSource.ABSENT


class InsuranceSlot(BaseModel):
    """One grounded insurance slot. Field names match `RawInsuranceSlot`."""

    insurance_name: ExtractedField = ExtractedField()
    policy_id: ExtractedField = ExtractedField()
    group_number: ExtractedField = ExtractedField()
    subscriber_name: ExtractedField = ExtractedField()
    relationship_to_subscriber: ExtractedField = ExtractedField()
    effective_date: ExtractedField = ExtractedField()
    verified: ExtractedField = ExtractedField()
    payer_address: ExtractedField = ExtractedField()
    payer_phone: ExtractedField = ExtractedField()


class FieldCorrection(BaseModel):
    """Request body for a reviewer overwriting one field's typed value.

    `None` (or an empty string) clears the typed value. `max_length` bounds the
    write -- the same discipline as every other PHI-bearing input.
    """

    typed_value: str | None = Field(default=None, max_length=255)


class PatientExtraction(BaseModel):
    """The grounded, storable result. Field names match `RawPatientExtraction`."""

    last_name: ExtractedField = ExtractedField()
    first_name: ExtractedField = ExtractedField()
    suffix: ExtractedField = ExtractedField()
    dob: ExtractedField = ExtractedField()
    age: ExtractedField = ExtractedField()
    gender: ExtractedField = ExtractedField()
    ssn: ExtractedField = ExtractedField()
    phone: ExtractedField = ExtractedField()
    phone_mobile: ExtractedField = ExtractedField()
    phone_home: ExtractedField = ExtractedField()
    phone_work: ExtractedField = ExtractedField()
    email: ExtractedField = ExtractedField()
    address_line1: ExtractedField = ExtractedField()
    address_line2: ExtractedField = ExtractedField()
    city: ExtractedField = ExtractedField()
    state: ExtractedField = ExtractedField()
    zip: ExtractedField = ExtractedField()
    mrn: ExtractedField = ExtractedField()
    account_number: ExtractedField = ExtractedField()
    ethnicity: ExtractedField = ExtractedField()
    race: ExtractedField = ExtractedField()
    national_origin: ExtractedField = ExtractedField()
    religion: ExtractedField = ExtractedField()
    employment_status: ExtractedField = ExtractedField()
    employer_name: ExtractedField = ExtractedField()
    pcp_name: ExtractedField = ExtractedField()
    marital_status: ExtractedField = ExtractedField()
    preferred_pronouns: ExtractedField = ExtractedField()
    legal_hold_status: ExtractedField = ExtractedField()
    admission_type: ExtractedField = ExtractedField()
    pt_class: ExtractedField = ExtractedField()
    case_number: ExtractedField = ExtractedField()
    weight_kg: ExtractedField = ExtractedField()
    guarantor_last_name: ExtractedField = ExtractedField()
    guarantor_first_name: ExtractedField = ExtractedField()
    guarantor_relationship: ExtractedField = ExtractedField()
    guarantor_dob: ExtractedField = ExtractedField()
    guarantor_phone: ExtractedField = ExtractedField()
    guarantor_address: ExtractedField = ExtractedField()
    guarantor_ssn: ExtractedField = ExtractedField()
    guarantor_occupation: ExtractedField = ExtractedField()
    guardian_name: ExtractedField = ExtractedField()
    guardian_relationship: ExtractedField = ExtractedField()
    next_of_kin_name: ExtractedField = ExtractedField()
    next_of_kin_phone: ExtractedField = ExtractedField()
    run_number: ExtractedField = ExtractedField()
    date_of_service: ExtractedField = ExtractedField()
    pickup_address: ExtractedField = ExtractedField()
    destination_facility: ExtractedField = ExtractedField()
    transport_priority: ExtractedField = ExtractedField()
    unit_number: ExtractedField = ExtractedField()
    actual_mileage: ExtractedField = ExtractedField()
    actual_distance: ExtractedField = ExtractedField()
    unit_en_route_datetime: ExtractedField = ExtractedField()
    unit_arrived_scene_datetime: ExtractedField = ExtractedField()
    arrived_at_patient_datetime: ExtractedField = ExtractedField()
    unit_left_scene_datetime: ExtractedField = ExtractedField()
    patient_arrived_destination_datetime: ExtractedField = ExtractedField()
    unit_back_in_service_datetime: ExtractedField = ExtractedField()
    chief_complaint: ExtractedField = ExtractedField()
    primary_impression: ExtractedField = ExtractedField()
    protocol_name: ExtractedField = ExtractedField()
    response_urgency: ExtractedField = ExtractedField()
    cms_service_level: ExtractedField = ExtractedField()
    requested_level_of_service: ExtractedField = ExtractedField()
    als_assessment_performed: ExtractedField = ExtractedField()
    authorizing_physician: ExtractedField = ExtractedField()
    physician_title: ExtractedField = ExtractedField()
    pcs_date_of_transfer: ExtractedField = ExtractedField()
    pcs_date_of_sign: ExtractedField = ExtractedField()
    facility_pay_reason: ExtractedField = ExtractedField()
    diagnosis: ExtractedField = ExtractedField()
    notifier_name: ExtractedField = ExtractedField()
    notifier_phone: ExtractedField = ExtractedField()

    insurance: list[InsuranceSlot] = Field(default_factory=list, max_length=MAX_INSURANCE_SLOTS)
