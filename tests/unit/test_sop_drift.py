"""TOML tag_rules.toml thresholds must match app.services.sop constants."""

from pathlib import Path

from app.services import sop
from app.services.tag_registry import build_registry

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCX = _REPO_ROOT / "files_for_project" / "TAGS.docx"
if not _DOCX.is_file():
    _DOCX = Path("/srv/app/TAGS.docx")
_RULES = _REPO_ROOT / "app" / "services" / "tag_rules.toml"


def test_bcbs_hmo_prefixes_match_sop() -> None:
    registry = build_registry(_DOCX, _RULES)
    tag = registry.by_key("BCBSTX HMO")
    assert tag is not None and tag.rule is not None
    prefix_branch = next(
        child
        for child in tag.rule.tree["all"]
        if "insurance" in child and child["insurance"].get("prefix_in")
    )
    assert tuple(prefix_branch["insurance"]["prefix_in"]) == sop.BCBS_HMO_PREFIXES


def test_mileage_review_minimum_matches_sop() -> None:
    registry = build_registry(_DOCX, _RULES)
    tag = registry.by_key("QA/Mileage Review")
    assert tag is not None and tag.rule is not None
    assert tag.rule.tree["numeric_diff"]["min"] == sop.MILEAGE_DISCREPANCY_MINIMUM
