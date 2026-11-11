"""Deterministic, SOP-driven field normalization.

The hallucination guard (`extraction_service._ground_field`) proves each field's
raw `value` appears on the page. This layer derives the `typed_value` a prebiller
enters into the billing system -- a pure function of the grounded value plus a couple of
operator-configured defaults. It never reads the document and never calls the
model: every typed value is either a transform of a proven page value or a named
SOP default, tagged by `FieldSource` so the two are never conflated.

Kept pure (no `Settings` import) so it is unit-testable in isolation; the two
operator defaults are passed in by `ExtractionService`.
"""

import re
from datetime import date, datetime

from app.schemas.extraction import (
    FIELD_NAMES,
    ExtractedField,
    FieldSource,
    InsuranceSlot,
    PatientExtraction,
)

# strptime is lenient about zero-padding, so "%m/%d/%Y" also parses "4/17/1958".
_DOB_FORMATS = ("%m/%d/%Y", "%Y/%m/%d", "%m-%d-%Y", "%Y-%m-%d", "%m/%d/%y", "%Y%m%d")
_DOB_OUTPUT = "%m/%d/%Y"

# %y (2-digit year) is inherently ambiguous ("53" could be 1953 or 2053).
# strptime's own POSIX windowing (00-68 -> 2000-2068, 69-99 -> 1969-1999) is
# the fallback of last resort below, not the primary rule: a printed age or
# the document's own date_of_service, when present, pin the century far more
# reliably than a blind date-of-parsing-relative guess ever could. Bounds a
# patient's plausible age when checking either signal -- wide enough for a
# genuine centenarian, narrow enough to reject "born in the future".
_PLAUSIBLE_AGE_MIN = 0
_PLAUSIBLE_AGE_MAX = 110

_AGE_DIGITS = re.compile(r"\d+")

_SUFFIXES = {"sr": "Sr", "jr": "Jr", "ii": "II", "iii": "III", "iv": "IV"}
_MASKED_SSN = re.compile(r"[*xX]{2,}")
_EMAIL = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")

# Forms in the wild use these as "not actually known" placeholders, not real
# values -- e.g. a guarantor phone left as "999-999-9999", or a zip stamped
# "99999" for an unknown city. Normalizing them as if real would enter a fake
# phone number or zip into the billing system.
_SENTINEL_PHONE_DIGITS = {"9999999999"}
_SENTINEL_ZIP = "99999"
_SENTINEL_ID = re.compile(r"^9+$")


def _parse_age_years(raw: str | None) -> int | None:
    """The first run of digits in a printed age field ("81", "81 y/o", "Age: 81").
    None if there is nothing numeric -- an unreadable or absent age is simply
    not a signal, never a false one."""
    if not raw:
        return None
    match = _AGE_DIGITS.search(raw)
    return int(match.group()) if match else None


def _resolve_two_digit_year(two_digit: int, *, reference: date, age: int | None) -> int:
    """Which century a bare 2-digit year (0-99) belongs to.

    `two_digit` is recovered as `strptime(..., "%m/%d/%y").year % 100` --
    strptime already POSIX-windowed it into a year, and `% 100` undoes that
    windowing back to the literal two digits, independent of what strptime
    guessed.

    Priority: an explicit age (if it survives its own plausibility check)
    beats a blind guess; failing that, whichever century keeps the patient's
    age plausible as of `reference` wins; failing that (both plausible, or
    neither -- reference far enough from either candidate that no signal
    disambiguates) falls back to strptime's own POSIX convention, so the
    sliver of genuinely unresolvable cases keeps today's shipped behavior
    rather than a newly invented one.
    """
    candidates = (1900 + two_digit, 2000 + two_digit)

    if age is not None:
        implied_year = reference.year - age
        best = min(candidates, key=lambda year: abs(year - implied_year))
        if _PLAUSIBLE_AGE_MIN <= reference.year - best <= _PLAUSIBLE_AGE_MAX:
            return best
        # The age signal itself doesn't hold up (OCR garble) -- ignore it.

    plausible = [
        year
        for year in candidates
        if _PLAUSIBLE_AGE_MIN <= reference.year - year <= _PLAUSIBLE_AGE_MAX
    ]
    if len(plausible) == 1:
        return plausible[0]
    return 2000 + two_digit if two_digit <= 68 else 1900 + two_digit


def parse_dob(
    raw: str | None, *, reference_date: date | None = None, age: int | None = None
) -> date | None:
    """The one DOB parser. Accepts HFS `MM/DD/YYYY` and EPCR `YYYY/MM/DD` (and
    dash variants); returns None if absent or unparseable.

    `reference_date` and `age` disambiguate a 2-digit-year DOB only --
    `_resolve_two_digit_year` above. Every other format already carries an
    explicit 4-digit year and never consults either.
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    for fmt in _DOB_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt != "%m/%d/%y":
            return parsed.date()
        two_digit = parsed.year % 100
        year = _resolve_two_digit_year(two_digit, reference=reference_date or date.today(), age=age)
        try:
            return parsed.date().replace(year=year)
        except ValueError:
            return None  # Feb 29 landing on a resolved non-leap year
    return None


def searchable_name(typed_value: str | None, source: FieldSource) -> str | None:
    """The value denormalized onto `Document.patient_last_name` /
    `Claim.patient_last_name` for search. Mirrors `searchable_dob`: excludes
    `SOP_DEFAULT` so the UNKNOWN alias does not pollute the name search index.
    A name search must never match "we don't actually know" against every
    no-name document in the database.
    """
    if source in (FieldSource.SOP_DEFAULT, FieldSource.ABSENT, FieldSource.SKIPPED):
        return None
    return typed_value


def searchable_dob(typed_value: str | None, source: FieldSource) -> date | None:
    """The value denormalized onto `Document.patient_dob`/`Claim.patient_dob`
    for search. Built from `typed_value`, not the raw grounded `value`:
    `typed_value` is what `normalize_dob` already resolved the century for,
    and it is what a reviewer's correction overwrites (`value` never changes
    on a correction, so parsing it here would silently ignore one). Excludes
    `SOP_DEFAULT`/`ABSENT`/`SKIPPED` -- a DOB range search must never match
    "we don't actually know" against the 01/01/1900 placeholder.
    """
    if source in (FieldSource.SOP_DEFAULT, FieldSource.ABSENT, FieldSource.SKIPPED):
        return None
    return parse_dob(typed_value)


def normalize_dob(
    raw: str | None,
    *,
    default: str | None,
    reference_date: date | None = None,
    age: str | None = None,
) -> tuple[str | None, FieldSource]:
    """-> `MM/DD/YYYY`. Absent + a default -> that default (`sop_default`);
    absent + no default -> absent. Present but unparseable -> kept verbatim.

    `reference_date` (typically the same document's own `date_of_service`)
    and `age` (that document's own printed age, raw text) resolve a
    2-digit-year DOB's century -- see `parse_dob`/`_resolve_two_digit_year`.
    """
    parsed = parse_dob(raw, reference_date=reference_date, age=_parse_age_years(age))
    if parsed is not None:
        typed = parsed.strftime(_DOB_OUTPUT)
        source = FieldSource.GROUNDED if typed == (raw or "").strip() else FieldSource.REFORMATTED
        return typed, source
    if raw and raw.strip():
        return raw.strip(), FieldSource.GROUNDED
    if default is not None:
        return default, FieldSource.SOP_DEFAULT
    return None, FieldSource.ABSENT


def _is_suffix(token: str) -> bool:
    return token.strip().rstrip(".").lower() in _SUFFIXES


def normalize_suffix(raw: str | None) -> tuple[str | None, FieldSource]:
    if not raw or not raw.strip():
        return None, FieldSource.ABSENT
    key = raw.strip().rstrip(".").lower()
    canonical = _SUFFIXES.get(key)
    if canonical is None:
        return None, FieldSource.ABSENT  # not an allowed suffix -> drop
    source = FieldSource.GROUNDED if canonical == raw.strip() else FieldSource.REFORMATTED
    return canonical, source


def normalize_name(
    last_raw: str | None, first_raw: str | None, suffix_raw: str | None
) -> tuple[str, str, str | None]:
    """Split into Last / First / Suffix and drop the middle name.

    Handles the common HFS `Last, First [Middle] [Suffix]` collapsed into one
    field, and a suffix trailing either the first or last name. Each returned
    part is a substring of the corresponding raw value, so grounding survives.
    """
    last = (last_raw or "").strip()
    first = (first_raw or "").strip()
    suffix = (suffix_raw or "").strip() or None

    if "," in last:
        left, _, right = last.partition(",")
        right = right.strip()
        if not first:
            last, first = left.strip(), right
        elif right and right.casefold().startswith(first.split()[0].casefold()):
            # HFS prints "Last, First"; the model filled both fields, so the
            # comma'd remainder duplicates the first name -- keep last alone.
            last = left.strip()

    if first:
        parts = first.split()
        if len(parts) > 1 and _is_suffix(parts[-1]) and suffix is None:
            suffix = parts[-1]
            parts = parts[:-1]
        first = parts[0] if parts else ""  # first token only -> middle dropped

    if last:
        lparts = last.split()
        if len(lparts) > 1 and _is_suffix(lparts[-1]) and suffix is None:
            suffix = lparts[-1]
            last = " ".join(lparts[:-1])

    return last, first, suffix


def normalize_zip(raw: str | None) -> tuple[str | None, FieldSource]:
    if not raw or not raw.strip():
        return None, FieldSource.ABSENT
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 5:
        five = digits[:5]
        if five == _SENTINEL_ZIP:  # placeholder for "unknown", not a real zip
            return None, FieldSource.SKIPPED
        source = FieldSource.GROUNDED if five == raw.strip() else FieldSource.REFORMATTED
        return five, source
    return None, FieldSource.SKIPPED  # not a valid zip -> not entered


def normalize_phone(raw: str | None) -> tuple[str | None, FieldSource]:
    if not raw or not raw.strip():
        return None, FieldSource.ABSENT
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if digits in _SENTINEL_PHONE_DIGITS:  # placeholder for "unknown", not a real phone
        return None, FieldSource.SKIPPED
    if len(digits) == 10:
        typed = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
        source = FieldSource.GROUNDED if typed == raw.strip() else FieldSource.REFORMATTED
        return typed, source
    return None, FieldSource.SKIPPED  # not a valid phone -> not entered


def normalize_email(raw: str | None) -> tuple[str | None, FieldSource]:
    if not raw or not raw.strip():
        return None, FieldSource.ABSENT
    text = raw.strip()
    if not _EMAIL.match(text):  # a comma or an unclear domain -> invalid, skip
        return None, FieldSource.SKIPPED
    return text, FieldSource.GROUNDED


def normalize_ssn(raw: str | None) -> tuple[str | None, FieldSource]:
    if not raw or not raw.strip():
        return None, FieldSource.ABSENT
    text = raw.strip()
    if _MASKED_SSN.search(text):
        if any(c.isdigit() for c in text):  # XXX-XX-1234 -> a real SSN, encrypted: skip
            return None, FieldSource.SKIPPED
        return None, FieldSource.ABSENT  # "XXX-XX-" -> an empty template field, never filled in
    digits = re.sub(r"\D", "", text)
    if len(digits) == 9:
        typed = f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
        source = FieldSource.GROUNDED if typed == text else FieldSource.REFORMATTED
        return typed, source
    return None, FieldSource.SKIPPED  # not a valid SSN -> not entered


def _basic(field: ExtractedField) -> ExtractedField:
    """No special rule: type the trimmed page value, or mark absent."""
    if field.value and field.value.strip():
        return field.model_copy(
            update={"typed_value": field.value.strip(), "source": FieldSource.GROUNDED}
        )
    return field.model_copy(update={"typed_value": None, "source": FieldSource.ABSENT})


def _apply(field: ExtractedField, result: tuple[str | None, FieldSource]) -> ExtractedField:
    typed, source = result
    return field.model_copy(update={"typed_value": typed, "source": source})


def _name_field(field: ExtractedField, typed: str | None) -> ExtractedField:
    if not typed:
        return field.model_copy(update={"typed_value": None, "source": FieldSource.ABSENT})
    source = (
        FieldSource.GROUNDED if typed == (field.value or "").strip() else FieldSource.REFORMATTED
    )
    return field.model_copy(update={"typed_value": typed, "source": source})


def normalize_insurance_id(raw: str | None) -> tuple[str | None, FieldSource]:
    """A policy or group number, unformatted -- but a run of all-9s
    (`999999999`) is the sentinel forms use for "not actually known"."""
    if not raw or not raw.strip():
        return None, FieldSource.ABSENT
    text = raw.strip()
    if _SENTINEL_ID.match(re.sub(r"\D", "", text) or "0"):
        return None, FieldSource.SKIPPED
    return text, FieldSource.GROUNDED


def _normalize_insurance_slot(slot: InsuranceSlot) -> InsuranceSlot:
    return slot.model_copy(
        update={
            "insurance_name": _basic(slot.insurance_name),
            "policy_id": _apply(slot.policy_id, normalize_insurance_id(slot.policy_id.value)),
            "group_number": _apply(
                slot.group_number, normalize_insurance_id(slot.group_number.value)
            ),
            "subscriber_name": _basic(slot.subscriber_name),
            "relationship_to_subscriber": _basic(slot.relationship_to_subscriber),
            "effective_date": _basic(slot.effective_date),
            "verified": _basic(slot.verified),
            "payer_address": _basic(slot.payer_address),
            "payer_phone": _apply(slot.payer_phone, normalize_phone(slot.payer_phone.value)),
        }
    )


def normalize(
    grounded: PatientExtraction,
    *,
    cbo_address: str,
    alias_name: str,
    dob_default: str = "01/01/1900",
) -> PatientExtraction:
    """Fill `typed_value` + `source` on every field. The guard already ran on
    `value`; this is a deterministic post-step, never touching the OCR text."""
    last_typed, first_typed, suffix_typed = normalize_name(
        grounded.last_name.value, grounded.first_name.value, grounded.suffix.value
    )
    if not last_typed and not first_typed:
        # No name anywhere -> SOP alias (operator-configured), clearly a default.
        fields = {
            "last_name": _apply(grounded.last_name, (alias_name, FieldSource.SOP_DEFAULT)),
            "first_name": _basic(grounded.first_name),
            "suffix": _name_field(grounded.suffix, suffix_typed),
        }
    else:
        fields = {
            "last_name": _name_field(grounded.last_name, last_typed),
            "first_name": _name_field(grounded.first_name, first_typed),
            "suffix": _name_field(grounded.suffix, suffix_typed),
        }

    # date_of_service is the reference point for resolving a 2-digit-year DOB
    # on this same document; parse_dob's own POSIX fallback is fine for it
    # specifically (a service date is essentially always "recent", so 00-68
    # -> 2000s is right far more often than not).
    reference_date = parse_dob(grounded.date_of_service.value) or date.today()
    fields["dob"] = _apply(
        grounded.dob,
        normalize_dob(
            grounded.dob.value,
            default=dob_default,
            reference_date=reference_date,
            age=grounded.age.value,
        ),
    )
    fields["guarantor_dob"] = _apply(
        grounded.guarantor_dob,
        normalize_dob(grounded.guarantor_dob.value, default=None, reference_date=reference_date),
        # No `age=`: there is no guarantor-age field, so guarantor_dob only
        # ever gets the plausible-window/POSIX fallback, never the age-pinned
        # resolution.
    )
    fields["ssn"] = _apply(grounded.ssn, normalize_ssn(grounded.ssn.value))
    fields["guarantor_ssn"] = _apply(
        grounded.guarantor_ssn, normalize_ssn(grounded.guarantor_ssn.value)
    )
    fields["phone"] = _apply(grounded.phone, normalize_phone(grounded.phone.value))
    fields["guarantor_phone"] = _apply(
        grounded.guarantor_phone, normalize_phone(grounded.guarantor_phone.value)
    )
    fields["next_of_kin_phone"] = _apply(
        grounded.next_of_kin_phone, normalize_phone(grounded.next_of_kin_phone.value)
    )
    for phone_name in ("phone_mobile", "phone_home", "phone_work"):
        field = getattr(grounded, phone_name)
        fields[phone_name] = _apply(field, normalize_phone(field.value))
    fields["email"] = _apply(grounded.email, normalize_email(grounded.email.value))
    fields["zip"] = _apply(grounded.zip, normalize_zip(grounded.zip.value))

    if grounded.address_line1.value and grounded.address_line1.value.strip():
        fields["address_line1"] = _basic(grounded.address_line1)
    else:
        fields["address_line1"] = _apply(
            grounded.address_line1, (cbo_address, FieldSource.SOP_DEFAULT)
        )

    # Everything else: type the page value as-is.
    for name in FIELD_NAMES:
        if name not in fields:
            fields[name] = _basic(getattr(grounded, name))

    insurance = [_normalize_insurance_slot(slot) for slot in grounded.insurance]
    return PatientExtraction(**fields, insurance=insurance)
