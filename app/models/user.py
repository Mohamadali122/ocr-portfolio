import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class UserRole(StrEnum):
    """The pilot seeds ADMIN only. The rest exist so that adding a user is an
    INSERT rather than a migration -- see the actor table in the Phase 0 plan."""

    ADMIN = "admin"
    PREBILLER = "prebiller"
    SUPERVISOR = "supervisor"
    AUDITOR = "auditor"


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # native_enum=False renders VARCHAR + CHECK. A Postgres ENUM would require
    # ALTER TYPE ... ADD VALUE to grow, which cannot run inside a transaction and
    # therefore cannot run inside a normal Alembic migration.
    #
    # create_constraint=True is not the default. Without it this is a bare
    # VARCHAR(32) that happily stores 'wizard', and the enum exists only in
    # Python -- which is no help to psql, to a restore, or to the next service.
    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            native_enum=False,
            length=32,
            create_constraint=True,
            name="userrole",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=UserRole.ADMIN,
        server_default=UserRole.ADMIN.value,
        nullable=False,
    )

    # server_default on each: a row inserted by psql or a restore script must not
    # depend on the ORM to be valid.
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )

    # Incrementing this invalidates every token the user currently holds.
    token_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        # Deliberately omits email. A repr leaks into exception messages and logs.
        return f"<User id={self.id} role={self.role}>"
