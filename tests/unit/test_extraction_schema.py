"""Local models routinely emit a bare `"field": null` for an empty field
instead of the `{"value": null, "source_page": null}` object shape a
`RawExtractedField`-typed field actually requires. Observed live as a
`model_type` validation error on every empty field simultaneously -- a
deterministic prompt reproducing a systematic model habit, not a one-off
glitch, so it survived the one repair retry too. `_coerce_bare_null_fields`
(app/schemas/extraction.py) is the fix; these tests pin that behavior at the
schema level, independent of `ExtractionService`'s grounding/repair logic.
"""

from app.schemas.extraction import RawInsuranceSlot, RawPatientExtraction


class TestBareNullCoercion:
    def test_bare_null_on_a_top_level_field_validates_as_an_empty_field(self) -> None:
        raw = RawPatientExtraction.model_validate_json(
            '{"last_name": {"value": "DOE"}, "suffix": null}'
        )
        assert raw.last_name.value == "DOE"
        assert raw.suffix.value is None

    def test_bare_null_on_every_empty_field_still_validates(self) -> None:
        # The actual live failure: ~46 fields at once, all bare null.
        payload = '{"last_name": {"value": "DOE"}, "email": null, "mrn": null, "ethnicity": null}'
        raw = RawPatientExtraction.model_validate_json(payload)
        assert raw.email.value is None
        assert raw.mrn.value is None
        assert raw.ethnicity.value is None

    def test_bare_null_inside_an_insurance_slot_validates(self) -> None:
        raw = RawPatientExtraction.model_validate_json(
            '{"insurance": [{"insurance_name": {"value": "BCBS"}, "subscriber_name": null}]}'
        )
        assert raw.insurance[0].insurance_name.value == "BCBS"
        assert raw.insurance[0].subscriber_name.value is None

    def test_insurance_slot_validated_standalone_also_coerces(self) -> None:
        slot = RawInsuranceSlot.model_validate_json('{"policy_id": null}')
        assert slot.policy_id.value is None

    def test_a_present_object_shaped_field_is_unaffected(self) -> None:
        # The common/correct shape must keep working -- this fix only widens
        # what's accepted, it never changes how a well-formed field is read.
        raw = RawPatientExtraction.model_validate_json(
            '{"last_name": {"value": "DOE", "source_page": 1}}'
        )
        assert raw.last_name.value == "DOE"
        assert raw.last_name.source_page == 1

    def test_a_missing_field_key_still_defaults_normally(self) -> None:
        # Absent (not even a null key) is the other common shape and must
        # keep taking the field's ordinary default, untouched by coercion.
        raw = RawPatientExtraction.model_validate_json('{"last_name": {"value": "DOE"}}')
        assert raw.suffix.value is None
