from datetime import date

from app.schemas.extraction import ExtractedField, FieldSource, InsuranceSlot, PatientExtraction
from app.services.normalization import (
    normalize,
    normalize_dob,
    normalize_email,
    normalize_insurance_id,
    normalize_name,
    normalize_phone,
    normalize_ssn,
    normalize_suffix,
    normalize_zip,
    parse_dob,
    searchable_dob,
    searchable_name,
)

_REFERENCE = date(2026, 7, 28)  # pins the century-boundary tests below in time


class TestDob:
    def test_epcr_format_is_reformatted_to_mmddyyyy(self) -> None:
        assert normalize_dob("1958/04/17", default=None) == ("04/17/1958", FieldSource.REFORMATTED)

    def test_already_mmddyyyy_is_grounded(self) -> None:
        assert normalize_dob("04/17/1958", default=None) == ("04/17/1958", FieldSource.GROUNDED)

    def test_single_digit_month_day_parses(self) -> None:
        assert normalize_dob("4/7/1958", default=None)[0] == "04/07/1958"

    def test_two_digit_year_below_pivot_parses_as_20xx(self) -> None:
        # No age, no signal either way plausible (2025 age 1, 1925 age 101,
        # both plausible against `_REFERENCE`) -- falls back to the POSIX
        # convention: 00-68 -> 2000-2068. A 2026-dated packet's infant
        # patient born "25" is correctly 2025 either way.
        result = normalize_dob("09/27/25", default=None, reference_date=_REFERENCE)
        assert result == ("09/27/2025", FieldSource.REFORMATTED)

    def test_two_digit_year_above_pivot_parses_as_19xx(self) -> None:
        # 1975 (age 51) is plausible; 2075 (age -49) is not -- the window
        # resolves it correctly on its own, no POSIX fallback needed here.
        result = normalize_dob("03/05/75", default=None, reference_date=_REFERENCE)
        assert result == ("03/05/1975", FieldSource.REFORMATTED)

    def test_two_digit_year_with_age_resolves_the_century(self) -> None:
        # "12/19/53" is genuinely ambiguous by the window alone in some years,
        # but a printed age of 81 (as of 2026-07-28) pins it to 1953 --
        # confirmed live against a real document during this session.
        result = normalize_dob("12/19/53", default=None, reference_date=_REFERENCE, age="81")
        assert result == ("12/19/1953", FieldSource.REFORMATTED)

    def test_garbled_age_that_fails_its_own_check_falls_back_to_the_window(self) -> None:
        # age="3" is an OCR misread (should have been "38" or similar): taken
        # at face value it would imply birth year 2023, closer to the 2053
        # candidate, which makes the patient -27 at the reference date --
        # impossible, so the age signal is discarded and the window heuristic
        # (only 1953 is plausible) decides instead.
        result = normalize_dob("12/19/53", default=None, reference_date=_REFERENCE, age="3")
        assert result == ("12/19/1953", FieldSource.REFORMATTED)

    def test_two_digit_year_both_centuries_plausible_falls_back_to_posix(self) -> None:
        # Constructed so both candidates land inside the plausible window
        # (ages 5 and 105) -- no signal disambiguates, so the POSIX
        # convention decides, same as it would have before this change.
        two_digit = (_REFERENCE.year - 5) % 100
        raw = f"06/15/{two_digit:02d}"
        expected_year = 2000 + two_digit if two_digit <= 68 else 1900 + two_digit
        result = normalize_dob(raw, default=None, reference_date=_REFERENCE)
        assert result == (f"06/15/{expected_year}", FieldSource.REFORMATTED)

    def test_two_digit_year_with_no_reference_date_uses_today(self) -> None:
        # No `reference_date` passed at all -- falls back to `date.today()`.
        # Constructed relative to today so the assertion holds regardless of
        # which real date the suite runs on: the "wrong" century candidate is
        # always ~100 years off from a chosen age of 30, so only one is ever
        # plausible.
        thirty_years_ago = date.today().year - 30
        two_digit = thirty_years_ago % 100
        result = normalize_dob(f"06/15/{two_digit:02d}", default=None)
        assert result == (f"06/15/{thirty_years_ago}", FieldSource.REFORMATTED)

    def test_guarantor_dob_never_receives_an_age_signal(self) -> None:
        # normalize_dob has no `age=` default -- calling it the way
        # normalize() calls it for guarantor_dob (no age= at all) must still
        # resolve the century via the window/POSIX fallback alone.
        result = normalize_dob("12/19/53", default=None, reference_date=_REFERENCE)
        assert result == ("12/19/1953", FieldSource.REFORMATTED)

    def test_absent_with_default_emits_sop_default(self) -> None:
        assert normalize_dob(None, default="01/01/1900") == ("01/01/1900", FieldSource.SOP_DEFAULT)

    def test_absent_without_default_is_absent(self) -> None:
        assert normalize_dob("  ", default=None) == (None, FieldSource.ABSENT)

    def test_unparseable_is_kept_verbatim(self) -> None:
        assert normalize_dob("sometime 1958", default="01/01/1900") == (
            "sometime 1958",
            FieldSource.GROUNDED,
        )

    def test_parse_dob_handles_both_orderings(self) -> None:
        assert parse_dob("1958/04/17") == date(1958, 4, 17)
        assert parse_dob("04/17/1958") == date(1958, 4, 17)
        assert parse_dob(None) is None
        assert parse_dob("not a date") is None

    def test_epcr_compact_yyyymmdd_parses(self) -> None:
        # billing SOP: "The DOB in EPCR is YYYYMMDD".
        assert normalize_dob("19580417", default=None) == ("04/17/1958", FieldSource.REFORMATTED)


class TestSearchableDob:
    def test_sop_default_is_excluded(self) -> None:
        assert searchable_dob("01/01/1900", FieldSource.SOP_DEFAULT) is None

    def test_absent_is_excluded(self) -> None:
        assert searchable_dob(None, FieldSource.ABSENT) is None

    def test_skipped_is_excluded(self) -> None:
        assert searchable_dob(None, FieldSource.SKIPPED) is None

    def test_grounded_value_is_parsed(self) -> None:
        assert searchable_dob("04/17/1958", FieldSource.GROUNDED) == date(1958, 4, 17)

    def test_reformatted_value_is_parsed(self) -> None:
        assert searchable_dob("04/17/1958", FieldSource.REFORMATTED) == date(1958, 4, 17)

    def test_corrected_value_is_parsed(self) -> None:
        # A reviewer's correction -- `value` never changes on a correction,
        # so this must be read from `typed_value` to reflect it at all.
        assert searchable_dob("04/17/1958", FieldSource.CORRECTED) == date(1958, 4, 17)


class TestSearchableName:
    def test_sop_default_alias_is_excluded(self) -> None:
        # "UNKNOWN" (the no-name alias) must not index into the name search.
        assert searchable_name("UNKNOWN", FieldSource.SOP_DEFAULT) is None

    def test_absent_is_excluded(self) -> None:
        assert searchable_name(None, FieldSource.ABSENT) is None

    def test_skipped_is_excluded(self) -> None:
        assert searchable_name(None, FieldSource.SKIPPED) is None

    def test_grounded_value_passes_through(self) -> None:
        assert searchable_name("DOE", FieldSource.GROUNDED) == "DOE"

    def test_reformatted_value_passes_through(self) -> None:
        assert searchable_name("DOE", FieldSource.REFORMATTED) == "DOE"

    def test_corrected_value_passes_through(self) -> None:
        assert searchable_name("DOE", FieldSource.CORRECTED) == "DOE"


class TestName:
    def test_comma_form_is_split(self) -> None:
        assert normalize_name("TESTERSON, JANE", None, None) == ("TESTERSON", "JANE", None)

    def test_middle_name_is_dropped(self) -> None:
        assert normalize_name("DOE", "JANE MARIE", None) == ("DOE", "JANE", None)

    def test_trailing_suffix_is_pulled_from_first(self) -> None:
        assert normalize_name("DOE", "JOHN JR", None) == ("DOE", "JOHN", "JR")

    def test_separate_fields_pass_through(self) -> None:
        assert normalize_name("DOE", "JANE", "III") == ("DOE", "JANE", "III")

    def test_comma_form_is_split_even_when_first_name_also_extracted(self) -> None:
        # HFS prints "Last, First"; the model routinely fills both fields. The
        # typed last name must not keep ", JANE" glued on.
        assert normalize_name("TESTERSON, JANE", "JANE", None) == (
            "TESTERSON",
            "JANE",
            None,
        )

    def test_comma_form_with_unrelated_first_name_is_left_alone(self) -> None:
        # If the comma'd remainder is NOT the extracted first name, splitting
        # would guess -- keep the grounded value and let a reviewer decide.
        assert normalize_name("SMITH, ROBERT", "JANE", None) == ("SMITH, ROBERT", "JANE", None)


class TestSuffix:
    def test_allowed_suffix_is_canonicalized(self) -> None:
        assert normalize_suffix("jr.") == ("Jr", FieldSource.REFORMATTED)
        assert normalize_suffix("III") == ("III", FieldSource.GROUNDED)

    def test_disallowed_suffix_is_dropped(self) -> None:
        assert normalize_suffix("Esq") == (None, FieldSource.ABSENT)


class TestZip:
    def test_zip_plus_four_is_truncated(self) -> None:
        assert normalize_zip("77002-1234") == ("77002", FieldSource.REFORMATTED)

    def test_five_digit_zip_is_grounded(self) -> None:
        assert normalize_zip("77002") == ("77002", FieldSource.GROUNDED)

    def test_sentinel_zip_is_skipped(self) -> None:
        # "99999" is a placeholder forms use for "unknown", not a real zip.
        assert normalize_zip("99999") == (None, FieldSource.SKIPPED)

    def test_short_fragment_is_skipped_not_entered(self) -> None:
        # A truncated OCR read is not an enterable zip.
        assert normalize_zip("1234") == (None, FieldSource.SKIPPED)


class TestPhone:
    def test_ten_digits_are_formatted(self) -> None:
        assert normalize_phone("8325550142") == ("(832) 555-0142", FieldSource.REFORMATTED)

    def test_leading_country_code_is_stripped(self) -> None:
        assert normalize_phone("1-832-555-0142")[0] == "(832) 555-0142"

    def test_invalid_phone_is_skipped(self) -> None:
        assert normalize_phone("555-0142") == (None, FieldSource.SKIPPED)

    def test_sentinel_phone_is_skipped(self) -> None:
        # "999-999-9999" is a placeholder forms use for "unknown", not a real phone.
        assert normalize_phone("999-999-9999") == (None, FieldSource.SKIPPED)

    def test_already_formatted_phone_is_grounded(self) -> None:
        # Typed value identical to the page value -> GROUNDED, not REFORMATTED,
        # same convention as ssn/zip/dob.
        assert normalize_phone("(832) 555-0142") == ("(832) 555-0142", FieldSource.GROUNDED)

    def test_typed_phone_variants_route_through_the_phone_rule(self) -> None:
        # phone_mobile/phone_home/phone_work exist for the SOP's priority
        # ladder and get the same validation as `phone`.
        grounded = PatientExtraction(
            phone_mobile=ExtractedField(value="832.555.0142", confidence=1.0),
            phone_home=ExtractedField(value="999-999-9999", confidence=1.0),
        )
        typed = normalize(grounded, cbo_address="CBO", alias_name="UNKNOWN")
        assert typed.phone_mobile.typed_value == "(832) 555-0142"
        assert typed.phone_home.typed_value is None
        assert typed.phone_home.source is FieldSource.SKIPPED
        assert typed.phone_work.source is FieldSource.ABSENT


class TestEmail:
    def test_valid_email_is_kept(self) -> None:
        assert normalize_email("a@b.com") == ("a@b.com", FieldSource.GROUNDED)

    def test_comma_email_is_skipped(self) -> None:
        assert normalize_email("a,b@c.com") == (None, FieldSource.SKIPPED)

    def test_no_domain_is_skipped(self) -> None:
        assert normalize_email("nobody@localhost") == (None, FieldSource.SKIPPED)


class TestSsn:
    def test_masked_ssn_is_skipped(self) -> None:
        assert normalize_ssn("XXX-XX-2539") == (None, FieldSource.SKIPPED)

    def test_valid_ssn_is_kept(self) -> None:
        assert normalize_ssn("123-45-6789") == ("123-45-6789", FieldSource.GROUNDED)

    def test_blank_template_field_is_absent_not_skipped(self) -> None:
        # "XXX-XX-" (no digits at all) is an empty template label, never
        # filled in -- distinct from "XXX-XX-2539", a real SSN that was
        # redacted. SKIPPED would wrongly claim a real value was withheld.
        assert normalize_ssn("XXX-XX-") == (None, FieldSource.ABSENT)

    def test_wrong_length_digit_string_is_skipped_not_entered(self) -> None:
        # A partial OCR read ("123-45-678") is not an enterable SSN -- same
        # rule as an invalid phone, and what the SOP implies (from the page or
        # not at all).
        assert normalize_ssn("123-45-678") == (None, FieldSource.SKIPPED)


class TestInsuranceId:
    def test_sentinel_id_is_skipped(self) -> None:
        assert normalize_insurance_id("999999999") == (None, FieldSource.SKIPPED)

    def test_real_id_is_kept_verbatim(self) -> None:
        assert normalize_insurance_id("4HT7MC8NM42") == ("4HT7MC8NM42", FieldSource.GROUNDED)

    def test_absent_id_is_absent(self) -> None:
        assert normalize_insurance_id(None) == (None, FieldSource.ABSENT)


class TestNormalizeOrchestrator:
    def _grounded(self, **fields: ExtractedField) -> PatientExtraction:
        return PatientExtraction(**fields)

    def test_absent_name_emits_alias_default(self) -> None:
        result = normalize(self._grounded(), cbo_address="CBO", alias_name="UNKNOWN")
        assert result.last_name.typed_value == "UNKNOWN"
        assert result.last_name.source is FieldSource.SOP_DEFAULT
        # The grounded value is untouched -- no page reading was invented.
        assert result.last_name.value is None
        assert result.last_name.confidence == 0.0
        assert result.last_name.source_bbox is None

    def test_absent_address_emits_cbo_default(self) -> None:
        result = normalize(self._grounded(), cbo_address="CBO", alias_name="UNKNOWN")
        assert result.address_line1.typed_value == "CBO"
        assert result.address_line1.source is FieldSource.SOP_DEFAULT

    def test_grounded_value_survives_and_is_typed(self) -> None:
        grounded = self._grounded(
            last_name=ExtractedField(value="DOE", confidence=1.0, source_page=1),
            dob=ExtractedField(value="1958/04/17", confidence=1.0, source_page=1),
            zip=ExtractedField(value="77002-1234", confidence=1.0),
        )
        result = normalize(grounded, cbo_address="CBO", alias_name="UNKNOWN")
        # Raw value + provenance preserved.
        assert result.last_name.value == "DOE"
        assert result.last_name.confidence == 1.0
        # Typed value is the SOP shape.
        assert result.last_name.typed_value == "DOE"
        assert result.dob.typed_value == "04/17/1958"
        assert result.dob.source is FieldSource.REFORMATTED
        assert result.zip.typed_value == "77002"

    def test_insurance_slots_are_normalized_independently(self) -> None:
        grounded = self._grounded(
            insurance=[
                InsuranceSlot(
                    insurance_name=ExtractedField(value="BCBS OF TEXAS"),
                    policy_id=ExtractedField(value="4HT7MC8NM42"),
                ),
                InsuranceSlot(
                    policy_id=ExtractedField(value="999999999"),  # sentinel
                ),
            ]
        )
        result = normalize(grounded, cbo_address="CBO", alias_name="UNKNOWN")

        assert len(result.insurance) == 2
        assert result.insurance[0].insurance_name.typed_value == "BCBS OF TEXAS"
        assert result.insurance[0].policy_id.typed_value == "4HT7MC8NM42"
        assert result.insurance[1].policy_id.typed_value is None
        assert result.insurance[1].policy_id.source is FieldSource.SKIPPED

    def test_payer_phone_routes_through_the_phone_rule(self) -> None:
        grounded = PatientExtraction(
            insurance=[
                InsuranceSlot(payer_phone=ExtractedField(value="8005550100", confidence=1.0))
            ]
        )
        typed = normalize(grounded, cbo_address="CBO", alias_name="UNKNOWN")
        assert typed.insurance[0].payer_phone.typed_value == "(800) 555-0100"

    def test_no_insurance_slots_is_an_empty_list(self) -> None:
        result = normalize(self._grounded(), cbo_address="CBO", alias_name="UNKNOWN")
        assert result.insurance == []

    def test_age_and_date_of_service_on_the_same_document_resolve_dob_century(self) -> None:
        # End-to-end through normalize(): a document's own age and
        # date_of_service (not passed explicitly -- normalize() reads them off
        # `grounded` itself) resolve an otherwise-ambiguous 2-digit-year dob.
        grounded = self._grounded(
            dob=ExtractedField(value="12/19/53", confidence=1.0),
            age=ExtractedField(value="81", confidence=1.0),
            date_of_service=ExtractedField(value="2026/07/28", confidence=1.0),
        )
        result = normalize(grounded, cbo_address="CBO", alias_name="UNKNOWN")
        assert result.dob.typed_value == "12/19/1953"
        # age itself is captured like any other field -- no special rule.
        assert result.age.typed_value == "81"
        assert result.age.source is FieldSource.GROUNDED
