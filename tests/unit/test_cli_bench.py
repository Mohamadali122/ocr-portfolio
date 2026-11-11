"""`bench_tally` -- the pure half of `python -m app.cli bench`."""

from app.cli import bench_tally


def _field(value: str | None, confidence: float = 0.0) -> dict:
    return {"value": value, "confidence": confidence}


class TestBenchTally:
    def test_tiers_are_counted_per_doc_type_and_field(self) -> None:
        rows = [
            ("hfs", {"last_name": _field("DOE", 1.0), "phone": _field(None)}),
            ("hfs", {"last_name": _field("doe", 0.7), "phone": _field(None)}),
            ("epcr", {"last_name": _field("DOE", 1.0)}),
        ]
        assert bench_tally(rows) == [
            ("epcr", "last_name", 1, 1, 0, 0),
            ("hfs", "last_name", 2, 1, 1, 0),
            ("hfs", "phone", 2, 0, 0, 2),
        ]

    def test_insurance_slots_aggregate_by_subfield(self) -> None:
        rows = [
            (
                "hfs",
                {
                    "insurance": [
                        {"policy_id": _field("A1", 1.0)},
                        {"policy_id": _field(None)},
                    ]
                },
            )
        ]
        assert bench_tally(rows) == [("hfs", "insurance.policy_id", 2, 1, 0, 1)]

    def test_no_rows_is_empty(self) -> None:
        assert bench_tally([]) == []
