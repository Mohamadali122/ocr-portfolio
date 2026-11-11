import json

import pytest

from app.llm.base import LlmError, LlmTransientError
from app.models.document import DocumentType
from app.services import extraction_service
from app.services.extraction_service import ExtractionService
from tests.fakes import ExplodingLlmProvider, FakeLlmProvider

BOX = [[0.0, 0.0], [40.0, 0.0], [40.0, 10.0], [0.0, 10.0]]


def _flatten(text: str) -> str:
    """Collapse newlines/indentation so a multi-word phrase check doesn't
    depend on exactly where the prompt's source wraps a line."""
    return " ".join(text.lower().split())


def _page_texts(text: str, *, page: int = 1) -> list[dict]:
    return [{"page": page, "lines": [{"text": text, "confidence": 0.9, "box": BOX}]}]


class _SequencedLlmProvider:
    """Implements `LlmProvider`. Returns each response in order, one per call."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.calls = 0
        self.seen_users: list[str] = []

    @property
    def version(self) -> str:
        return "sequenced-llm/1.0"

    def complete(self, *, system: str, user: str, temperature: float = 0.0) -> str:
        self.seen_users.append(user)
        response = self._responses[self.calls]
        self.calls += 1
        return response


class TestGrounding:
    def test_value_present_verbatim_is_grounded_with_full_confidence(self) -> None:
        provider = FakeLlmProvider(response='{"last_name": {"value": "DOE", "source_page": 1}}')
        service = ExtractionService(provider)

        outcome = service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        field = outcome.fields.last_name
        assert field.value == "DOE"
        assert field.confidence == 1.0
        assert field.source_page == 1
        assert field.source_bbox == BOX
        assert outcome.repaired is False

    def test_value_present_with_different_case_and_whitespace_is_grounded_lower_confidence(
        self,
    ) -> None:
        provider = FakeLlmProvider(response='{"last_name": {"value": "doe"}}')
        service = ExtractionService(provider)

        outcome = service.extract(DocumentType.HFS, "DOE   JANE\n", _page_texts("  DOE   JANE  "))

        field = outcome.fields.last_name
        assert field.value == "doe"
        assert field.confidence == 0.7

    def test_value_split_across_ocr_lines_grounds_via_page_concatenation(self) -> None:
        # OCR split "123 MAIN ST HOUSTON" across two detected lines; no single
        # line contains it, but the page does. Third tier: 0.7, no bbox.
        provider = FakeLlmProvider(response='{"address_line1": {"value": "123 MAIN ST HOUSTON"}}')
        service = ExtractionService(provider)
        page_texts = [
            {
                "page": 1,
                "lines": [
                    {"text": "123 MAIN ST", "confidence": 0.9, "box": BOX},
                    {"text": "HOUSTON", "confidence": 0.9, "box": BOX},
                ],
            }
        ]

        outcome = service.extract(DocumentType.HFS, "123 MAIN ST\nHOUSTON\n", page_texts)

        field = outcome.fields.address_line1
        assert field.value == "123 MAIN ST HOUSTON"
        assert field.confidence == 0.7
        assert field.source_page == 1
        assert field.source_bbox is None  # no single line to point at

    def test_value_absent_from_ocr_text_is_nulled(self) -> None:
        # A hallucinated value: the model returned something that never
        # appears anywhere in the OCR text.
        provider = FakeLlmProvider(response='{"last_name": {"value": "SMITH"}}')
        service = ExtractionService(provider)

        outcome = service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        field = outcome.fields.last_name
        assert field.value is None
        assert field.confidence == 0.0
        assert field.source_page is None
        assert field.source_bbox is None

    def test_null_value_from_the_model_stays_null(self) -> None:
        provider = FakeLlmProvider(response='{"last_name": {"value": null}}')
        service = ExtractionService(provider)

        outcome = service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        assert outcome.fields.last_name.value is None
        assert outcome.fields.last_name.confidence == 0.0


class TestOutputWrapping:
    def test_markdown_fenced_json_is_unwrapped_without_a_repair_call(self) -> None:
        # ministral-14b's actual first-run output shape: fenced despite the
        # "no markdown" instruction. Must parse on the first attempt.
        provider = FakeLlmProvider(response='```json\n{"last_name": {"value": "DOE"}}\n```')
        service = ExtractionService(provider)

        outcome = service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        assert outcome.repaired is False
        assert outcome.fields.last_name.value == "DOE"

    def test_think_block_before_json_is_stripped(self) -> None:
        provider = FakeLlmProvider(
            response='<think>the name is DOE</think>\n{"last_name": {"value": "DOE"}}'
        )
        service = ExtractionService(provider)

        outcome = service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        assert outcome.repaired is False
        assert outcome.fields.last_name.value == "DOE"

    def test_think_block_logs_a_char_count_signal_never_the_reasoning_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Thinking is meant to be off (an LM Studio model setting, trusted not
        # enforced); this is the tripwire if it silently drifts back on.
        logged: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            extraction_service.logger,
            "warning",
            lambda event, **kw: logged.append((event, kw)),
        )
        think_block = "<think>the name is DOE</think>"
        provider = FakeLlmProvider(response=f'{think_block}\n{{"last_name": {{"value": "DOE"}}}}')
        service = ExtractionService(provider)

        service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        assert logged == [("llm_thinking_detected", {"chars": len(think_block)})]

    def test_no_think_block_logs_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        logged: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            extraction_service.logger,
            "warning",
            lambda event, **kw: logged.append((event, kw)),
        )
        provider = FakeLlmProvider(response='{"last_name": {"value": "DOE"}}')
        service = ExtractionService(provider)

        service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        assert logged == []


class TestNormalizationIntegration:
    def test_extract_fills_typed_value_and_source(self) -> None:
        # A grounded EPCR-format DOB must survive the guard, then get a typed
        # MM/DD/YYYY value tagged `reformatted` -- the raw stays proven on the page.
        provider = FakeLlmProvider(response='{"dob": {"value": "1958/04/17"}}')
        service = ExtractionService(provider)

        outcome = service.extract(
            DocumentType.EPCR, "DOB 1958/04/17\n", _page_texts("DOB 1958/04/17")
        )

        dob = outcome.fields.dob
        assert dob.value == "1958/04/17"  # raw, guard-proven
        assert dob.confidence == 1.0
        assert dob.typed_value == "04/17/1958"  # what the prebiller types
        assert dob.source.value == "reformatted"

    def test_absent_dob_emits_sop_default_without_faking_a_reading(self) -> None:
        service = ExtractionService(FakeLlmProvider(response="{}"))
        outcome = service.extract(DocumentType.HFS, "no dob here\n", _page_texts("no dob here"))

        dob = outcome.fields.dob
        assert dob.value is None  # nothing was read off the page
        assert dob.confidence == 0.0
        assert dob.source_bbox is None
        assert dob.typed_value == "01/01/1900"  # SOP default
        assert dob.source.value == "sop_default"


class TestRepairRetry:
    def test_malformed_json_triggers_one_repair_retry_then_succeeds(self) -> None:
        provider = _SequencedLlmProvider(["not json at all", '{"last_name": {"value": "DOE"}}'])
        service = ExtractionService(provider)

        outcome = service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        assert provider.calls == 2
        assert outcome.repaired is True
        assert outcome.fields.last_name.value == "DOE"

    def test_malformed_json_after_repair_retry_raises(self) -> None:
        provider = _SequencedLlmProvider(["not json", "still not json"])
        service = ExtractionService(provider)

        with pytest.raises(LlmError) as excinfo:
            service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        assert provider.calls == 2
        # The ValidationError must NOT be chained: pydantic's message embeds
        # `input_value=` -- the LLM's output, PHI -- and PipelineService logs
        # this exception with exc_info=True, rendering the whole chain.
        assert excinfo.value.__cause__ is None
        assert "input_value" not in repr(excinfo.value)

    def test_provider_failure_propagates_as_llm_error(self) -> None:
        service = ExtractionService(ExplodingLlmProvider())

        with pytest.raises(LlmError):
            service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

    def test_truncated_json_triggers_one_repair_retry_then_succeeds(self) -> None:
        # The actual live failure mode: a large document's completion runs out
        # of output budget mid-value ("EOF while parsing"), not garbage text.
        truncated = '{"last_name": {"value": "DOE'
        provider = _SequencedLlmProvider([truncated, '{"last_name": {"value": "DOE"}}'])
        service = ExtractionService(provider)

        outcome = service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        assert outcome.repaired is True
        assert outcome.fields.last_name.value == "DOE"

    def test_truncated_json_on_both_attempts_raises_plain_llm_error_not_transient(self) -> None:
        # Retrying a deterministic (temperature=0.0) prompt reproduces the same
        # truncation -- this must never be mistaken for the transport failure
        # Celery's autoretry_for is allowed to retry (app/worker.py).
        truncated = '{"last_name": {"value": "DOE'
        provider = _SequencedLlmProvider([truncated, truncated])
        service = ExtractionService(provider)

        with pytest.raises(LlmError) as excinfo:
            service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        assert not isinstance(excinfo.value, LlmTransientError)

    def test_repair_prompt_caps_the_echoed_previous_response(self) -> None:
        # A full truncated response echoed back into the repair prompt is what
        # made the repair overflow its own output budget and truncate again.
        # It must be capped, not passed through whole.
        huge_response = '{"last_name": {"value": "' + ("A" * 5000)
        provider = _SequencedLlmProvider([huge_response, '{"last_name": {"value": "DOE"}}'])
        service = ExtractionService(provider)

        service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        repair_user = provider.seen_users[1]
        assert huge_response not in repair_user
        assert len(repair_user) < len(huge_response)


class TestEmptyResponseRetry:
    """X1: retry a well-formed but fully-empty LLM response."""

    def test_empty_response_triggers_exactly_one_retry(self) -> None:
        # {} validates cleanly -- every field has a default. populated=0 must
        # trigger the empty-response repair path, not the validation-error path.
        populated_response = '{"last_name": {"value": "DOE"}}'
        provider = _SequencedLlmProvider(["{}", populated_response])
        service = ExtractionService(provider)

        outcome = service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        assert provider.calls == 2
        assert outcome.repaired is True
        assert outcome.fields.last_name.value == "DOE"

    def test_empty_then_empty_returns_without_further_retry(self) -> None:
        # An honest near-blank page: two empty responses → repaired=True,
        # gate will fail it. Must not loop.
        provider = _SequencedLlmProvider(["{}", "{}"])
        service = ExtractionService(provider)

        outcome = service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        assert provider.calls == 2
        assert outcome.repaired is True
        assert outcome.fields.last_name.value is None

    def test_populated_response_is_not_retried(self) -> None:
        populated_response = '{"last_name": {"value": "DOE"}}'
        provider = _SequencedLlmProvider([populated_response])
        service = ExtractionService(provider)

        outcome = service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        assert provider.calls == 1
        assert outcome.repaired is False

    def test_empty_retry_prompt_differs_from_validation_error_prompt(self) -> None:
        # The repair prompt for an empty response must not mention schema
        # validation -- the schema was fine, the model just didn't fill it.
        populated_response = '{"last_name": {"value": "DOE"}}'
        provider = _SequencedLlmProvider(["{}", populated_response])
        service = ExtractionService(provider)

        service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        repair_user = provider.seen_users[1]
        assert "no extracted values" in repair_user.lower()
        assert "schema validation" not in repair_user.lower()

    def test_empty_retry_logs_warning_with_populated_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            extraction_service.logger,
            "warning",
            lambda event, **kw: warnings.append((event, kw)),
        )
        populated_response = '{"last_name": {"value": "DOE"}}'
        provider = _SequencedLlmProvider(["{}", populated_response])
        service = ExtractionService(provider)

        service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        events = [e for e, _ in warnings]
        assert "llm_response_empty_retrying" in events
        kw = next(kw for e, kw in warnings if e == "llm_response_empty_retrying")
        assert kw["populated"] == 0

    def test_llm_response_shape_logged_with_retry_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        infos: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            extraction_service.logger,
            "info",
            lambda event, **kw: infos.append((event, kw)),
        )
        populated_response = '{"last_name": {"value": "DOE"}}'
        provider = _SequencedLlmProvider(["{}", populated_response])
        service = ExtractionService(provider)

        service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        shape_events = [(e, kw) for e, kw in infos if e == "llm_response_shape"]
        assert shape_events, "llm_response_shape must be logged"
        _, kw = shape_events[0]
        assert kw["retried_empty"] is True
        assert "raw_chars" in kw
        assert "stripped_chars" in kw
        assert "populated" in kw

    def test_llm_response_shape_logged_on_success_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        infos: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            extraction_service.logger,
            "info",
            lambda event, **kw: infos.append((event, kw)),
        )
        provider = FakeLlmProvider(response='{"last_name": {"value": "DOE"}}')
        service = ExtractionService(provider)

        service.extract(DocumentType.HFS, "DOE JANE\n", _page_texts("DOE JANE"))

        shape_events = [(e, kw) for e, kw in infos if e == "llm_response_shape"]
        assert shape_events
        _, kw = shape_events[0]
        assert kw["retried_empty"] is False
        assert kw["populated"] >= 1


class TestNarrativeSummary:
    def test_summary_text_is_returned_stripped(self) -> None:
        provider = FakeLlmProvider(response="```\n- Non-emergent transfer to rehab.\n```")
        service = ExtractionService(provider)
        assert service.summarize_narrative("...") == "- Non-emergent transfer to rehab."

    def test_none_sentinel_means_no_summary(self) -> None:
        service = ExtractionService(FakeLlmProvider(response="NONE"))
        assert service.summarize_narrative("...") is None

    def test_provider_failure_yields_none_not_an_exception(self) -> None:
        # Best-effort: a failed summary must never fail the document.
        service = ExtractionService(ExplodingLlmProvider())
        assert service.summarize_narrative("...") is None

    def test_runaway_completion_is_truncated(self) -> None:
        from app.services.extraction_service import MAX_NARRATIVE_SUMMARY_CHARS

        service = ExtractionService(FakeLlmProvider(response="x" * 10_000))
        summary = service.summarize_narrative("...")
        assert summary is not None
        assert len(summary) == MAX_NARRATIVE_SUMMARY_CHARS


class TestPrompt:
    def test_system_prompt_forbids_weight_unit_conversion(self) -> None:
        # The field is named weight_kg but US forms print pounds; without this
        # instruction the model converts, and the guard nulls the converted
        # value (audit F5).
        system, _ = ExtractionService(FakeLlmProvider()).build_prompt(DocumentType.EPCR, "x")
        assert "never convert units" in system.lower()

    def test_hfs_and_epcr_prompts_differ(self) -> None:
        service = ExtractionService(FakeLlmProvider())
        _, hfs_user = service.build_prompt(DocumentType.HFS, "text")
        _, epcr_user = service.build_prompt(DocumentType.EPCR, "text")
        assert hfs_user != epcr_user

    def test_every_doc_type_has_a_distinct_hint(self) -> None:
        service = ExtractionService(FakeLlmProvider())
        hints = {doc_type: service.build_prompt(doc_type, "text")[1] for doc_type in DocumentType}
        assert len(set(hints.values())) == len(DocumentType)

    def test_hfs_hint_never_tells_the_model_to_null_trip_fields(self) -> None:
        # Regression guard for the contradiction this hint used to have: it
        # once said "leave them null" for trip fields in the same breath as
        # the mixed-doc caveat saying "extract regardless of section" --
        # directly contradictory on an HFS+EPCR mixed scan.
        _, hfs_user = ExtractionService(FakeLlmProvider()).build_prompt(DocumentType.HFS, "x")
        assert "leave them null" not in hfs_user

    def test_system_prompt_treats_ocr_as_untrusted(self) -> None:
        system, _ = ExtractionService(FakeLlmProvider()).build_prompt(DocumentType.HFS, "x")
        assert "untrusted" in system.lower()

    def test_system_prompt_forbids_placeholder_strings(self) -> None:
        system, _ = ExtractionService(FakeLlmProvider()).build_prompt(DocumentType.HFS, "x")
        assert '"n/a"' in _flatten(system)

    def test_system_prompt_forbids_including_field_labels_in_values(self) -> None:
        system, _ = ExtractionService(FakeLlmProvider()).build_prompt(DocumentType.HFS, "x")
        assert "patient name: john smith" in _flatten(system)

    def test_system_prompt_allows_a_legitimately_repeated_value(self) -> None:
        # A shared phone number for two different, correctly-labeled fields
        # (e.g. patient and next-of-kin) must not be nulled just because it
        # repeats -- only an *unresolvable* association should be nulled.
        system, _ = ExtractionService(FakeLlmProvider()).build_prompt(DocumentType.HFS, "x")
        assert "do not null a field solely because its value repeats" in _flatten(system)

    def test_system_prompt_states_insurance_ordering_rule(self) -> None:
        system, _ = ExtractionService(FakeLlmProvider()).build_prompt(DocumentType.HFS, "x")
        assert "primary/secondary/tertiary" in _flatten(system)

    def test_system_prompt_requires_empty_list_for_absent_insurance(self) -> None:
        system, _ = ExtractionService(FakeLlmProvider()).build_prompt(DocumentType.HFS, "x")
        assert "empty insurance list" in _flatten(system)

    def test_system_prompt_covers_checkbox_selection_fields(self) -> None:
        system, _ = ExtractionService(FakeLlmProvider()).build_prompt(DocumentType.HFS, "x")
        assert "checkbox" in _flatten(system)

    def test_system_and_narrative_prompts_forbid_think_tags(self) -> None:
        system, _ = ExtractionService(FakeLlmProvider()).build_prompt(DocumentType.HFS, "x")
        assert "<think>" in _flatten(system)
        assert "<think>" in _flatten(extraction_service._NARRATIVE_SYSTEM_PROMPT)

    def test_extraction_schema_is_valid_json_not_a_python_repr(self) -> None:
        # The original prompt interpolated the schema dict directly into an
        # f-string, rendering Python's repr (single quotes, None/True/False)
        # instead of JSON (double quotes, null/true/false) -- a different
        # literal dialect than what the model is asked to produce.
        assert "None" not in extraction_service._EXTRACTION_SCHEMA
        json.loads(extraction_service._EXTRACTION_SCHEMA)  # must not raise

    def test_narrative_prompt_has_ambiguous_urgency_fallback(self) -> None:
        narrative_prompt = extraction_service._NARRATIVE_SYSTEM_PROMPT
        assert "urgency unclear from the narrative" in _flatten(narrative_prompt)


class TestInsuranceGrounding:
    def test_insurance_slot_fields_are_grounded_independently(self) -> None:
        provider = FakeLlmProvider(
            response=(
                '{"insurance": [{"insurance_name": {"value": "BCBS OF TEXAS"}, '
                '"policy_id": {"value": "FAKE-NOT-ON-PAGE"}}]}'
            )
        )
        service = ExtractionService(provider)

        outcome = service.extract(DocumentType.HFS, "BCBS OF TEXAS\n", _page_texts("BCBS OF TEXAS"))

        slot = outcome.fields.insurance[0]
        assert slot.insurance_name.value == "BCBS OF TEXAS"
        assert slot.insurance_name.confidence == 1.0
        # Not on the page -> the hallucination guard nulls it, same as any
        # top-level field.
        assert slot.policy_id.value is None
        assert slot.policy_id.confidence == 0.0

    def test_no_insurance_in_response_is_an_empty_list(self) -> None:
        service = ExtractionService(FakeLlmProvider(response="{}"))
        outcome = service.extract(DocumentType.HFS, "text\n", _page_texts("text"))
        assert outcome.fields.insurance == []
