"""Scores cached extractions against hand-corrected answer keys: a four-way
confusion per field -- correct, wrong-value, hallucinated, missed -- printed
as an aggregate scorecard plus a per-field breakdown, and written in full
(including the actual values) to a local report file.

Console output never prints a field value, only field NAMES and bucket
counts -- the same "names are safe, values are not" rule
app/services/extraction_service.py's hallucination guard already follows for
its own logging (`hallucination_guard_nulled_field`). The full report file is
PHI like everything else under sample/eval/ -- open it locally, never paste
its contents anywhere.
"""

import json
import time
from collections import Counter
from dataclasses import asdict, dataclass

from app.schemas.extraction import FIELD_NAMES, INSURANCE_FIELD_NAMES, PatientExtraction
from app.services.extraction_service import _normalize
from scripts.prompt_eval._common import ANSWER_KEY_DIR, REPORTS_DIR

BUCKETS = ("correct", "wrong", "hallucinated", "missed")


@dataclass(frozen=True, slots=True)
class FieldResult:
    encounter: str
    doc_key: str
    field: str
    bucket: str
    got: str | None
    expected: str | None


def _bucket(got: str | None, expected: str | None) -> str:
    if got is None and expected is None:
        return "correct"
    if expected is None:
        return "hallucinated"
    if got is None:
        return "missed"
    return "correct" if _normalize(got) == _normalize(expected) else "wrong"


def _flatten(fields: PatientExtraction) -> dict[str, str | None]:
    flat: dict[str, str | None] = {name: getattr(fields, name).value for name in FIELD_NAMES}
    for index, slot in enumerate(fields.insurance):
        for name in INSURANCE_FIELD_NAMES:
            flat[f"insurance[{index}].{name}"] = getattr(slot, name).value
    return flat


def load_answer_key(encounter: str, doc_key: str) -> PatientExtraction | None:
    path = ANSWER_KEY_DIR / encounter / f"{doc_key}.json"
    return PatientExtraction.model_validate_json(path.read_text()) if path.exists() else None


def score_document(
    encounter: str, doc_key: str, got: PatientExtraction, expected: PatientExtraction
) -> list[FieldResult]:
    got_flat = _flatten(got)
    expected_flat = _flatten(expected)
    # Union, not intersection: an insurance slot the model added or dropped
    # relative to the key must still show up as hallucinated/missed, not be
    # silently skipped because the two field sets don't line up.
    field_names = sorted(set(got_flat) | set(expected_flat))
    return [
        FieldResult(
            encounter,
            doc_key,
            name,
            _bucket(got_flat.get(name), expected_flat.get(name)),
            got_flat.get(name),
            expected_flat.get(name),
        )
        for name in field_names
    ]


def report(scored: list[tuple[str, str, PatientExtraction]]) -> None:
    results: list[FieldResult] = []
    missing_keys: list[str] = []
    for encounter, doc_key, got in scored:
        expected = load_answer_key(encounter, doc_key)
        if expected is None:
            missing_keys.append(f"{encounter}/{doc_key}")
            continue
        results.extend(score_document(encounter, doc_key, got, expected))

    if missing_keys:
        print(
            f"\n{len(missing_keys)} doc(s) have no answer key yet "
            "(run --bootstrap, then correct them):"
        )
        for key in missing_keys:
            print(f"  {key}")

    counts = Counter(r.bucket for r in results)
    total = sum(counts.values()) or 1
    print("\n--- scorecard (all docs, all fields) ---")
    for bucket in BUCKETS:
        n = counts.get(bucket, 0)
        print(f"{bucket:>12}: {n:4d}  ({n / total:.1%})")

    alarms = [r for r in results if r.bucket in ("wrong", "hallucinated")]
    if alarms:
        print(
            f"\n--- {len(alarms)} regression(s) -- field names only, see report file for values ---"
        )
        for r in alarms:
            print(f"  {r.encounter}/{r.doc_key}  {r.field}  [{r.bucket}]")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{time.strftime('%Y%m%dT%H%M%S')}.json"
    report_path.write_text(
        json.dumps(
            {
                "counts": dict(counts),
                "total": total,
                "missing_keys": missing_keys,
                "fields": [asdict(r) for r in results],
            },
            indent=2,
        )
    )
    print(f"\nfull report (includes actual values -- PHI, local only): {report_path}")
