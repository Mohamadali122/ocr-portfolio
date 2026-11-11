"""The PHI redaction pipeline.

These assertions are a HIPAA control, not a style preference. If one of them
starts failing, the fix is the code under test, never the test.
"""

import json
import logging

import pytest

from app.core.config import Settings
from app.core.logging import (
    REDACTED,
    SENSITIVE_KEYS,
    configure_logging,
    get_logger,
    redact_processor,
)
from app.schemas.extraction import FIELD_NAMES, INSURANCE_FIELD_NAMES

# A synthetic patient. Every value here is PHI under 45 CFR 160.103 and must not
# survive a round trip through the logger.
SSN_DASHED = "123-45-6789"
SSN_BARE = "123456789"
PHONE = "(713) 555-0142"
EMAIL = "jane.doe@example.com"
DOB_US = "04/17/1958"
DOB_EPCR = "1958/04/17"
NARRATIVE = "Pt found supine, c/o chest pain, transported to Methodist ER."


def _apply(event_dict: dict) -> dict:
    return dict(redact_processor(None, "info", event_dict))


class TestPatternLayer:
    """Catches PHI arriving under an innocent key -- typically inside a message."""

    @pytest.mark.parametrize(
        ("raw", "expected_token"),
        [
            (SSN_DASHED, "<ssn>"),
            (SSN_BARE, "<id>"),
            (PHONE, "<phone>"),
            (EMAIL, "<email>"),
            (DOB_US, "<date>"),
            (DOB_EPCR, "<date>"),
        ],
    )
    def test_pattern_is_replaced_anywhere_in_a_string(self, raw: str, expected_token: str) -> None:
        result = _apply({"event": f"lookup failed for {raw} during import"})
        assert raw not in result["event"]
        assert expected_token in result["event"]

    def test_recurses_into_nested_containers(self) -> None:
        result = _apply({"event": "ok", "rows": [{"note": f"call {PHONE}"}]})
        assert PHONE not in json.dumps(result)

    def test_iso_timestamp_is_not_mistaken_for_a_date_of_birth(self) -> None:
        # The date patterns match slash-separated dates only. Hyphenated ISO-8601
        # instants, which every log line carries, must survive intact.
        stamp = "2026-07-10T14:22:05.123456Z"
        assert _apply({"timestamp": stamp, "event": stamp}) == {
            "timestamp": stamp,
            "event": stamp,
        }


class TestKeyLayer:
    """Catches free text that no pattern can match -- names, narratives, OCR dumps."""

    @pytest.mark.parametrize(
        "key",
        [
            "raw_text",
            "narrative",
            "patient_name",
            "last_name",
            "payload",
            "filename",
            # Claim tagging (app/services/tagging.py, app/services/claim_merge.py):
            # evidence carries matched text and field values under these keys.
            "evidence",
            "snippet",
            "matched_text",
            "merged_text",
            "claim_text",
            "values",
        ],
    )
    def test_sensitive_key_is_dropped_wholesale(self, key: str) -> None:
        assert _apply({key: NARRATIVE})[key] == REDACTED

    def test_key_matching_is_case_insensitive_and_matches_trailing_segment(self) -> None:
        result = _apply({"SSN": SSN_DASHED, "patient.last_name": "Testerson"})
        assert result["SSN"] == REDACTED
        assert result["patient.last_name"] == REDACTED

    def test_a_surname_under_a_neutral_key_is_not_caught(self) -> None:
        # Documents the limit of the design, so nobody mistakes it for total
        # coverage. A patient surname is indistinguishable from a facility name
        # or a developer's name; only the key tells them apart. This is why
        # SENSITIVE_KEYS exists and why new PHI-bearing keys must be added to it.
        assert _apply({"detail": "Testerson"})["detail"] == "Testerson"

    def test_every_extraction_field_name_is_a_sensitive_key(self) -> None:
        # The maintenance obligation from CLAUDE.md, made mechanical: when the
        # extraction schema grows a field, its key must join SENSITIVE_KEYS in
        # the same change. A bare surname under a forgotten key is exactly the
        # leak the key layer exists to stop (see the test above).
        missing = (set(FIELD_NAMES) | set(INSURANCE_FIELD_NAMES)) - SENSITIVE_KEYS
        assert not missing, f"extraction fields absent from SENSITIVE_KEYS: {sorted(missing)}"


class TestEndToEndThroughStdlib:
    """The pipeline as it actually runs: stdlib record -> processors -> stdout."""

    @staticmethod
    def _configure(settings: Settings) -> None:
        # Must run inside the test body, not in a fixture. dictConfig resolves
        # `ext://sys.stdout` to a concrete stream object exactly once, and pytest
        # installs a different capture buffer for the setup phase than for the
        # call phase. Configuring during setup binds the handler to a stream
        # `capsys` will never read.
        configure_logging(settings.model_copy(update={"log_format": "json"}))

    def test_no_phi_reaches_stdout(
        self, settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._configure(settings)
        get_logger("test").info(
            "extraction_completed",
            document_id="d290f1ee-6c54-4b01-90e6-d701748f0851",
            raw_text=NARRATIVE,
            contact=f"{EMAIL} / {PHONE}",
            ssn=SSN_DASHED,
        )
        out = capsys.readouterr().out

        for secret in (NARRATIVE, EMAIL, PHONE, SSN_DASHED):
            assert secret not in out
        # The non-PHI identifier survives, or the log would be useless.
        assert "d290f1ee-6c54-4b01-90e6-d701748f0851" in out
        assert "extraction_completed" in out

    def test_traceback_from_a_third_party_logger_is_scrubbed(
        self, settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._configure(settings)
        # Simulates SQLAlchemy raising with a row value in the message -- the most
        # common real-world PHI leak, and one no call-site discipline prevents.
        try:
            raise ValueError(f"duplicate key: ssn={SSN_DASHED}, dob={DOB_US}")
        except ValueError:
            logging.getLogger("sqlalchemy.engine").exception("statement failed")

        out = capsys.readouterr().out
        assert SSN_DASHED not in out
        assert DOB_US not in out
        assert "<ssn>" in out
        assert "statement failed" in out
