import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.extraction import Extraction
from app.schemas.extraction import FieldSource


class ExtractionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_document_id(self, document_id: uuid.UUID) -> Extraction | None:
        stmt = select(Extraction).where(Extraction.document_id == document_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def set_typed_value(
        self, extraction: Extraction, field_name: str, typed_value: str | None
    ) -> Extraction:
        """Overwrite one field's `typed_value` with a reviewer's correction.

        `field_name` is either a top-level field (`"last_name"`) or an
        insurance-slot path (`"insurance.0.policy_id"`) -- callers validate
        the path against `FIELD_NAMES`/`INSURANCE_FIELD_NAMES` before this is
        reached; see `documents.correct_extraction_field`.

        Reassigns the whole `fields` dict (and, for an insurance path, the
        whole `insurance` list and the whole slot within it) rather than
        mutating nested `extraction.fields[...]` in place -- SQLAlchemy does
        not dirty-track an in-place mutation of a JSONB column, so an
        in-place write here would silently never reach the database.
        """
        fields: dict[str, Any] = dict(extraction.fields)
        parts = field_name.split(".")
        if len(parts) == 1:
            field: dict[str, Any] = dict(fields[field_name])
            field["typed_value"] = typed_value
            field["source"] = FieldSource.CORRECTED.value
            fields[field_name] = field
        else:
            _, index_str, subfield = parts
            insurance: list[Any] = list(fields.get("insurance", []))
            slot: dict[str, Any] = dict(insurance[int(index_str)])
            slot_field: dict[str, Any] = dict(slot[subfield])
            slot_field["typed_value"] = typed_value
            slot_field["source"] = FieldSource.CORRECTED.value
            slot[subfield] = slot_field
            insurance[int(index_str)] = slot
            fields["insurance"] = insurance
        extraction.fields = fields
        self._session.flush()
        return extraction

    def count_corrected_fields(self, extraction: Extraction) -> int:
        """How many fields (top-level or an insurance-slot subfield) carry a
        reviewer's correction (`source == "corrected"`, set by `set_typed_value`).

        `add()` deletes-then-inserts the whole row on every pipeline re-run,
        which would silently discard every correction along with it -- this
        is what `documents.reprocess_document` checks before allowing that."""
        count = 0
        for key, value in extraction.fields.items():
            if key == "insurance":
                for slot in value:
                    count += sum(
                        1
                        for field in slot.values()
                        if isinstance(field, dict)
                        and field.get("source") == FieldSource.CORRECTED.value
                    )
            elif isinstance(value, dict) and value.get("source") == FieldSource.CORRECTED.value:
                count += 1
        return count

    def add(self, extraction: Extraction) -> Extraction:
        # document_id is unique -- a re-run (e.g. retrying a document that failed
        # after extraction started) replaces the prior row rather than colliding
        # with it, same convention as OcrRepository.
        self._session.execute(
            delete(Extraction).where(Extraction.document_id == extraction.document_id)
        )
        self._session.add(extraction)
        self._session.flush()
        return extraction
