import pytest

from app.models.document import DocumentType
from app.schemas.extraction import ExtractedField, FieldSource, PatientExtraction
from app.services.extraction_validation import evaluate


def _extraction(**fields: ExtractedField) -> PatientExtraction:
    return PatientExtraction(**fields)


class TestEvaluate:
    def test_empty_extraction_fails(self) -> None:
        # The exact FakeLlmProvider("{}") shape: nothing grounded anywhere.
        verdict = evaluate(DocumentType.EPCR, _extraction(), min_confidence=0.5)
        assert verdict.passed is False
        assert "last_name" in verdict.missing_fields

    def test_all_required_fields_present_and_grounded_passes(self) -> None:
        fields = _extraction(
            last_name=ExtractedField(typed_value="DOE", confidence=1.0),
            first_name=ExtractedField(typed_value="JANE", confidence=1.0),
            run_number=ExtractedField(typed_value="12345", confidence=1.0),
            date_of_service=ExtractedField(typed_value="06/28/2026", confidence=0.7),
        )
        verdict = evaluate(DocumentType.EPCR, fields, min_confidence=0.5)
        assert verdict.passed is True
        assert verdict.missing_fields == ()

    def test_one_missing_required_field_fails_even_if_others_are_grounded(self) -> None:
        fields = _extraction(
            last_name=ExtractedField(typed_value="DOE", confidence=1.0),
            first_name=ExtractedField(typed_value="JANE", confidence=1.0),
            # run_number and date_of_service both absent.
        )
        verdict = evaluate(DocumentType.EPCR, fields, min_confidence=0.5)
        assert verdict.passed is False
        assert set(verdict.missing_fields) == {"run_number", "date_of_service"}

    def test_sop_default_satisfies_presence_but_fails_confidence(self) -> None:
        # normalize()'s SOP alias/DOB-default path: typed_value is fabricated,
        # not read off the page, so it carries the original 0.0 confidence
        # forward. HFS requires dob -- a document with no DOB anywhere still
        # gets "01/01/1900" here, and must not pass on that alone.
        fields = _extraction(
            last_name=ExtractedField(typed_value="DOE", confidence=1.0),
            first_name=ExtractedField(typed_value="JANE", confidence=1.0),
            dob=ExtractedField(typed_value="01/01/1900", confidence=0.0),
        )
        verdict = evaluate(DocumentType.HFS, fields, min_confidence=0.5)
        # Gated on the minimum, not the mean -- two grounded fields (1.0) and
        # one fabricated one (0.0) average to 0.67, well above the bar, but
        # the fabricated field alone must still sink it.
        assert verdict.passed is False
        assert verdict.missing_fields == ()  # present, just low-confidence

    def test_an_unrelated_optional_fields_sop_default_does_not_drag_down_the_gate(self) -> None:
        # address_line1's CBO default is common on EPCRs (they rarely mention
        # an address) and is not in EPCR's required set -- it must not sink an
        # otherwise fully-grounded extraction.
        fields = _extraction(
            last_name=ExtractedField(typed_value="DOE", confidence=1.0),
            first_name=ExtractedField(typed_value="JANE", confidence=1.0),
            run_number=ExtractedField(typed_value="12345", confidence=1.0),
            date_of_service=ExtractedField(typed_value="06/28/2026", confidence=1.0),
            address_line1=ExtractedField(typed_value="CBO", confidence=0.0),
        )
        verdict = evaluate(DocumentType.EPCR, fields, min_confidence=0.5)
        assert verdict.passed is True

    def test_fuzzy_grounded_fields_clear_the_default_threshold(self) -> None:
        # The hallucination guard's fuzzy-match tier (0.7) must not be treated
        # as fabricated -- only the true absent/SOP-default 0.0 tier should.
        fields = _extraction(
            last_name=ExtractedField(typed_value="DOE", confidence=0.7),
            first_name=ExtractedField(typed_value="JANE", confidence=0.7),
        )
        verdict = evaluate(DocumentType.BOOKING_SUMMARY, fields, min_confidence=0.5)
        assert verdict.passed is True

    def test_min_confidence_reports_the_minimum_required_field_confidence(self) -> None:
        # Two grounded fields (1.0) and one fabricated one (0.0) -- the report
        # must surface the minimum (what the gate decides on), not the mean
        # (which would hide the fabricated field behind the others).
        fields = _extraction(
            last_name=ExtractedField(typed_value="DOE", confidence=1.0),
            first_name=ExtractedField(typed_value="JANE", confidence=1.0),
            dob=ExtractedField(typed_value="01/01/1900", confidence=0.0),
        )
        verdict = evaluate(DocumentType.HFS, fields, min_confidence=0.5)
        assert verdict.min_confidence == pytest.approx(0.0)

    def test_unknown_doc_type_falls_back_to_the_unknown_required_set(self) -> None:
        fields = _extraction(
            last_name=ExtractedField(typed_value="DOE", confidence=1.0),
            first_name=ExtractedField(typed_value="JANE", confidence=1.0),
        )
        verdict = evaluate(DocumentType.UNKNOWN, fields, min_confidence=0.5)
        assert verdict.passed is True


class TestSopDefaultTreatedAsMissing:
    """X3: a required field filled by an SOP default must not appear present."""

    def test_sop_default_last_name_counts_as_missing(self) -> None:
        # normalization fills last_name with "UNKNOWN" at SOP_DEFAULT when no
        # name was found. The gate used to see typed_value="UNKNOWN" and count
        # it as present, reporting missing=first_name instead of both names.
        fields = _extraction(
            last_name=ExtractedField(
                typed_value="UNKNOWN", confidence=0.0, source=FieldSource.SOP_DEFAULT
            ),
        )
        verdict = evaluate(DocumentType.HFS, fields, min_confidence=0.5)
        assert verdict.passed is False
        assert "last_name" in verdict.missing_fields
        assert "first_name" in verdict.missing_fields

    def test_grounded_name_is_not_treated_as_missing(self) -> None:
        fields = _extraction(
            last_name=ExtractedField(
                typed_value="DOE", confidence=1.0, source=FieldSource.GROUNDED
            ),
            first_name=ExtractedField(
                typed_value="JANE", confidence=1.0, source=FieldSource.GROUNDED
            ),
            dob=ExtractedField(
                typed_value="04/17/1958", confidence=1.0, source=FieldSource.REFORMATTED
            ),
        )
        verdict = evaluate(DocumentType.HFS, fields, min_confidence=0.5)
        assert verdict.passed is True
        assert verdict.missing_fields == ()

    def test_sop_default_dob_counts_as_missing(self) -> None:
        # The 01/01/1900 DOB default is SOP_DEFAULT -- an HFS with a real name
        # but no DOB should report missing=dob, not pass on the placeholder.
        fields = _extraction(
            last_name=ExtractedField(
                typed_value="DOE", confidence=1.0, source=FieldSource.GROUNDED
            ),
            first_name=ExtractedField(
                typed_value="JANE", confidence=1.0, source=FieldSource.GROUNDED
            ),
            dob=ExtractedField(
                typed_value="01/01/1900", confidence=0.0, source=FieldSource.SOP_DEFAULT
            ),
        )
        verdict = evaluate(DocumentType.HFS, fields, min_confidence=0.5)
        assert verdict.passed is False
        assert "dob" in verdict.missing_fields
