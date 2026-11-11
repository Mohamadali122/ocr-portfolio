"""Deterministic claim tag assignment.

`evaluate()` is a pure function of a `ClaimContext`, no session, mirroring
`flags.suggest()` and `ExtractionService` being pure functions of their
inputs -- unit-testable with a dict and no database.

A rule's predicate tree (`app/services/tag_registry.py::TagRule.tree`, parsed
from `tag_rules.toml`) is one of the combinators (`all`/`any`/`none`) or one
of the leaves below. Confidence is never invented: it is the grounding
confidence (`ExtractedField.confidence` -- 1.0 verbatim, 0.7 fuzzy-matched,
1.0 for a reviewer's own correction) of whichever fields the rule actually
consumed, propagated through `min` for `all` and passed through unchanged for
whichever branch of an `any` fired. A leaf with nothing gradeable (`text`,
`has_doc_type`/`missing_doc_type`, `field_conflict`) contributes 1.0.

Determinism: `registry.rulable` is iterated in `TAGS.docx` row order (the
registry's own order, see `tag_registry.Registry.tags`), `all`/`any` children
are evaluated in TOML array order, and evidence is appended in that same
order -- the same claim yields byte-identical assignments every time.

`requires_tag` is why order matters beyond aesthetics: "NO PCS" only makes
sense once "Non-Emergent" is already known, and it works only because
"Non-Emergent" is an earlier row in `TAGS.docx` than "NO PCS" -- `evaluate()`
builds up `assigned` as it goes, so a later rule can see an earlier tag's
result but never the reverse.

Evidence (`Evidence.snippet`) is PHI -- matched text, a field's typed value,
a conflicting document's value. It must never reach a log record or an
`AuditLog.detail`; see CLAUDE.md's PHI rules and `app/core/logging.py`'s
`SENSITIVE_KEYS`.
"""

import re
import uuid
from dataclasses import dataclass
from typing import Any

from app.models.document import DocumentType
from app.schemas.extraction import INSURANCE_FIELD_NAMES
from app.services import sop
from app.services.claim_merge import MergedExtraction, MergedField
from app.services.tag_registry import Registry, _TOP_KEYS, key

_NUMBER = re.compile(r"(\d+(?:\.\d+)?)")


class RuleError(ValueError):
    """A malformed predicate tree in tag_rules.toml. Raised at evaluation
    time -- caught and logged by the caller, never left to crash a claim
    finalize outright; see app/services/claim_service.py."""


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: str  # "text" | "field" | "insurance" | "doc_type" | "conflict" | "tag"
    field: str | None
    document_id: uuid.UUID | None
    page: int | None
    snippet: str  # PHI -- see module docstring


@dataclass(frozen=True, slots=True)
class TagAssignment:
    tag_name: str
    rule_id: str
    confidence: float
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True, slots=True)
class ClaimDocumentText:
    """One member document's text, in the shape a tag rule can search --
    same per-page/per-line structure as `OcrResult.page_texts`."""

    document_id: uuid.UUID
    position: int
    doc_type: DocumentType
    raw_text: str
    page_texts: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ClaimContext:
    merged: MergedExtraction
    doc_types_present: frozenset[DocumentType]
    documents: tuple[ClaimDocumentText, ...]  # position order
    # `has_doc_type`/`missing_doc_type` rules (NO EPCR, No Face Sheet, NO PCS)
    # trust `doc_types_present` as the claim's final, complete set of document
    # types. While any member is still mid-pipeline or failed before
    # classification, `missing_doc_type` must not fire -- but rules that do not
    # depend on document types still can.
    doc_types_complete: bool = True


_Result = tuple[float, list[Evidence]] | None


def evaluate(registry: Registry, ctx: ClaimContext) -> list[TagAssignment]:
    assigned: dict[str, TagAssignment] = {}
    for tag in registry.rulable:
        assert tag.rule is not None  # `rulable` guarantees this
        result = _eval_node(tag.rule.tree, ctx, assigned)
        if result is not None:
            confidence, evidence = result
            assigned[tag.key] = TagAssignment(
                tag_name=tag.name,
                rule_id=tag.rule.rule_id,
                confidence=confidence,
                evidence=tuple(evidence),
            )
    return list(assigned.values())


def _eval_node(
    node: dict[str, Any], ctx: ClaimContext, assigned: dict[str, TagAssignment]
) -> _Result:
    keys = [k for k in _TOP_KEYS if k in node]
    if len(keys) != 1:
        raise RuleError(f"a rule node must have exactly one condition key, got {list(node)}")
    condition = keys[0]

    if condition == "all":
        confidence = 1.0
        evidence: list[Evidence] = []
        for child in node["all"]:
            result = _eval_node(child, ctx, assigned)
            if result is None:
                return None
            child_confidence, child_evidence = result
            confidence = min(confidence, child_confidence)
            evidence.extend(child_evidence)
        return confidence, evidence

    if condition == "any":
        for child in node["any"]:
            result = _eval_node(child, ctx, assigned)
            if result is not None:
                return result
        return None

    if condition == "none":
        for child in node["none"]:
            if _eval_node(child, ctx, assigned) is not None:
                return None
        return 1.0, []

    if condition == "text":
        return _eval_text(node["text"], ctx)
    if condition == "field":
        return _eval_field(node, ctx)
    if condition == "insurance":
        return _eval_insurance(node["insurance"], ctx)
    if condition == "has_doc_type":
        return _eval_doc_type(node["has_doc_type"], ctx, expect_present=True)
    if condition == "missing_doc_type":
        return _eval_doc_type(node["missing_doc_type"], ctx, expect_present=False)
    if condition == "field_conflict":
        return _eval_field_conflict(node["field_conflict"], ctx)
    if condition == "numeric_diff":
        return _eval_numeric_diff(node["numeric_diff"], ctx)
    if condition == "requires_tag":
        return _eval_requires_tag(node["requires_tag"], assigned)
    raise RuleError(f"unknown rule condition {condition!r}")  # pragma: no cover - exhaustive above


def _get_field(ctx: ClaimContext, name: str) -> MergedField:
    field = ctx.merged.fields.get(name)
    if field is None:
        raise RuleError(f"unknown field {name!r} in tag rule")
    return field


def _field_evidence(name: str, field: MergedField) -> Evidence:
    return Evidence(
        kind="field",
        field=name,
        document_id=field.document_id,
        page=field.source_page,
        snippet=field.typed_value or "",
    )


def _eval_field(node: dict[str, Any], ctx: ClaimContext) -> _Result:
    name = node["field"]
    field = _get_field(ctx, name)

    if "matches" in node:
        if field.typed_value and re.search(node["matches"], field.typed_value, re.IGNORECASE):
            return field.confidence, [_field_evidence(name, field)]
        return None

    if "present" in node:
        want = bool(node["present"])
        has = field.typed_value is not None
        if has != want:
            return None
        return (field.confidence if has else 1.0), ([_field_evidence(name, field)] if has else [])

    if "field_source" in node:
        if field.source.value == node["field_source"]:
            return field.confidence, [_field_evidence(name, field)]
        return None

    if "bariatric" in node:
        if _is_bariatric(field.typed_value):
            return field.confidence, [_field_evidence(name, field)]
        return None

    raise RuleError(f"field rule for {name!r} has no recognized condition")


def _eval_insurance(spec: dict[str, Any], ctx: ClaimContext) -> _Result:
    slot_index = int(spec["slot"])
    if slot_index >= len(ctx.merged.insurance):
        return None
    slot = ctx.merged.insurance[slot_index]
    name = spec["field"]
    if name not in INSURANCE_FIELD_NAMES:
        raise RuleError(f"unknown insurance field {name!r}")
    field: MergedField = getattr(slot, name)
    evidence_field = f"insurance.{slot_index}.{name}"

    if "matches" in spec:
        if field.typed_value and re.search(spec["matches"], field.typed_value, re.IGNORECASE):
            return field.confidence, [_field_evidence(evidence_field, field)]
        return None

    if "prefix_in" in spec:
        if field.typed_value and field.typed_value.upper().startswith(tuple(spec["prefix_in"])):
            return field.confidence, [_field_evidence(evidence_field, field)]
        return None

    if "present" in spec:
        want = bool(spec["present"])
        has = field.typed_value is not None
        if has != want:
            return None
        return (
            field.confidence if has else 1.0,
            [_field_evidence(evidence_field, field)] if has else [],
        )

    raise RuleError(
        f"insurance rule for slot {slot_index} field {name!r} has no recognized condition"
    )


# A line an EPCR/HFS export prints identically on 2+ pages is a running
# header/footer -- the reporting agency's own name and letterhead, a patient
# banner, a page number -- never patient-specific content. Without this, a
# `text` rule matching the agency's own name (e.g. "city ambulance") fires on
# every document that agency produces, regardless of the patient: confirmed
# live on a real PCR whose "Company City Ambulance Service ..." header line,
# repeated on all 5 pages, wrongly fired the "CAS Employee" tag.
def _repeated_lines(document: ClaimDocumentText) -> frozenset[str]:
    # On a 1-2 page document, "appears twice" is not letterhead -- only suppress
    # repeated lines when the document has enough pages for that pattern to mean
    # running header/footer (the 5-page false positive the rule was added for).
    if len(document.page_texts) < 3:
        return frozenset()
    seen_on_a_page: dict[str, int] = {}
    for page in document.page_texts:
        for text in {line.get("text", "") for line in page.get("lines", [])}:
            if text:
                seen_on_a_page[text] = seen_on_a_page.get(text, 0) + 1
    return frozenset(text for text, count in seen_on_a_page.items() if count >= 2)


# Structured EPCR exports print a fixed field label followed directly by its
# answer on one line ("Work-Related Illness/Injury No", "Mass Casualty
# Incident No") -- confirmed live on a real PCR, where the "Work comp" rule's
# pattern narrowly missed matching exactly such a line. A `text` rule must
# not fire off the label alone when the line's own answer is a negative or
# null one -- this is deliberately conservative (some genuine "No, but ..."
# narratives will be missed) because a wrong auto-tag reaches a biller as a
# confident assertion, while a missed one just falls back to manual review.
_NEGATIVE_ENDING = re.compile(
    r"\b(no|none|n/a|na|denied|denies|negative|unknown|not applicable|not reported|not done)\s*$",
    re.IGNORECASE,
)


def _eval_text(pattern: str, ctx: ClaimContext) -> _Result:
    regex = re.compile(pattern, re.IGNORECASE)
    for document in ctx.documents:
        boilerplate = _repeated_lines(document)
        page_texts: list[str] = []
        matched_a_line = False
        for page in document.page_texts:
            lines = [line.get("text", "") for line in page.get("lines", [])]
            page_texts.append("\n".join(text for text in lines if text not in boilerplate))
            for line in page.get("lines", []):
                text = line.get("text", "")
                if text in boilerplate or not regex.search(text):
                    continue
                # The raw pattern is fully present on this single line -- the
                # line loop is authoritative for it either way, matched or
                # deliberately suppressed. Never let the wrapped-phrase
                # fallback below re-match the same negated line unguarded.
                matched_a_line = True
                if not _NEGATIVE_ENDING.search(text):
                    return 1.0, [
                        Evidence(
                            kind="text",
                            field=None,
                            document_id=document.document_id,
                            page=page.get("page"),
                            snippet=text,
                        )
                    ]
        if matched_a_line:
            continue
        # The value spans more than one recognized line (a wrapped phrase) --
        # genuinely present, but there is no single line to cite. Record the
        # pattern that matched, not an arbitrary slice of the document.
        # Boilerplate-stripped, same reasoning as the per-line loop above.
        # Only reached when no single line contained the raw pattern at all,
        # so there is no line-level negation verdict this could be undoing.
        deboilerplated = "\n\n".join(page_texts)
        if regex.search(deboilerplated):
            return 1.0, [
                Evidence(
                    kind="text",
                    field=None,
                    document_id=document.document_id,
                    page=None,
                    snippet=pattern,
                )
            ]
    return None


def _eval_doc_type(name: str, ctx: ClaimContext, *, expect_present: bool) -> _Result:
    if not expect_present and not ctx.doc_types_complete:
        return None
    try:
        doc_type = DocumentType(name)
    except ValueError as exc:
        raise RuleError(f"unknown document type {name!r}") from exc
    present = doc_type in ctx.doc_types_present
    if present != expect_present:
        return None
    return 1.0, [
        Evidence(kind="doc_type", field=None, document_id=None, page=None, snippet=doc_type.value)
    ]


def _eval_field_conflict(name: str, ctx: ClaimContext) -> _Result:
    for conflict in ctx.merged.conflicts:
        if conflict.field == name:
            evidence = [
                Evidence(
                    kind="conflict", field=name, document_id=document_id, page=None, snippet=value
                )
                for document_id, value in conflict.values_by_document
            ]
            return 1.0, evidence
    return None


def _number(text: str | None) -> float | None:
    if not text:
        return None
    match = _NUMBER.search(text)
    return float(match.group(1)) if match else None


def _eval_numeric_diff(spec: dict[str, Any], ctx: ClaimContext) -> _Result:
    names: list[str] = spec["fields"]
    if len(names) != 2:
        raise RuleError("numeric_diff needs exactly two `fields`")
    minimum = float(spec["min"])
    resolved = [(name, _get_field(ctx, name)) for name in names]
    first, second = (_number(field.typed_value) for _, field in resolved)
    if first is None or second is None:
        return None
    if abs(first - second) < minimum:
        return None
    confidence = min(field.confidence for _, field in resolved)
    evidence = [_field_evidence(name, field) for name, field in resolved]
    return confidence, evidence


def _eval_requires_tag(name: str, assigned: dict[str, TagAssignment]) -> _Result:
    existing = assigned.get(key(name))
    if existing is None:
        return None
    return existing.confidence, [
        Evidence(kind="tag", field=None, document_id=None, page=None, snippet=existing.tag_name)
    ]


# SOP: bariatric at 300 lb / 136 kg -- same threshold and unit-required
# discipline as app/services/flags.py::_weight_exceeds_bariatric (a bare
# number with no printed unit is ambiguous and never flags).
def _is_bariatric(typed_value: str | None) -> bool:
    if not typed_value:
        return False
    value = _number(typed_value)
    if value is None:
        return False
    lowered = typed_value.lower()
    if "lb" in lowered or "pound" in lowered:
        return value >= sop.BARIATRIC_POUNDS
    if "kg" in lowered or "kilo" in lowered:
        return value >= sop.BARIATRIC_KILOGRAMS
    return False
