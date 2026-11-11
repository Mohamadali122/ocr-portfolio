"""Advisory review flags derived from a stored extraction.

Read-time, not persisted: `suggest()` is a pure function of the grounded
`PatientExtraction`, computed when the extraction is read. Rule changes apply
to every already-processed document instantly, with no reprocessing and no
migration -- the same reasoning as `PatientGroupingService`'s read-time
mismatch flags.

Only rules the prebilling SOP states unambiguously live here. The MCR Age /
MCD Minor age-threshold rules are deliberately absent until the contradictions
in `PREBILLER_QUESTIONS.md` are answered.

`Flag.detail` carries field names and slot indices only, never a document
value -- the same discipline as `extraction_validation.Verdict`, so a flag is
safe to log and safe to store anywhere an audit detail can go.
"""

import re
from dataclasses import dataclass

from app.schemas.extraction import FieldSource, PatientExtraction
from app.services import sop

# SOP: "If the BCBS is a primary insurance and the prefix is one of these ...
# So, we tag BCBSTX_HMO" (billing SOP).

# The SOP's literal group numbers that mean the plan is a MAP / Market Place /
# MMP-SNP combination rather than the plain commercial payor (billing SOP
# tables 19, 23-24, 38).
_PLAN_GROUPS = {
    "000003-TX",  # Aetna MAP
    "000001-01",  # Aetna Market Place
    "H5413-061",  # CIGNA MAP
    "H5413-066",  # CIGNA MAP
    "5413-060",  # CIGNA MMP/SNP
    "TXONEX",  # UHC Market Place
    "TXMCD000",  # Wellpoint MCD
    "TXMCR000",  # Wellpoint MAP
    "TXMMP000",  # Wellpoint MMP/SNP
}
_PLAN_GROUP_PREFIXES = ("100-", "200-", "TXDSNP")  # Aetna MAP ranges, Wellmed DSNP

# SOP alias names for an unknown patient: "Doe, Jane (female) / Doe, Jhon
# (male)" (billing SOP). "John" included for the obvious spelling.

_NUMBER = re.compile(r"(\d+(?:\.\d+)?)")


@dataclass(frozen=True, slots=True)
class Flag:
    code: str
    detail: str  # field names / slot indices only -- never a document value


def _number(text: str | None) -> float | None:
    if not text:
        return None
    match = _NUMBER.search(text)
    return float(match.group(1)) if match else None


def _weight_exceeds_bariatric(text: str | None) -> bool:
    """SOP: bariatric at 300 lb / 136 kg (billing SOP). The unit must be
    printed -- a bare number is ambiguous and never flags."""
    if not text:
        return False
    value = _number(text)
    if value is None:
        return False
    lowered = text.lower()
    if "lb" in lowered or "pound" in lowered:
        return value >= sop.BARIATRIC_POUNDS
    if "kg" in lowered or "kilo" in lowered:
        return value >= sop.BARIATRIC_KILOGRAMS
    return False


def suggest(fields: PatientExtraction) -> list[Flag]:
    flags: list[Flag] = []

    if fields.address_line1.source is FieldSource.SOP_DEFAULT:
        flags.append(
            Flag(
                code="cbo_address_used",
                detail="address_line1 fell back to the CBO default -- SOP: candidate "
                "for Invalid PT Demos / FS Request / Do Not Bill Patient tags",
            )
        )

    last = (fields.last_name.typed_value or "").casefold()
    first = (fields.first_name.typed_value or "").casefold()
    if last == sop.ALIAS_LAST_NAME and first in sop.ALIAS_FIRST_NAMES:
        flags.append(
            Flag(
                code="alias_name_detected",
                detail="patient name matches the SOP alias pair -- check attachments "
                "for the real name, mention alias in the leg note",
            )
        )

    for index, slot in enumerate(fields.insurance):
        name = (slot.insurance_name.typed_value or "").casefold()
        policy = (slot.policy_id.typed_value or "").upper()
        group = (slot.group_number.typed_value or "").upper()

        if index >= 1 and slot.insurance_name.typed_value and not slot.policy_id.typed_value:
            flags.append(
                Flag(
                    code="secondary_slot_missing_policy",
                    detail=f"insurance.{index} has a payor name but no policy id -- "
                    "SOP: do not add to the payor table, mention in the leg note",
                )
            )

        if (
            index == 0
            and ("bcbs" in name or "blue cross" in name)
            and policy.startswith(sop.BCBS_HMO_PREFIXES)
        ):
            flags.append(
                Flag(
                    code="bcbstx_hmo_prefix",
                    detail="insurance.0 policy prefix is on the BCBSTX HMO list -- "
                    "SOP: tag BCBSTX HMO and state it in the leg note",
                )
            )

        if group and (group in _PLAN_GROUPS or group.startswith(_PLAN_GROUP_PREFIXES)):
            flags.append(
                Flag(
                    code="plan_map_or_marketplace_group",
                    detail=f"insurance.{index} group number matches a MAP / Market Place "
                    "/ MMP-SNP combination -- SOP: enter the combination payor, not the "
                    "plain commercial one",
                )
            )

        if "supplement" in name:
            flags.append(
                Flag(
                    code="supplement_never_primary",
                    detail=f"insurance.{index} name contains 'Supplement' -- SOP: a "
                    "Medicare Supplement plan cannot be the primary payor",
                )
            )

    mileage = _number(fields.actual_mileage.typed_value)
    distance = _number(fields.actual_distance.typed_value)
    if (
        mileage is not None
        and distance is not None
        and abs(mileage - distance) >= sop.MILEAGE_DISCREPANCY_MINIMUM
    ):
        flags.append(
            Flag(
                code="mileage_discrepancy",
                detail="actual_mileage and actual_distance differ by 5 or more -- "
                "SOP: tag QA/Mileage Review",
            )
        )

    if _weight_exceeds_bariatric(fields.weight_kg.typed_value):
        flags.append(
            Flag(
                code="bariatric_weight",
                detail="weight meets the bariatric threshold (300 lb / 136 kg) -- "
                "SOP: Bariatric tag",
            )
        )

    return flags
