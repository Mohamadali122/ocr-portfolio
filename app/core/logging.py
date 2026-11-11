"""Structured logging with a PHI redaction chokepoint.

Every log record in this process -- ours, uvicorn's, SQLAlchemy's -- passes
through `redact_processor` before it is rendered. Redaction is a property of the
pipeline, not of the discipline of whoever wrote the log call.

Two independent layers, because either alone is insufficient:

1. Key-based. A value under a known-PHI key is dropped wholesale, regardless of
   what it looks like. Catches free text (narratives, OCR dumps) that no regex
   can match.
2. Pattern-based. Every remaining string is scanned for PHI-shaped substrings.
   Catches PHI that arrives under an innocent key, which is how it always
   arrives in practice -- inside an exception message.

Both layers fail closed: an unrecognised structure is recursed into, not passed
through.
"""

import logging
import logging.config
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

from app.core.config import Settings

REDACTED = "<redacted>"

# Values under these keys are never rendered, whatever their shape. Compared
# case-insensitively against the full key, and against its trailing segment so
# that `patient.last_name` is caught by `last_name`.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        # Credentials. Not PHI, but a rendered bearer token in a log file is a
        # 15-minute account takeover, and a rendered password is a permanent one.
        "access_token",
        "authorization",
        "jwt",
        "password",
        "password_hash",
        "refresh_token",
        "secret",
        "token",
        # PHI. Every member of `app.schemas.extraction.FIELD_NAMES` and
        # `INSURANCE_FIELD_NAMES` belongs here -- asserted by
        # tests/unit/test_phi_logging.py, so schema growth that forgets this
        # set fails a test instead of leaking.
        "account_number",
        "actual_distance",
        "actual_mileage",
        "address",
        "address_line1",
        "address_line2",
        "address_line_1",
        "address_line_2",
        "admission_type",
        "age",
        "als_assessment_performed",
        "arrived_at_patient_datetime",
        "authorizing_physician",
        "case_number",
        "chief_complaint",
        "city",
        "cms_service_level",
        "complaint",
        "destination",
        "destination_facility",
        "diagnosis",
        "dispatch_number",
        "dob",
        "date_of_birth",
        "date_of_service",
        "effective_date",
        "email",
        # Claim tagging (app/services/tagging.py, app/services/claim_merge.py):
        # a rule's evidence carries matched text and field values, and the
        # merge's conflict list carries a document's typed value under a
        # neutral-looking key. Same discipline as every other PHI key here.
        "evidence",
        "snippet",
        "matched_text",
        "merged_text",
        "claim_text",
        "values",
        "employer_name",
        "employment_status",
        "ethnicity",
        "extraction",
        "facility_pay_reason",
        "file_name",
        "filename",
        "first_name",
        "gender",
        "group_number",
        "guarantor",
        "guarantor_address",
        "guarantor_dob",
        "guarantor_first_name",
        "guarantor_last_name",
        "guarantor_occupation",
        "guarantor_phone",
        "guarantor_relationship",
        "guarantor_ssn",
        "guardian_name",
        "guardian_relationship",
        "incident_location",
        "insurance",
        "insurance_name",
        "last_name",
        "legal_hold_status",
        "marital_status",
        "middle_name",
        "mrn",
        "narrative",
        "narrative_summary",
        "national_origin",
        "next_of_kin_name",
        "next_of_kin_phone",
        "notifier_name",
        "notifier_phone",
        "ocr_text",
        "original_filename",
        "page_texts",
        "patient",
        "patient_arrived_destination_datetime",
        "patient_name",
        "payer_address",
        "payer_phone",
        "payload",
        "pcp_name",
        "pcs_date_of_sign",
        "pcs_date_of_transfer",
        "phone",
        "phone_home",
        "phone_mobile",
        "phone_number",
        "phone_work",
        "physician_title",
        "pickup_address",
        "policy_id",
        "preferred_pronouns",
        "primary_impression",
        "protocol_name",
        "pt_class",
        "race",
        "raw_text",
        "relationship",
        "relationship_to_subscriber",
        "religion",
        "requested_level_of_service",
        "response_urgency",
        "run_number",
        "signature",
        "ssn",
        "state",
        "subscriber",
        "subscriber_name",
        "suffix",
        "transport_priority",
        "unit_arrived_scene_datetime",
        "unit_back_in_service_datetime",
        "unit_en_route_datetime",
        "unit_left_scene_datetime",
        "unit_number",
        "verified",
        "weight_kg",
        "zip",
        "zip_code",
    }
)

# Keys skipped by the pattern scanner. Machine-generated, structurally incapable
# of holding PHI. Skipping them is a performance concession, not a safety one.
_UNSCANNED_KEYS: frozenset[str] = frozenset({"timestamp", "level", "logger", "lineno"})

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Dashed SSN. Matched first so its digits are gone before the phone rule runs.
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "<ssn>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\b(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b"), "<phone>"),
    # MM/DD/YYYY (face sheets) and YYYY/MM/DD (EPCR). Deliberately does not match
    # the ISO-8601 hyphenated timestamps this logger emits.
    (re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"), "<date>"),
    (re.compile(r"\b\d{4}/\d{1,2}/\d{1,2}\b"), "<date>"),
    # Bare 9-digit run: undashed SSN, and the Medicaid-of-Texas policy ID format.
    (re.compile(r"\b\d{9}\b"), "<id>"),
)


def _scrub_text(value: str) -> str:
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return lowered in SENSITIVE_KEYS or lowered.rsplit(".", 1)[-1] in SENSITIVE_KEYS


def _redact(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _is_sensitive(key):
        return REDACTED
    if key is not None and key.lower() in _UNSCANNED_KEYS:
        return value
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, Mapping):
        return {k: _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return type(value)(_redact(item) for item in value)
    return value


def redact_processor(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """structlog processor. Applied last, so it also sees rendered tracebacks."""
    return {k: _redact(v, key=str(k)) for k, v in event_dict.items()}


def configure_logging(settings: Settings) -> None:
    """Route stdlib logging and structlog through one redacted pipeline."""
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    # Applied to records originating outside structlog (uvicorn, SQLAlchemy).
    foreign_pre_chain: list[Any] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        timestamper,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "structured": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "foreign_pre_chain": foreign_pre_chain,
                    "processors": [
                        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                        structlog.processors.format_exc_info,
                        # Last before rendering: the traceback string is now in the
                        # event dict and gets scrubbed like anything else.
                        redact_processor,
                        renderer,
                    ],
                }
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "structured",
                }
            },
            "root": {"handlers": ["default"], "level": settings.log_level},
            "loggers": {
                # Replaced by AccessLogMiddleware, which logs the path without its
                # query string. Uvicorn's own access log would print
                # `?patient_name=...` straight into the log stream.
                "uvicorn.access": {"handlers": [], "propagate": False},
                "uvicorn.error": {"level": settings.log_level, "propagate": True},
                "sqlalchemy.engine": {
                    "level": "INFO" if settings.db_echo else "WARNING",
                    "propagate": True,
                },
            },
        }
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            timestamper,
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
