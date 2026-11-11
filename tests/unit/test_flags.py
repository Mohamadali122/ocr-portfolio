"""Advisory review flags (`app/services/flags.py`).

Each rule quotes an unambiguous prebilling-SOP line; the ambiguous ones
(MCR Age, MCD Minor) are deliberately absent pending PREBILLER_QUESTIONS.md.
"""

from app.schemas.extraction import (
    ExtractedField,
    FieldSource,
    InsuranceSlot,
    PatientExtraction,
)
from app.services.flags import suggest


def _codes(fields: PatientExtraction) -> set[str]:
    return {flag.code for flag in suggest(fields)}


def _typed(value: str) -> ExtractedField:
    return ExtractedField(value=value, confidence=1.0, typed_value=value)


class TestSuggest:
    def test_clean_extraction_raises_no_flags(self) -> None:
        assert suggest(PatientExtraction()) == []

    def test_cbo_address_default_flags(self) -> None:
        fields = PatientExtraction(
            address_line1=ExtractedField(typed_value="CBO", source=FieldSource.SOP_DEFAULT)
        )
        assert _codes(fields) == {"cbo_address_used"}

    def test_alias_name_pair_flags(self) -> None:
        fields = PatientExtraction(first_name=_typed("Jane"), last_name=_typed("Doe"))
        assert _codes(fields) == {"alias_name_detected"}

    def test_real_name_does_not_flag(self) -> None:
        fields = PatientExtraction(first_name=_typed("Jane"), last_name=_typed("Testerson"))
        assert _codes(fields) == set()

    def test_secondary_slot_without_policy_flags(self) -> None:
        fields = PatientExtraction(
            insurance=[
                InsuranceSlot(insurance_name=_typed("Aetna"), policy_id=_typed("W12345")),
                InsuranceSlot(insurance_name=_typed("Medicaid of Texas")),
            ]
        )
        assert _codes(fields) == {"secondary_slot_missing_policy"}

    def test_primary_slot_without_policy_does_not_flag(self) -> None:
        # SOP: "if it's primary, we add it normally" (billing SOP).
        fields = PatientExtraction(insurance=[InsuranceSlot(insurance_name=_typed("Aetna"))])
        assert _codes(fields) == set()

    def test_bcbs_hmo_prefix_on_primary_flags(self) -> None:
        fields = PatientExtraction(
            insurance=[
                InsuranceSlot(insurance_name=_typed("BCBS of Texas"), policy_id=_typed("ZGZ123456"))
            ]
        )
        assert _codes(fields) == {"bcbstx_hmo_prefix"}

    def test_bcbs_hmo_prefix_on_secondary_does_not_flag(self) -> None:
        # SOP scopes the tag to BCBS as *primary* insurance.
        fields = PatientExtraction(
            insurance=[
                InsuranceSlot(insurance_name=_typed("Aetna"), policy_id=_typed("W1")),
                InsuranceSlot(
                    insurance_name=_typed("BCBS of Texas"), policy_id=_typed("ZGZ123456")
                ),
            ]
        )
        assert _codes(fields) == set()

    def test_plan_map_group_flags(self) -> None:
        fields = PatientExtraction(
            insurance=[
                InsuranceSlot(
                    insurance_name=_typed("Aetna"),
                    policy_id=_typed("W1"),
                    group_number=_typed("000003-TX"),
                )
            ]
        )
        assert _codes(fields) == {"plan_map_or_marketplace_group"}

    def test_supplement_in_name_flags(self) -> None:
        fields = PatientExtraction(
            insurance=[
                InsuranceSlot(
                    insurance_name=_typed("AARP Medicare Supplement"), policy_id=_typed("1")
                )
            ]
        )
        assert _codes(fields) == {"supplement_never_primary"}

    def test_mileage_discrepancy_of_five_or_more_flags(self) -> None:
        fields = PatientExtraction(actual_mileage=_typed("12.4"), actual_distance=_typed("18 mi"))
        assert _codes(fields) == {"mileage_discrepancy"}

    def test_mileage_within_tolerance_does_not_flag(self) -> None:
        fields = PatientExtraction(actual_mileage=_typed("12.4"), actual_distance=_typed("14"))
        assert _codes(fields) == set()

    def test_bariatric_weight_needs_a_unit(self) -> None:
        assert _codes(PatientExtraction(weight_kg=_typed("320 lb"))) == {"bariatric_weight"}
        assert _codes(PatientExtraction(weight_kg=_typed("140 kg"))) == {"bariatric_weight"}
        # A bare number is ambiguous -- never flags.
        assert _codes(PatientExtraction(weight_kg=_typed("320"))) == set()
        assert _codes(PatientExtraction(weight_kg=_typed("180 lb"))) == set()

    def test_details_carry_no_document_values(self) -> None:
        # Same doctrine as Verdict: field names and slot indices only.
        fields = PatientExtraction(
            first_name=_typed("Jane"),
            last_name=_typed("Doe"),
            insurance=[
                InsuranceSlot(insurance_name=_typed("Aetna"), policy_id=_typed("W99887")),
                InsuranceSlot(insurance_name=_typed("SecretPayor Supplement")),
            ],
        )
        rendered = " ".join(flag.detail for flag in suggest(fields))
        for value in ("Jane", "Doe", "W99887", "SecretPayor"):
            assert value not in rendered
