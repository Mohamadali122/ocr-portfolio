import uuid
from datetime import date

from app.models.document import Document
from app.services.patient_grouping_service import MismatchFlag, PatientGroupingService


def _doc(
    *,
    run_number: str | None = None,
    last_name: str | None = None,
    first_name: str | None = None,
    dob: date | None = None,
) -> Document:
    return Document(
        id=uuid.uuid4(),
        run_number=run_number,
        patient_last_name=last_name,
        patient_first_name=first_name,
        patient_dob=dob,
    )


class _StubDocumentRepository:
    """Returns a canned list, regardless of the match criteria passed in --
    `PatientGroupingService` is what's under test here, not the query itself
    (that's `tests/integration/test_documents_api.py`'s job)."""

    def __init__(self, related: list[Document]) -> None:
        self._related = related
        self.last_call: dict[str, object] | None = None

    def find_related(self, **kwargs: object) -> list[Document]:
        self.last_call = kwargs
        return self._related


class TestFindRelated:
    def test_passes_the_anchors_identity_to_the_repository(self) -> None:
        anchor = _doc(run_number="R1", last_name="Doe", first_name="Jane", dob=date(1990, 1, 1))
        repo = _StubDocumentRepository([])
        PatientGroupingService(repo).find_related(anchor)  # type: ignore[arg-type]

        assert repo.last_call == {
            "run_number": "R1",
            "last_name": "Doe",
            "first_name": "Jane",
            "dob": date(1990, 1, 1),
            "exclude_id": anchor.id,
        }

    def test_no_related_documents_is_not_an_error(self) -> None:
        anchor = _doc(run_number="R1")
        result = PatientGroupingService(_StubDocumentRepository([])).find_related(  # type: ignore[arg-type]
            anchor
        )
        assert result.related == ()
        assert result.mismatches == ()

    def test_all_fields_null_does_not_crash(self) -> None:
        anchor = _doc()
        result = PatientGroupingService(_StubDocumentRepository([])).find_related(  # type: ignore[arg-type]
            anchor
        )
        assert result.related == ()
        assert result.mismatches == ()

    def test_matching_run_number_with_a_disagreeing_dob_is_flagged(self) -> None:
        # The sample/insurance_eligibility.pdf scenario: same encounter, an
        # eligibility response that disagrees with the rest on identity.
        anchor = _doc(run_number="R1", last_name="Doe", dob=date(1990, 1, 1))
        other = _doc(run_number="R1", last_name="Doe", dob=date(1985, 6, 15))

        result = PatientGroupingService(_StubDocumentRepository([other])).find_related(  # type: ignore[arg-type]
            anchor
        )

        assert result.related == (other,)
        assert result.mismatches == (
            MismatchFlag(field="patient_dob", values=("1985-06-15", "1990-01-01")),
        )

    def test_agreeing_documents_have_no_mismatches(self) -> None:
        anchor = _doc(run_number="R1", last_name="Doe", first_name="Jane", dob=date(1990, 1, 1))
        other = _doc(run_number="R1", last_name="Doe", first_name="Jane", dob=date(1990, 1, 1))

        result = PatientGroupingService(_StubDocumentRepository([other])).find_related(  # type: ignore[arg-type]
            anchor
        )
        assert result.mismatches == ()

    def test_one_document_missing_a_field_is_not_a_mismatch(self) -> None:
        # Absence isn't disagreement -- only two actual values differing is.
        anchor = _doc(run_number="R1", last_name="Doe")
        other = _doc(run_number="R1", last_name=None)

        result = PatientGroupingService(_StubDocumentRepository([other])).find_related(  # type: ignore[arg-type]
            anchor
        )
        assert result.mismatches == ()
