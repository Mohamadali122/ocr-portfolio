from app.models.document import DocumentType
from app.services.classification import classify


class TestClassify:
    def test_epcr_marker(self) -> None:
        text = "Company City Ambulance Service PATIENT CARE REPORT Incident# 12345-26"
        assert classify(text) is DocumentType.EPCR

    def test_booking_summary_marker(self) -> None:
        text = "BookingSummary https://gmr.transport.net/bookingsummary"
        assert classify(text) is DocumentType.BOOKING_SUMMARY

    def test_pcs_marker(self) -> None:
        text = "PHYSICIAN CERTIFICATION STATEMENT for medical necessity"
        assert classify(text) is DocumentType.PCS

    def test_hfs_marker(self) -> None:
        text = "Hospital Face Sheet  Guarantor Information  Next of Kin"
        assert classify(text) is DocumentType.HFS

    def test_no_marker_is_unknown(self) -> None:
        text = "some unrelated scanned text with no recognizable header"
        assert classify(text) is DocumentType.UNKNOWN

    def test_empty_text_is_unknown(self) -> None:
        assert classify("") is DocumentType.UNKNOWN

    def test_epcr_takes_priority_over_a_face_sheet_marker_in_the_same_text(self) -> None:
        # extraction_service's own doc-type hints note a face sheet and an
        # EPCR are routinely scanned into one document; the report title is
        # the more distinctive signal and must win.
        text = "PATIENT CARE REPORT ... attached Guarantor information follows"
        assert classify(text) is DocumentType.EPCR

    def test_case_insensitive(self) -> None:
        assert classify("patient care report") is DocumentType.EPCR
