import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.user import User


class UserRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self._session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        # Emails are stored and queried lowercase; normalization happens in the
        # Pydantic schema so it cannot be forgotten at one call site.
        stmt = select(User).where(User.email == email.lower())
        return self._session.execute(stmt).scalar_one_or_none()

    def add(self, user: User) -> User:
        self._session.add(user)
        self._session.flush()  # populates user.id without ending the transaction
        return user

    def count(self) -> int:
        return self._session.execute(select(func.count()).select_from(User)).scalar_one()
