"""widen documenttype: add pcs, booking_summary

Revision ID: 8a1f3c9d2b47
Revises: b6212144c3f8
Create Date: 2026-07-14 16:00:00.000000+00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "8a1f3c9d2b47"
down_revision: str | None = "b6212144c3f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_VALUES = ("hfs", "epcr", "unknown")
_NEW_VALUES = ("hfs", "epcr", "pcs", "booking_summary", "unknown")
_CONSTRAINT = "documenttype"  # Alembic re-applies Base.metadata's naming
# convention to whatever string is passed here, expanding it to
# "ck_documents_documenttype" -- passing the already-expanded name doubles
# the prefix (see PHASE5_REDESIGN.md history for the failed first attempt).


def _values_sql(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    # VARCHAR + CHECK, not a Postgres ENUM (see app/models/document.py) --
    # widening the allowed set is a plain constraint swap, no ALTER TYPE, so
    # this runs inside the normal transactional migration.
    op.drop_constraint(_CONSTRAINT, "documents", type_="check")
    op.create_check_constraint(
        _CONSTRAINT, "documents", f"doc_type IN ({_values_sql(_NEW_VALUES)})"
    )


def downgrade() -> None:
    # A downgrade with existing 'pcs'/'booking_summary' rows would violate the
    # narrowed constraint immediately -- that is correct: those rows must be
    # reclassified (or the documents deleted) before this can downgrade, the
    # same way any narrowing migration behaves.
    op.drop_constraint(_CONSTRAINT, "documents", type_="check")
    op.create_check_constraint(
        _CONSTRAINT, "documents", f"doc_type IN ({_values_sql(_OLD_VALUES)})"
    )
