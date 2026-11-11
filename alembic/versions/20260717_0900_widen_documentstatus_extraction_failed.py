"""widen documentstatus: add extraction_failed

Revision ID: 0d027cd96ddb
Revises: 8a1f3c9d2b47
Create Date: 2026-07-17 09:00:00.000000+00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0d027cd96ddb"
down_revision: str | None = "8a1f3c9d2b47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_VALUES = ("pending", "ocr_running", "ai_running", "completed", "failed")
_NEW_VALUES = ("pending", "ocr_running", "ai_running", "completed", "failed", "extraction_failed")
_CONSTRAINT = "documentstatus"  # Alembic re-applies Base.metadata's naming
# convention to whatever string is passed here, expanding it to
# "ck_documents_documentstatus" -- passing the already-expanded name doubles
# the prefix (see PHASE5_REDESIGN.md history for the failed first attempt,
# and the 8a1f3c9d2b47 migration this one mirrors).


def _values_sql(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    # VARCHAR + CHECK, not a Postgres ENUM (see app/models/document.py) --
    # widening the allowed set is a plain constraint swap, no ALTER TYPE, so
    # this runs inside the normal transactional migration.
    op.drop_constraint(_CONSTRAINT, "documents", type_="check")
    op.create_check_constraint(
        _CONSTRAINT, "documents", f"status IN ({_values_sql(_NEW_VALUES)})"
    )


def downgrade() -> None:
    # A downgrade with existing 'extraction_failed' rows would violate the
    # narrowed constraint immediately -- correct: those rows must be
    # reclassified (or the documents deleted) before this can downgrade, the
    # same way any narrowing migration behaves.
    op.drop_constraint(_CONSTRAINT, "documents", type_="check")
    op.create_check_constraint(
        _CONSTRAINT, "documents", f"status IN ({_values_sql(_OLD_VALUES)})"
    )
