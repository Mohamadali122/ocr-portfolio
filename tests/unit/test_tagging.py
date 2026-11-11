import uuid

from app.core.logging import REDACTED, redact_processor
from app.models.document import DocumentType
from app.schemas.extraction import FIELD_NAMES, FieldSource
from app.services.claim_merge import (
    FieldConflict,
    MergedExtraction,
    MergedField,
    MergedInsuranceSlot,
)
from app.services.tag_registry import Registry, TagDefinition, TagRule
from app.services.tag_registry import key as tag_key
from app.services.tagging import ClaimContext, ClaimDocumentText, evaluate

_ABSENT = MergedField(
    value=None,
    typed_value=None,
    confidence=0.0,
    source=FieldSource.ABSENT,
    document_id=None,
    source_page=None,
    source_bbox=None,
)


def _field(
    value: str, *, confidence: float = 1.0, source: FieldSource = FieldSource.GROUNDED
) -> MergedField:
    return MergedField(
        value=value,
        typed_value=value,
        confidence=confidence,
        source=source,
        document_id=uuid.uuid4(),
        source_page=1,
        source_bbox=None,
    )


def _insurance_slot(**overrides: MergedField) -> MergedInsuranceSlot:
    base = dict.fromkeys(MergedInsuranceSlot.__dataclass_fields__, _ABSENT)
    base.update(overrides)
    return MergedInsuranceSlot(**base)


def _merged(
    *, fields: dict[str, MergedField] | None = None, conflicts: tuple[FieldConflict, ...] = ()
) -> MergedExtraction:
    all_fields: dict[str, MergedField] = dict.fromkeys(FIELD_NAMES, _ABSENT)
    all_fields.update(fields or {})
    return MergedExtraction(
        fields=all_fields,
        insurance=(_insurance_slot(), _insurance_slot(), _insurance_slot()),
        conflicts=conflicts,
    )


def _ctx(
    *,
    fields: dict[str, MergedField] | None = None,
    conflicts: tuple[FieldConflict, ...] = (),
    doc_types: frozenset[DocumentType] = frozenset(),
    documents: tuple[ClaimDocumentText, ...] = (),
    doc_types_complete: bool = True,
) -> ClaimContext:
    return ClaimContext(
        merged=_merged(fields=fields, conflicts=conflicts),
        doc_types_present=doc_types,
        documents=documents,
        doc_types_complete=doc_types_complete,
    )


def _doc_text(text: str, *, position: int = 0) -> ClaimDocumentText:
    return ClaimDocumentText(
        document_id=uuid.uuid4(),
        position=position,
        doc_type=DocumentType.EPCR,
        raw_text=text,
        page_texts=[{"page": 1, "lines": [{"text": text}]}],
    )


def _registry(*rules: tuple[str, dict]) -> Registry:
    tags = tuple(
        TagDefinition(
            name=name, key=tag_key(name), reason="test", rule=TagRule(rule_id=name, tree=tree)
        )
        for name, tree in rules
    )
    return Registry(tags=tags)


class TestLeaves:
    def test_text_matches_a_recognized_line(self) -> None:
        registry = _registry(("Hospice", {"text": r"\bhospice\b"}))
        ctx = _ctx(documents=(_doc_text("PATIENT IS HOSPICE PER FAMILY"),))
        result = evaluate(registry, ctx)
        assert [a.tag_name for a in result] == ["Hospice"]
        assert result[0].confidence == 1.0
        assert result[0].evidence[0].kind == "text"

    def test_text_ignores_a_line_repeated_across_pages(self) -> None:
        registry = _registry(("CAS Employee", {"text": r"city ambulance"}))
        header = "Company City Ambulance Service Incident# 1 PCR# 1"
        pages = [
            {"page": n, "lines": [{"text": header}, {"text": f"detail {n}"}]}
            for n in range(1, 6)
        ]
        doc = ClaimDocumentText(
            document_id=uuid.uuid4(),
            position=0,
            doc_type=DocumentType.EPCR,
            raw_text="\n".join(header for _ in range(5)),
            page_texts=pages,
        )
        assert evaluate(registry, _ctx(documents=(doc,))) == []

    def test_inmate_fires_on_two_page_repeat(self) -> None:
        registry = _registry(("INMATE", {"text": r"\binmate\b"}))
        line = "Patient is an INMATE"
        doc = ClaimDocumentText(
            document_id=uuid.uuid4(),
            position=0,
            doc_type=DocumentType.EPCR,
            raw_text=f"{line}\n\n{line}",
            page_texts=[
                {"page": 1, "lines": [{"text": line}]},
                {"page": 2, "lines": [{"text": line}]},
            ],
        )
        assert [a.tag_name for a in evaluate(registry, _ctx(documents=(doc,)))] == ["INMATE"]

    def test_five_page_letterhead_still_suppresses_repeated_line(self) -> None:
        registry = _registry(("INMATE", {"text": r"\binmate\b"}))
        header = "Facility INMATE roster header"
        pages = [
            {"page": n, "lines": [{"text": header}, {"text": f"page {n} unique"}]}
            for n in range(1, 6)
        ]
        doc = ClaimDocumentText(
            document_id=uuid.uuid4(),
            position=0,
            doc_type=DocumentType.EPCR,
            raw_text="\n".join(header for _ in range(5)),
            page_texts=pages,
        )
        assert evaluate(registry, _ctx(documents=(doc,))) == []

    def test_text_still_matches_a_line_that_does_not_repeat(self) -> None:
        # The boilerplate filter must not swallow a genuine, page-unique match.
        registry = _registry(("Hospice", {"text": r"\bhospice\b"}))
        header = "Company City Ambulance Service Incident# 1 PCR# 1"
        doc = ClaimDocumentText(
            document_id=uuid.uuid4(),
            position=0,
            doc_type=DocumentType.EPCR,
            raw_text=f"{header}\nPatient is hospice per family\n\n{header}\nVitals normal",
            page_texts=[
                {"page": 1, "lines": [{"text": header}, {"text": "Patient is hospice per family"}]},
                {"page": 2, "lines": [{"text": header}, {"text": "Vitals normal"}]},
            ],
        )
        assert [a.tag_name for a in evaluate(registry, _ctx(documents=(doc,)))] == ["Hospice"]

    def test_text_ignores_a_match_ending_in_a_negative_answer(self) -> None:
        # A structured "Label Answer" field line must not fire the label's
        # tag when the line's own answer is negative -- confirmed live: a
        # real PCR's "Work-Related Illness/Injury No" line narrowly missed
        # firing "Work comp" only by luck of that exact label's wording.
        registry = _registry(("Work comp", {"text": r"work[- ]related injury"}))
        ctx = _ctx(documents=(_doc_text("Work Related Injury No"),))
        assert evaluate(registry, ctx) == []

    def test_text_still_matches_an_affirmative_field(self) -> None:
        registry = _registry(("Work comp", {"text": r"work[- ]related injury"}))
        ctx = _ctx(documents=(_doc_text("Work Related Injury Yes"),))
        assert [a.tag_name for a in evaluate(registry, ctx)] == ["Work comp"]

    def test_text_no_match_does_not_assign(self) -> None:
        registry = _registry(("Hospice", {"text": r"\bhospice\b"}))
        ctx = _ctx(documents=(_doc_text("ROUTINE TRANSPORT"),))
        assert evaluate(registry, ctx) == []

    def test_employer_field_ignores_the_agency_s_own_letterhead(self) -> None:
        # A `field` rule only ever sees the LLM's grounded employer_name
        # value -- unlike a `text` rule, it never sees the raw OCR page, so
        # the reporting agency's own letterhead (repeated on every page it
        # produces) simply isn't in its search space at all.
        registry = _registry(
            ("CAS Employee", {"field": "employer_name", "matches": "city ambulance"})
        )
        ctx = _ctx(
            fields={"employer_name": _ABSENT},  # LLM correctly left it blank
            documents=(_doc_text("Company City Ambulance Service Incident# 1 PCR# 1"),),
        )
        assert evaluate(registry, ctx) == []

    def test_employer_field_still_matches_a_real_employer(self) -> None:
        registry = _registry(
            ("CAS Employee", {"field": "employer_name", "matches": "city ambulance"})
        )
        ctx = _ctx(fields={"employer_name": _field("City Ambulance Service")})
        assert [a.tag_name for a in evaluate(registry, ctx)] == ["CAS Employee"]

    def test_field_matches_uses_grounded_confidence(self) -> None:
        registry = _registry(("911", {"field": "transport_priority", "matches": "P1"}))
        ctx = _ctx(fields={"transport_priority": _field("P1", confidence=0.7)})
        result = evaluate(registry, ctx)
        assert result[0].confidence == 0.7

    def test_field_present_false_fires_when_absent(self) -> None:
        registry = _registry(
            ("Invalid PCS Bit", {"field": "authorizing_physician", "present": False})
        )
        ctx = _ctx()
        result = evaluate(registry, ctx)
        assert [a.tag_name for a in result] == ["Invalid PCS Bit"]
        assert result[0].confidence == 1.0

    def test_field_source_matches_sop_default(self) -> None:
        registry = _registry(
            ("Invalid Pt Demos", {"field": "address_line1", "field_source": "sop_default"})
        )
        ctx = _ctx(fields={"address_line1": _field("CBO", source=FieldSource.SOP_DEFAULT)})
        assert [a.tag_name for a in evaluate(registry, ctx)] == ["Invalid Pt Demos"]

    def test_bariatric_requires_a_unit(self) -> None:
        registry = _registry(("Bariatric", {"field": "weight_kg", "bariatric": True}))
        assert evaluate(registry, _ctx(fields={"weight_kg": _field("320 lb")}))
        assert evaluate(registry, _ctx(fields={"weight_kg": _field("140 kg")}))
        assert evaluate(registry, _ctx(fields={"weight_kg": _field("320")})) == []

    def test_insurance_prefix_in(self) -> None:
        registry = _registry(
            (
                "BCBSTX HMO",
                {"insurance": {"slot": 0, "field": "policy_id", "prefix_in": ["ZGZ", "T2U"]}},
            )
        )
        ctx = ClaimContext(
            merged=MergedExtraction(
                fields={},
                insurance=(
                    _insurance_slot(policy_id=_field("ZGZ998877")),
                    _insurance_slot(),
                    _insurance_slot(),
                ),
                conflicts=(),
            ),
            doc_types_present=frozenset(),
            documents=(),
        )
        assert [a.tag_name for a in evaluate(registry, ctx)] == ["BCBSTX HMO"]

    def test_missing_doc_type_fires_when_absent(self) -> None:
        registry = _registry(("NO EPCR", {"missing_doc_type": "epcr"}))
        ctx = _ctx(doc_types=frozenset({DocumentType.HFS}))
        assert [a.tag_name for a in evaluate(registry, ctx)] == ["NO EPCR"]

    def test_has_doc_type_fires_when_present(self) -> None:
        registry = _registry(("Has EPCR", {"has_doc_type": "epcr"}))
        assert evaluate(registry, _ctx(doc_types=frozenset({DocumentType.EPCR}))) != []
        assert evaluate(registry, _ctx(doc_types=frozenset({DocumentType.HFS}))) == []

    def test_field_conflict_carries_both_documents_as_evidence(self) -> None:
        registry = _registry(("Pt Demos Correction", {"field_conflict": "dob"}))
        doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
        conflict = FieldConflict(
            field="dob", values_by_document=((doc_a, "1958-03-14"), (doc_b, "1985-03-14"))
        )
        result = evaluate(registry, _ctx(conflicts=(conflict,)))
        assert len(result[0].evidence) == 2

    def test_numeric_diff_fires_at_threshold(self) -> None:
        registry = _registry(
            (
                "QA/Mileage Review",
                {"numeric_diff": {"fields": ["actual_mileage", "actual_distance"], "min": 5}},
            )
        )
        ctx = _ctx(fields={"actual_mileage": _field("10"), "actual_distance": _field("16")})
        assert evaluate(registry, ctx) != []
        ctx = _ctx(fields={"actual_mileage": _field("10"), "actual_distance": _field("13")})
        assert evaluate(registry, ctx) == []


_CRANE_911_RULE = {
    "all": [
        {"field": "pickup_address", "matches": "crane"},
        {"field": "transport_priority", "matches": "P1"},
    ]
}


class TestCombinators:
    def test_all_requires_every_child(self) -> None:
        registry = _registry(("Crane + 911", _CRANE_911_RULE))
        ctx = _ctx(
            fields={
                "pickup_address": _field("123 Crane Rd"),
                "transport_priority": _field("P2"),
            }
        )
        assert evaluate(registry, ctx) == []
        ctx = _ctx(
            fields={
                "pickup_address": _field("123 Crane Rd"),
                "transport_priority": _field("P1"),
            }
        )
        assert evaluate(registry, ctx) != []

    def test_all_confidence_is_the_minimum_of_its_children(self) -> None:
        registry = _registry(("Crane + 911", _CRANE_911_RULE))
        ctx = _ctx(
            fields={
                "pickup_address": _field("123 Crane Rd", confidence=0.7),
                "transport_priority": _field("P1", confidence=1.0),
            }
        )
        assert evaluate(registry, ctx)[0].confidence == 0.7

    def test_any_short_circuits_on_first_match(self) -> None:
        registry = _registry(
            (
                "Non-Emergent",
                {"any": [{"field": "transport_priority", "matches": "P3"}, {"text": "morgue"}]},
            )
        )
        ctx = _ctx(
            fields={"transport_priority": _field("P3")},
            documents=(_doc_text("no keyword here"),),
        )
        assert [a.tag_name for a in evaluate(registry, ctx)] == ["Non-Emergent"]


_NON_EMERGENT_RULE = {
    "any": [
        {"field": "transport_priority", "matches": "P3"},
        {"field": "destination_facility", "matches": "morgue"},
        {"field": "requested_level_of_service", "matches": "wheelchair"},
    ]
}

_NO_PCS_RULE = {
    "all": [
        {"missing_doc_type": "pcs"},
        {"requires_tag": "Non-Emergent"},
        {"none": [{"field": "requested_level_of_service", "matches": "wheelchair"}]},
    ]
}


class TestRequiresTag:
    def test_fires_only_after_the_dependency_is_assigned(self) -> None:
        registry = _registry(
            ("Non-Emergent", _NON_EMERGENT_RULE),
            ("NO PCS", _NO_PCS_RULE),
        )
        ctx = _ctx(
            fields={"transport_priority": _field("P3")}, doc_types=frozenset({DocumentType.EPCR})
        )
        names = [a.tag_name for a in evaluate(registry, ctx)]
        assert names == ["Non-Emergent", "NO PCS"]

    def test_does_not_fire_when_dependency_is_absent(self) -> None:
        registry = _registry(
            ("Non-Emergent", _NON_EMERGENT_RULE),
            ("NO PCS", _NO_PCS_RULE),
        )
        ctx = _ctx(
            fields={"transport_priority": _field("P1")}, doc_types=frozenset({DocumentType.EPCR})
        )
        assert [a.tag_name for a in evaluate(registry, ctx)] == []


class TestPhase8Rules:
    def test_non_emergent_fires_on_wheelchair_service_level(self) -> None:
        # Phase 8 item 1: requested_level_of_service = wheelchair
        registry = _registry(("Non-Emergent", _NON_EMERGENT_RULE))
        ctx = _ctx(fields={"requested_level_of_service": _field("Wheelchair")})
        assert [a.tag_name for a in evaluate(registry, ctx)] == ["Non-Emergent"]

    def test_non_emergent_still_fires_on_p3(self) -> None:
        registry = _registry(("Non-Emergent", _NON_EMERGENT_RULE))
        ctx = _ctx(fields={"transport_priority": _field("P3")})
        assert [a.tag_name for a in evaluate(registry, ctx)] == ["Non-Emergent"]

    def test_no_pcs_does_not_fire_on_wheelchair_claim(self) -> None:
        # Knock-on guard: wheelchair is exempt from PCS requirement (billing SOP)
        registry = _registry(
            ("Non-Emergent", _NON_EMERGENT_RULE),
            ("NO PCS", _NO_PCS_RULE),
        )
        ctx = _ctx(
            fields={"requested_level_of_service": _field("Wheelchair")},
            doc_types=frozenset({DocumentType.EPCR}),
        )
        names = [a.tag_name for a in evaluate(registry, ctx)]
        assert "Non-Emergent" in names
        assert "NO PCS" not in names

    def test_no_pcs_still_fires_on_non_wheelchair_non_emergent(self) -> None:
        registry = _registry(
            ("Non-Emergent", _NON_EMERGENT_RULE),
            ("NO PCS", _NO_PCS_RULE),
        )
        ctx = _ctx(
            fields={"transport_priority": _field("P3")},
            doc_types=frozenset({DocumentType.EPCR}),
        )
        names = [a.tag_name for a in evaluate(registry, ctx)]
        assert "Non-Emergent" in names
        assert "NO PCS" in names

    def test_pt_demos_correction_fires_on_age_conflict(self) -> None:
        # Phase 8 item 2: age conflict (billing SOP)
        registry = _registry(
            (
                "Pt Demos Correction",
                {
                    "any": [
                        {"field_conflict": "last_name"},
                        {"field_conflict": "first_name"},
                        {"field_conflict": "dob"},
                        {"field_conflict": "gender"},
                        {"field_conflict": "age"},
                    ]
                },
            )
        )
        conflict = FieldConflict(
            field="age",
            values_by_document=((uuid.uuid4(), "72"), (uuid.uuid4(), "73")),
        )
        ctx = _ctx(conflicts=(conflict,))
        assert [a.tag_name for a in evaluate(registry, ctx)] == ["Pt Demos Correction"]

    def test_pt_demos_correction_does_not_fire_without_conflict(self) -> None:
        registry = _registry(
            (
                "Pt Demos Correction",
                {
                    "any": [
                        {"field_conflict": "last_name"},
                        {"field_conflict": "age"},
                    ]
                },
            )
        )
        ctx = _ctx()
        assert evaluate(registry, ctx) == []


class TestDocTypesComplete:
    def test_missing_doc_type_suppressed_when_types_incomplete(self) -> None:
        registry = _registry(("NO EPCR", {"missing_doc_type": "epcr"}))
        ctx = _ctx(doc_types=frozenset({DocumentType.HFS}), doc_types_complete=False)
        assert evaluate(registry, ctx) == []

    def test_non_doc_type_rule_still_fires_when_types_incomplete(self) -> None:
        registry = _registry(
            ("NO EPCR", {"missing_doc_type": "epcr"}),
            ("911", {"field": "transport_priority", "matches": "P1"}),
        )
        ctx = _ctx(
            fields={"transport_priority": _field("P1")},
            doc_types=frozenset({DocumentType.HFS}),
            doc_types_complete=False,
        )
        assert [a.tag_name for a in evaluate(registry, ctx)] == ["911"]


class TestDeterminism:
    def test_same_input_yields_identical_output(self) -> None:
        registry = _registry(
            ("Hospice", {"text": r"\bhospice\b"}),
            ("Dialysis", {"text": r"\bdialysis\b"}),
        )
        ctx = _ctx(documents=(_doc_text("hospice patient, on dialysis"),))
        first = evaluate(registry, ctx)
        second = evaluate(registry, ctx)
        assert [(a.tag_name, a.confidence, a.evidence) for a in first] == [
            (a.tag_name, a.confidence, a.evidence) for a in second
        ]


class TestEvidenceIsRedacted:
    def test_a_tag_assignment_serialized_into_a_log_event_is_scrubbed(self) -> None:
        registry = _registry(("Hospice", {"text": r"\bhospice\b"}))
        ctx = _ctx(documents=(_doc_text("PATIENT TESTERSON IS HOSPICE"),))
        assignment = evaluate(registry, ctx)[0]
        event = {
            "event": "claim_tags_assigned",
            "evidence": [
                {"kind": e.kind, "field": e.field, "snippet": e.snippet}
                for e in assignment.evidence
            ],
        }
        scrubbed = dict(redact_processor(None, "info", event))
        assert scrubbed["evidence"] == REDACTED
        assert "TESTERSON" not in str(scrubbed)
