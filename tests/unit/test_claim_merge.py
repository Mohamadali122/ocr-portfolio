import uuid

from app.models.document import DocumentType
from app.models.extraction import Extraction
from app.schemas.extraction import (
    ExtractedField,
    FieldSource,
    InsuranceSlot,
    PatientExtraction,
)
from app.services.claim_merge import MemberDocument, merge


def _field(
    value: str, *, confidence: float = 1.0, source: FieldSource = FieldSource.GROUNDED
) -> ExtractedField:
    return ExtractedField(value=value, typed_value=value, confidence=confidence, source=source)


def _extraction(document_id: uuid.UUID, **fields: object) -> Extraction:
    pe = PatientExtraction(**fields)
    return Extraction(
        document_id=document_id,
        fields=pe.model_dump(mode="json"),
        llm_model="fake-llm/1.0",
        duration_ms=1,
    )


def _member(position: int, doc_type: DocumentType, extraction: Extraction | None) -> MemberDocument:
    document_id = extraction.document_id if extraction is not None else uuid.uuid4()
    return MemberDocument(
        position=position, document_id=document_id, doc_type=doc_type, extraction=extraction
    )


class TestMerge:
    def test_demographic_field_prefers_hfs_over_epcr(self) -> None:
        hfs_id, epcr_id = uuid.uuid4(), uuid.uuid4()
        hfs = _extraction(hfs_id, last_name=_field("Testerson"))
        epcr = _extraction(epcr_id, last_name=_field("Wojceehowski"))  # OCR garble
        result = merge([_member(0, DocumentType.HFS, hfs), _member(1, DocumentType.EPCR, epcr)])
        merged = result.fields["last_name"]
        assert merged.typed_value == "Testerson"
        assert merged.document_id == hfs_id

    def test_trip_field_prefers_epcr_over_hfs(self) -> None:
        hfs_id, epcr_id = uuid.uuid4(), uuid.uuid4()
        hfs = _extraction(hfs_id, run_number=_field("WRONG"))
        epcr = _extraction(epcr_id, run_number=_field("R-1024"))
        result = merge([_member(0, DocumentType.HFS, hfs), _member(1, DocumentType.EPCR, epcr)])
        merged = result.fields["run_number"]
        assert merged.typed_value == "R-1024"
        assert merged.document_id == epcr_id

    def test_corrected_field_outranks_precedence(self) -> None:
        # A reviewer's correction lives on the EPCR row even though HFS would
        # normally win demographic fields -- it must still win.
        hfs_id, epcr_id = uuid.uuid4(), uuid.uuid4()
        hfs = _extraction(hfs_id, last_name=_field("Smith"))
        epcr = _extraction(
            epcr_id, last_name=_field("Smyth", confidence=0.0, source=FieldSource.CORRECTED)
        )
        result = merge([_member(0, DocumentType.HFS, hfs), _member(1, DocumentType.EPCR, epcr)])
        assert result.fields["last_name"].typed_value == "Smyth"
        assert result.fields["last_name"].source is FieldSource.CORRECTED

    def test_real_value_from_another_document_beats_an_sop_default(self) -> None:
        # HFS wins address_line1 by precedence, but HFS itself never mentions
        # an address -- normalization.py falls back to the CBO placeholder,
        # source=SOP_DEFAULT. The EPCR has a real, page-grounded address.
        # SOP_DEFAULT is not information -- it must lose to genuine info from
        # anywhere else in the same claim, precedence notwithstanding.
        hfs_id, epcr_id = uuid.uuid4(), uuid.uuid4()
        hfs = _extraction(
            hfs_id,
            address_line1=_field("CBO", confidence=0.0, source=FieldSource.SOP_DEFAULT),
        )
        epcr = _extraction(epcr_id, address_line1=_field("123 Main St"))
        result = merge([_member(0, DocumentType.HFS, hfs), _member(1, DocumentType.EPCR, epcr)])
        merged = result.fields["address_line1"]
        assert merged.typed_value == "123 Main St"
        assert merged.document_id == epcr_id

    def test_sop_default_is_used_when_it_is_the_only_value_anywhere(self) -> None:
        # No member document has a real address -- the SOP default is all
        # there is, and it must still come through.
        hfs = _extraction(
            uuid.uuid4(),
            address_line1=_field("CBO", confidence=0.0, source=FieldSource.SOP_DEFAULT),
        )
        result = merge([_member(0, DocumentType.HFS, hfs)])
        merged = result.fields["address_line1"]
        assert merged.typed_value == "CBO"
        assert merged.source is FieldSource.SOP_DEFAULT

    def test_tiebreak_on_confidence_then_position(self) -> None:
        # Two documents of the same precedence rank (both EPCR) disagree on a
        # trip field -- higher grounding confidence wins.
        a_id, b_id = uuid.uuid4(), uuid.uuid4()
        a = _extraction(a_id, unit_number=_field("12", confidence=0.7))
        b = _extraction(b_id, unit_number=_field("21", confidence=1.0))
        result = merge([_member(0, DocumentType.EPCR, a), _member(1, DocumentType.EPCR, b)])
        assert result.fields["unit_number"].typed_value == "21"
        assert result.fields["unit_number"].document_id == b_id

    def test_hfs_and_epcr_disagreeing_on_dob_is_a_conflict(self) -> None:
        hfs = _extraction(uuid.uuid4(), dob=_field("1958-03-14"))
        epcr = _extraction(uuid.uuid4(), dob=_field("1985-03-14"))
        result = merge([_member(0, DocumentType.HFS, hfs), _member(1, DocumentType.EPCR, epcr)])
        conflict_fields = {c.field for c in result.conflicts}
        assert "dob" in conflict_fields

    def test_agreeing_documents_produce_no_conflict(self) -> None:
        hfs = _extraction(uuid.uuid4(), dob=_field("1958-03-14"))
        epcr = _extraction(uuid.uuid4(), dob=_field("1958-03-14"))
        result = merge([_member(0, DocumentType.HFS, hfs), _member(1, DocumentType.EPCR, epcr)])
        assert result.conflicts == ()

    def test_document_with_no_extraction_contributes_no_fields(self) -> None:
        hfs = _extraction(uuid.uuid4(), last_name=_field("Doe"))
        result = merge([_member(0, DocumentType.HFS, hfs), _member(1, DocumentType.EPCR, None)])
        assert result.fields["last_name"].typed_value == "Doe"

    def test_field_absent_everywhere_stays_absent(self) -> None:
        result = merge([_member(0, DocumentType.HFS, _extraction(uuid.uuid4()))])
        merged = result.fields["last_name"]
        assert merged.typed_value is None
        assert merged.source is FieldSource.ABSENT

    def test_insurance_prefers_hfs_and_grounds_slot_index(self) -> None:
        hfs_id, epcr_id = uuid.uuid4(), uuid.uuid4()
        hfs = _extraction(
            hfs_id,
            insurance=[InsuranceSlot(insurance_name=_field("Aetna"), policy_id=_field("W1"))],
        )
        epcr = _extraction(
            epcr_id,
            insurance=[InsuranceSlot(insurance_name=_field("Wrong Payor"))],
        )
        result = merge([_member(0, DocumentType.HFS, hfs), _member(1, DocumentType.EPCR, epcr)])
        assert result.insurance[0].insurance_name.typed_value == "Aetna"
        assert result.insurance[0].policy_id.typed_value == "W1"

    def test_missing_insurance_slot_on_one_document_does_not_error(self) -> None:
        hfs = _extraction(uuid.uuid4(), insurance=[])
        result = merge([_member(0, DocumentType.HFS, hfs)])
        assert result.insurance[0].insurance_name.typed_value is None
