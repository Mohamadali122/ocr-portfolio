"""`app/services/tag_registry.py` against the real `TAGS.docx` + `tag_rules.toml`.

Deliberately not a fixture-built fake docx: the parsing edge cases worth
locking down (a cell with several tag names, a name repeated across rows
with a different reason, the one fully blank row) only exist in the real
file, and a hand-built fixture would silently stop testing them the moment
someone "cleans up" the fixture.
"""

from pathlib import Path

import pytest

from app.services.tag_registry import RegistryError, build_registry, key

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TAGS_DOCX_CONTAINER = Path("/srv/app/TAGS.docx")
_TAGS_DOCX_HOST = _REPO_ROOT / "files_for_project" / "TAGS.docx"
_DOCX = _TAGS_DOCX_CONTAINER if _TAGS_DOCX_CONTAINER.is_file() else _TAGS_DOCX_HOST
_RULES = _REPO_ROOT / "app" / "services" / "tag_rules.toml"


class TestKey:
    def test_casefolds_and_collapses_whitespace(self) -> None:
        assert key("Port Arthur  Quick bill") == key("port arthur quick bill")

    def test_strips_leading_and_trailing_whitespace(self) -> None:
        assert key("  Austin ") == key("Austin")


class TestBuildRegistry:
    def test_parses_at_least_forty_tags(self) -> None:
        registry = build_registry(_DOCX, _RULES)
        assert len(registry.tags) >= 40

    def test_multi_line_name_cell_splits_into_separate_tags(self) -> None:
        # TAGS.docx row 2: one cell holds "Invalid Pt Demos" / "Do Not Bill Pt"
        # / "FS Request" on separate lines, sharing one CBO-address reason.
        registry = build_registry(_DOCX, _RULES)
        for name in ("Invalid Pt Demos", "Do Not Bill Pt", "FS Request"):
            tag = registry.by_key(name)
            assert tag is not None, f"{name!r} not found"

    def test_duplicate_name_across_rows_merges_reasons_not_overwrites(self) -> None:
        # "Do Not Bill Pt" appears twice: once with the CBO-address reason,
        # once with the unrelated HCA-group-insurance reason. Both must survive.
        registry = build_registry(_DOCX, _RULES)
        tag = registry.by_key("Do Not Bill Pt")
        assert tag is not None
        assert "CBO" in tag.reason
        assert "HCA" in tag.reason

    def test_blank_separator_row_is_skipped(self) -> None:
        registry = build_registry(_DOCX, _RULES)
        assert all(t.name.strip() for t in registry.tags)
        assert registry.by_key("") is None

    def test_header_row_is_not_a_tag(self) -> None:
        registry = build_registry(_DOCX, _RULES)
        assert registry.by_key("TAGS") is None
        assert registry.by_key("REASON") is None

    def test_every_toml_rule_names_a_registered_tag(self) -> None:
        # build_registry itself raises on an unknown name (see below) -- this
        # just asserts that path was actually exercised, not vacuously true
        # because the TOML is empty.
        registry = build_registry(_DOCX, _RULES)
        assert len(registry.rulable) > 0

    def test_manual_review_set_is_non_empty(self) -> None:
        registry = build_registry(_DOCX, _RULES)
        assert len(registry.manual_review) > 0
        names = {t.name for t in registry.manual_review}
        # SOP source document contradicts itself on both thresholds --
        # see PREBILLER_QUESTIONS.md. Must never be silently auto-assigned.
        assert "MCR Age" in names
        assert "MCD Minor" in names

    def test_no_epcr_is_rulable(self) -> None:
        registry = build_registry(_DOCX, _RULES)
        tag = registry.by_key("NO EPCR")
        assert tag is not None
        assert tag.rule is not None

    def test_unknown_tag_name_in_toml_raises(self, tmp_path: Path) -> None:
        bad_rules = tmp_path / "bad.toml"
        bad_rules.write_text('[[tag]]\nname = "Not A Real Tag"\ntext = "x"\n')
        with pytest.raises(RegistryError, match="Not A Real Tag"):
            build_registry(_DOCX, bad_rules)

    def test_missing_docx_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RegistryError):
            build_registry(tmp_path / "nope.docx", _RULES)

    def test_directory_path_raises_with_mount_hint(self, tmp_path: Path) -> None:
        bad_dir = tmp_path / "TAGS.docx"
        bad_dir.mkdir()
        with pytest.raises(RegistryError, match="is not a file"):
            build_registry(bad_dir, _RULES)

    def test_tags_docx_path_resolves_to_real_file(self, settings) -> None:
        path = settings.tags_docx_path
        assert path.is_file(), f"{path} must exist for registry load"

    def test_employer_tags_key_off_the_grounded_field_not_raw_ocr_text(self) -> None:
        # CAS Employee / Do Not Bill Patient / UTMB Employee ask "is the
        # *patient's* employer X" -- `employer_name` is an already-grounded,
        # LLM-extracted field for exactly that, and using it instead of a
        # `text` search over raw OCR closes a confirmed false positive: the
        # reporting agency's own name repeats in its letterhead on every page
        # it produces, which a raw-text search can't tell apart from the
        # patient's actual employer.
        registry = build_registry(_DOCX, _RULES)
        for name in ("CAS Employee", "Do Not Bill Patient", "UTMB Employee"):
            tag = registry.by_key(name)
            assert tag is not None and tag.rule is not None
            assert tag.rule.tree.get("field") == "employer_name", (
                f"{name!r} must match app.services.tagging's `field` leaf against "
                "employer_name, not a `text` search over raw OCR"
            )

    def test_morgue_tags_key_off_destination_facility_not_raw_ocr_text(self) -> None:
        # A morgue is a destination -- `destination_facility` is already an
        # extracted, grounded field. A `text` search over raw OCR could also
        # match an unselected destination-type legend printed on the form.
        registry = build_registry(_DOCX, _RULES)
        morgue = registry.by_key("Morgue")
        assert morgue is not None and morgue.rule is not None
        assert morgue.rule.tree.get("field") == "destination_facility"

        non_emergent = registry.by_key("Non-Emergent")
        assert non_emergent is not None and non_emergent.rule is not None
        any_branches = non_emergent.rule.tree["any"]
        morgue_branch = next(
            child
            for child in any_branches
            if "field" in child and child["field"] == "destination_facility"
        )
        assert morgue_branch["matches"] == "morgue"
        # Phase 8 item 1: wheelchair disjunct (billing SOP)
        wheelchair_branch = next(
            child
            for child in any_branches
            if "field" in child and child["field"] == "requested_level_of_service"
        )
        assert wheelchair_branch["matches"] == "wheelchair"

    def test_no_pcs_excludes_wheelchair_transports(self) -> None:
        # Knock-on guard: widening Non-Emergent to include wheelchair must not
        # widen NO PCS onto wheelchair claims -- the SOP exempts wheelchair.
        registry = build_registry(_DOCX, _RULES)
        no_pcs = registry.by_key("NO PCS")
        assert no_pcs is not None and no_pcs.rule is not None
        all_clauses = no_pcs.rule.tree["all"]
        none_clauses = [c for c in all_clauses if "none" in c]
        assert none_clauses, "NO PCS must contain a none[] wheelchair exclusion"
        wheelchair_excluded = any(
            leaf.get("field") == "requested_level_of_service"
            and leaf.get("matches") == "wheelchair"
            for clause in none_clauses
            for leaf in clause["none"]
        )
        assert wheelchair_excluded

    def test_pt_demos_correction_includes_age_conflict(self) -> None:
        # Phase 8 item 2: age added per billing SOP
        registry = build_registry(_DOCX, _RULES)
        tag = registry.by_key("Pt Demos Correction")
        assert tag is not None and tag.rule is not None
        conflict_fields = {
            child["field_conflict"]
            for child in tag.rule.tree["any"]
            if "field_conflict" in child
        }
        assert "age" in conflict_fields

    def test_wheelchair_transport_keys_off_requested_level_of_service_only(self) -> None:
        # `requested_level_of_service` is already grounded; the `text`
        # branch it used to be paired with could also match an unselected
        # equipment-checklist legend.
        registry = build_registry(_DOCX, _RULES)
        tag = registry.by_key("Wheelchair transport")
        assert tag is not None and tag.rule is not None
        assert tag.rule.tree == {"field": "requested_level_of_service", "matches": "wheelchair"}

    def test_hospice_and_dialysis_key_off_destination_facility(self) -> None:
        # TAGS.docx's own Hospice reason cites "Drop-off" as a valid source
        # alongside narrative -- `destination_facility` is already an
        # extracted, grounded field for exactly that.
        registry = build_registry(_DOCX, _RULES)
        for name in ("Hospice", "Dialysis"):
            tag = registry.by_key(name)
            assert tag is not None and tag.rule is not None
            assert tag.rule.tree.get("field") == "destination_facility"

    def test_mva_work_comp_and_psych_key_off_primary_impression(self) -> None:
        registry = build_registry(_DOCX, _RULES)
        for name in ("MVA/PI Quick bill", "Work comp", "Quickbill"):
            tag = registry.by_key(name)
            assert tag is not None and tag.rule is not None
            assert tag.rule.tree.get("field") == "primary_impression"

        psych = registry.by_key("PSYCH")
        assert psych is not None and psych.rule is not None
        impression_branch = next(child for child in psych.rule.tree["all"] if "field" in child)
        assert impression_branch == {"field": "primary_impression", "matches": "\\bpsych"}


class TestPredicateValidation:
    def test_bogus_field_fails_build(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text('[[tag]]\nname = "911"\nfield = "not_a_real_field"\nmatches = "x"\n')
        with pytest.raises(RegistryError, match="unknown field"):
            build_registry(_DOCX, bad)

    def test_bogus_insurance_field_fails_build(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text(
            '[[tag]]\nname = "911"\ninsurance = { slot = 0, field = "not_real", matches = "x" }\n'
        )
        with pytest.raises(RegistryError, match="unknown insurance field"):
            build_registry(_DOCX, bad)

    def test_out_of_range_insurance_slot_fails_build(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text(
            '[[tag]]\nname = "911"\n'
            'insurance = { slot = 9, field = "insurance_name", matches = "x" }\n'
        )
        with pytest.raises(RegistryError, match="out of range"):
            build_registry(_DOCX, bad)

    def test_bad_has_doc_type_fails_build(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text('[[tag]]\nname = "911"\nhas_doc_type = "not_a_type"\n')
        with pytest.raises(RegistryError, match="unknown document type"):
            build_registry(_DOCX, bad)

    def test_forward_requires_tag_fails_build(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text(
            '[[tag]]\nname = "Non-Emergent"\nall = [\n'
            '  { requires_tag = "NO PCS" },\n'
            '  { field = "transport_priority", matches = "P3" },\n'
            "]\n"
        )
        with pytest.raises(RegistryError, match="earlier in TAGS.docx"):
            build_registry(_DOCX, bad)

    def test_invalid_text_regex_fails_build(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text('[[tag]]\nname = "911"\ntext = "([unclosed"\n')
        with pytest.raises(RegistryError, match="invalid regex"):
            build_registry(_DOCX, bad)

    def test_invalid_field_regex_fails_build(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text(
            '[[tag]]\nname = "911"\nfield = "transport_priority"\nmatches = "([unclosed"\n'
        )
        with pytest.raises(RegistryError, match="invalid regex"):
            build_registry(_DOCX, bad)

    def test_invalid_insurance_regex_fails_build(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text(
            '[[tag]]\nname = "911"\n'
            'insurance = { slot = 0, field = "insurance_name", matches = "([unclosed" }\n'
        )
        with pytest.raises(RegistryError, match="invalid regex"):
            build_registry(_DOCX, bad)
