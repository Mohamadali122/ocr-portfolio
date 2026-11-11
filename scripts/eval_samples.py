"""Phase 8 regression harness: run every file under `sample/` through the real
running pipeline (real PaddleOCR, real LM Studio) and report accuracy metrics.

Talks to the API over HTTP, like any other client -- not a shortcut through
`app.api.deps`'s wiring, so it exercises the same code path a real upload does
and needs nothing this repo doesn't already exist to serve. Requires the full
stack up (`docker compose up --build -d`) and an admin user
(`docker compose exec -e ADMIN_PASSWORD=... api python -m app.cli seed-admin
--email admin@example.org`) -- this script cannot start either for you.

Usage:
    EVAL_EMAIL=admin@example.org EVAL_PASSWORD=... python scripts/eval_samples.py
    python scripts/eval_samples.py --baseline eval_baseline.json  # diff against a prior run
    python scripts/eval_samples.py --out eval_baseline.json       # save this run for next time

`sample/` is gitignored (real PHI) -- this script reads it locally and prints
only aggregate counts and non-PHI metadata (status, doc_type, engine, field
counts, confidence). Never prints a field value or a filename.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import httpx

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample"
TERMINAL_STATUSES = {"completed", "failed", "extraction_failed"}
POLL_INTERVAL_SECONDS = 2
POLL_TIMEOUT_SECONDS = 180


def _login(client: httpx.Client, email: str, password: str) -> str:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    return response.json()["access_token"]  # noqa: S105  not a credential, a response key


def _upload(client: httpx.Client, path: Path) -> str:
    content_type = {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }.get(path.suffix.lower(), "application/octet-stream")
    with path.open("rb") as fh:
        response = client.post(
            "/api/v1/documents",
            files={"file": (path.name, fh, content_type)},
        )
    if response.status_code == 429:
        time.sleep(5)
        return _upload(client, path)
    response.raise_for_status()
    return response.json()["id"]


def _poll(client: httpx.Client, document_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/documents/{document_id}")
        response.raise_for_status()
        doc = response.json()
        if doc["status"] in TERMINAL_STATUSES:
            return doc
        time.sleep(POLL_INTERVAL_SECONDS)
    return {"status": "timeout", "id": document_id}


def _populated_field_count(fields: dict[str, Any]) -> int:
    count = 0
    for name, field in fields.items():
        if name == "insurance":
            continue
        if field.get("typed_value"):
            count += 1
    return count


def _evaluate_one(client: httpx.Client, path: Path) -> dict[str, Any]:
    document_id = _upload(client, path)
    doc = _poll(client, document_id)
    row: dict[str, Any] = {
        "path": str(path.relative_to(SAMPLE_DIR)),
        "status": doc["status"],
        "doc_type": doc.get("doc_type"),
    }

    ocr_response = client.get(f"/api/v1/documents/{document_id}/ocr")
    if ocr_response.status_code == 200:
        ocr = ocr_response.json()
        row["engine"] = ocr["engine_version"]
        row["ocr_confidence"] = round(ocr["mean_confidence"], 3)

    if doc["status"] == "completed":
        extraction_response = client.get(f"/api/v1/documents/{document_id}/extraction")
        extraction_response.raise_for_status()
        row["populated_fields"] = _populated_field_count(extraction_response.json()["fields"])
    elif doc.get("failure_reason"):
        row["failure_reason"] = doc["failure_reason"]

    return row


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = Counter(row["status"] for row in rows)
    engine_counts = Counter(row.get("engine", "n/a") for row in rows)
    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        by_type[row.get("doc_type") or "unknown"][row["status"]] += 1

    completed = [row for row in rows if row["status"] == "completed"]
    avg_populated = (
        sum(row["populated_fields"] for row in completed) / len(completed) if completed else 0.0
    )

    return {
        "total": len(rows),
        "status_counts": dict(status_counts),
        "success_rate": round(status_counts["completed"] / len(rows), 4) if rows else 0.0,
        "engine_counts": dict(engine_counts),
        "by_doc_type": {k: dict(v) for k, v in by_type.items()},
        "avg_populated_fields_on_completed": round(avg_populated, 2),
    }


def _print_report(summary: dict[str, Any], baseline: dict[str, Any] | None) -> None:
    print(f"\n{'=' * 60}\nsample/ regression report\n{'=' * 60}")
    print(f"total documents:        {summary['total']}")
    print(f"success rate:           {summary['success_rate']:.1%}")
    print(f"status breakdown:       {summary['status_counts']}")
    print(f"OCR engine breakdown:   {summary['engine_counts']}")
    print(f"avg populated fields:   {summary['avg_populated_fields_on_completed']}")
    print("by document type:")
    for doc_type, counts in summary["by_doc_type"].items():
        print(f"  {doc_type:20s} {dict(counts)}")

    if baseline:
        print(f"\n{'-' * 60}\nvs baseline\n{'-' * 60}")
        delta = summary["success_rate"] - baseline.get("success_rate", 0.0)
        sign = "+" if delta >= 0 else ""
        print(f"success rate:           {baseline.get('success_rate', 0.0):.1%} -> "
              f"{summary['success_rate']:.1%} ({sign}{delta:.1%})")
        delta_fields = summary["avg_populated_fields_on_completed"] - baseline.get(
            "avg_populated_fields_on_completed", 0.0
        )
        sign_f = "+" if delta_fields >= 0 else ""
        old_avg = baseline.get("avg_populated_fields_on_completed", 0.0)
        new_avg = summary["avg_populated_fields_on_completed"]
        print(f"avg populated fields:   {old_avg} -> {new_avg} ({sign_f}{delta_fields:.2f})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_base = os.environ.get("EVAL_API_BASE", "http://localhost:8000")
    parser.add_argument("--api-base", default=default_base)
    parser.add_argument("--baseline", type=Path, default=None, help="prior --out JSON to diff")
    parser.add_argument("--out", type=Path, default=None, help="write this run's summary+rows JSON")
    args = parser.parse_args()

    email = os.environ.get("EVAL_EMAIL")
    password = os.environ.get("EVAL_PASSWORD")
    if not email or not password:
        print("Set EVAL_EMAIL and EVAL_PASSWORD (an admin/prebiller account; see CLAUDE.md's "
              "seed-admin command).", file=sys.stderr)
        return 2

    if not SAMPLE_DIR.is_dir():
        print(f"No sample/ directory at {SAMPLE_DIR}.", file=sys.stderr)
        return 2
    paths = sorted(p for p in SAMPLE_DIR.rglob("*") if p.is_file() and p.name != "ANALYSIS.md")
    if not paths:
        print("sample/ has no files to evaluate.", file=sys.stderr)
        return 2

    with httpx.Client(base_url=args.api_base, timeout=30.0) as client:
        token = _login(client, email, password)
        client.headers["Authorization"] = f"Bearer {token}"

        rows: list[dict[str, Any]] = []
        for index, path in enumerate(paths, start=1):
            print(f"[{index}/{len(paths)}] processing...", file=sys.stderr)
            rows.append(_evaluate_one(client, path))

    summary = _summarize(rows)
    baseline = json.loads(args.baseline.read_text())["summary"] if args.baseline else None
    _print_report(summary, baseline)

    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
        print(f"\nWrote {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
