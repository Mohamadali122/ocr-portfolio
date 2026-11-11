import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.ocr_result import OcrResult


class OcrRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_document_id(self, document_id: uuid.UUID) -> OcrResult | None:
        stmt = select(OcrResult).where(OcrResult.document_id == document_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def add(self, result: OcrResult) -> OcrResult:
        # document_id is unique -- a re-OCR (e.g. retrying a document that failed
        # at a later stage) replaces the prior row rather than colliding with it.
        self._session.execute(delete(OcrResult).where(OcrResult.document_id == result.document_id))
        self._session.add(result)
        self._session.flush()
        return result
