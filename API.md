# API Reference

Base path: `/api/v1` (health probes are unprefixed). Interactive schema at `/docs`
only when `ENVIRONMENT=local` — disabled elsewhere, an interactive schema
browser is needless attack surface on a PHI system.

All endpoints except `/health*` and `/api/v1/auth/login` and `/refresh` require
`Authorization: Bearer <access_token>`.

## Error shape

Every error response, regardless of cause, is:

```json
{ "error": "not_found", "message": "The requested resource does not exist.", "request_id": "..." }
```

Never an exception string, traceback, or database value — see `app/core/exceptions.py`.
Validation errors (422) add a `fields` array of dotted field locations that failed;
the offending values themselves are never echoed back.

| code | status | meaning |
|---|---|---|
| `not_found` | 404 | resource does not exist |
| `validation_error` | 422 | request payload invalid |
| `authentication_failed` | 401 | bad credentials / missing / expired token (`WWW-Authenticate: Bearer` header set) |
| `forbidden` | 403 | authenticated but not permitted |
| `document_unreadable` | 500 | stored ciphertext failed to decrypt or file missing — an integrity fault, not a missing resource |
| `internal_error` | 500 | unhandled exception |

## Health

### `GET /health`
Liveness. No auth, touches nothing.

```json
{ "status": "ok", "service": "local-doc-ocr", "version": "0.1.0", "environment": "local" }
```

### `GET /health/ready`
Readiness. Probes DB with `SELECT 1`. Returns 503 + `"degraded"` if the database
is unreachable.

```json
{ "status": "ready", "database": "ok" }
```

## Auth

### `POST /api/v1/auth/login`
Rate limited per client IP (`rate_limit_login_per_minute`, default 10/min).

Request:
```json
{ "email": "user@example.org", "password": "..." }
```

Response `200`:
```json
{ "access_token": "...", "refresh_token": "...", "token_type": "bearer", "expires_in": 900 }
```

Every failure mode (unknown email, wrong password, inactive account) returns
the **same** generic 401 — distinguishing them is an account-enumeration oracle.

### `POST /api/v1/auth/refresh`
Request: `{ "refresh_token": "..." }` → same `TokenPair` shape as login.
Old refresh token stays valid until it expires (rotation, not invalidation).
Revoke all of a user's tokens by bumping `users.token_version` — no denylist table.

### `GET /api/v1/auth/me`
Requires bearer token.

```json
{ "id": "uuid", "email": "user@example.org", "role": "prebiller", "is_active": true,
  "created_at": "...", "last_login_at": "..." }
```

## Documents

### `POST /api/v1/documents`
`multipart/form-data`: `file` (required), `doc_type` (`hfs` | `epcr` | `unknown`,
default `unknown`). Max 25 MB (`max_upload_bytes`), enforced while streaming.
Rate limited per client IP (`rate_limit_upload_per_minute`, default 30/min).

Returns **202** immediately — bytes are stored, OCR/extraction pipeline has not
run yet:

```json
{ "id": "uuid", "status": "pending", "deduplicated": false }
```

Re-uploading identical bytes (same SHA-256) returns the existing document's id
with `"deduplicated": true` and does not re-run the pipeline.

Poll `GET /api/v1/documents/{id}` until `status` leaves `pending` / `ocr_running`
/ `ai_running`.

### `GET /api/v1/documents`
Search by denormalized identity columns. Query params (all optional):

| param | type | notes |
|---|---|---|
| `patient_name` | string, ≤100 | |
| `run_number` | string, ≤32 | |
| `dob_from` / `dob_to` | date | |
| `limit` | int, 1–100, default 50 | |
| `cursor_created_at` / `cursor_id` | keyset page cursor from previous response | |

```json
{ "items": [ { "id": "uuid", "doc_type": "hfs", "status": "completed",
    "patient_last_name": "...", "patient_first_name": "...", "patient_dob": "...",
    "run_number": "...", "created_at": "...", "ocr_completed_at": "...",
    "ai_completed_at": "..." } ],
  "next_cursor": { "created_at": "...", "id": "uuid" } }
```

`next_cursor` is `null` on the last page. Audited as `document_viewed`
(`detail.count` only — search terms are never logged, they're PHI in the query string).

### `GET /api/v1/documents/{id}`
Full document metadata, decrypted filename included. Audited.

```json
{ "id": "uuid", "doc_type": "hfs", "status": "completed",
  "original_filename": "...", "content_type": "application/pdf",
  "size_bytes": 123456, "page_count": 2, "sha256": "...",
  "failure_reason": null, "created_at": "...",
  "ocr_completed_at": "...", "ai_completed_at": "..." }
```

### `GET /api/v1/documents/{id}/ocr`
Raw OCR text + per-line geometry. **404** if OCR hasn't finished yet (never an
empty result — that would read as "nothing on the page"). Audited.

```json
{ "document_id": "uuid", "raw_text": "...",
  "page_texts": [ { "page": 1, "lines": [ { "text": "...", "confidence": 0.99,
    "box": [[x,y],[x,y],[x,y],[x,y]] } ] } ],
  "mean_confidence": 0.9912, "engine_version": "...", "page_count": 2,
  "duration_ms": 23400, "created_at": "..." }
```

### `GET /api/v1/documents/{id}/extraction`
Grounded field extraction (64 flat fields plus a 0-3-slot `insurance` list — see
`app/schemas/extraction.py` and `PHASE5_REDESIGN.md`).
**404** until the LLM stage completes. Audited.

```json
{ "document_id": "uuid", "llm_model": "...", "repaired": false, "created_at": "...",
  "fields": {
    "last_name": { "value": "SMITH", "confidence": 0.98, "source_page": 1,
      "source_bbox": [[...]], "typed_value": "SMITH", "source": "grounded" },
    "dob": { "value": null, "confidence": 0.0, "source_page": null,
      "source_bbox": null, "typed_value": "01/01/1900", "source": "sop_default" },
    "...": "24 more fields, same shape"
  } }
```

`source` tells you why `typed_value` has its value: `grounded` (verbatim from
the page), `reformatted` (deterministic transform, e.g. date normalization),
`sop_default` (rule-supplied because the field was absent), `skipped`
(present on the page but SOP says don't enter it), `absent` (nothing found,
no default applies).

### `GET /api/v1/documents/{id}/page/{page}`
Renders one page to PNG at the pipeline's OCR DPI, for the review UI's bbox
overlay. `page` is 1-indexed. `Cache-Control: no-store`.

### `GET /api/v1/documents/{id}/file`
Original file bytes, decrypted. `Content-Disposition: attachment` (never
inline — a PDF must never render in the browser's origin).

## Not implemented

No billing/claims endpoints exist. This API stops at grounded field
extraction for human review; see `the project proposal` for what an export-to-
the billing system bridge or a full claims (837P) API would add.
