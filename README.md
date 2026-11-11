# local-doc-ocr

HIPAA-ready local OCR and AI document processing for ambulance dispatch prebilling.

Extracts structured fields from claim attachments (face sheets, patient care reports) so a
prebiller stops reading PDFs by eye. **It does not replace the billing system and does not make billing
decisions.** The prebiller remains the decision-maker.

Everything runs on the client's own hardware. No document, no OCR text, and no extracted field
ever leaves the network. There are no external AI or OCR APIs.

> Portfolio copy. Client documents, sample files and client-specific references are removed;
> every name, date of birth and identifier in the tests is synthetic.

---

## Status

**Phase 7 of 7 — Hardening.** Rate limiting, the database-enforced append-only audit log, and the
HIPAA technical-safeguards checklist are done. TLS / reverse-proxy deployment is the one Phase 7
item still open — see [HIPAA_CHECKLIST.md](HIPAA_CHECKLIST.md). Alembic head `b6212144c3f8`.

This README is the deep verification reference, phase by phase.

| Phase | Scope | State |
|---|---|---|
| 1 | Docker, config, PHI-safe logging, Alembic, `/health` | ✅ |
| 2 | Auth: users, argon2, JWT, seeded admin, audit table | ✅ |
| 3 | Documents: upload, encrypted storage, validation | ✅ |
| 4 | OCR: PaddleOCR behind a protocol, background pipeline | ✅ |
| 5 | LLM: field extraction to grounded/validated JSON | ✅ |
| 6 | Search + minimal review UI | ✅ |
| 7 | Hardening: audit immutability, rate limits, HIPAA checklist | mostly ✅, TLS open |

**Before starting the app after this change:** `.env` needs `POSTGRES_APP_USER` /
`POSTGRES_APP_PASSWORD` (see `.env.example`), the image needs a rebuild (`pyproject.toml` gained
`slowapi`), and `alembic upgrade head` must run before the app connects — the new migration
creates the restricted role the app now connects as. See [HIPAA_CHECKLIST.md](HIPAA_CHECKLIST.md)
for what closed and what is still open.
Real documents belong on the deployment machine only, never in this repo or any notes system.

---

## Pilot deployment target

This laptop: Windows 11, 13th-gen Intel HX, **RTX 5050 (8 GB)**. Not the Ubuntu server with a
16 GB RTX 4060 Ti in the proposal. Three consequences, all landing in Phases 4–5:

- **PaddleOCR runs on CPU.** Blackwell is `sm_120` and needs CUDA 12.8+; the `paddlepaddle-gpu`
  2.6.x wheels ship no kernels for it. GPU Paddle in a Linux container on Windows would also
  mean Docker Desktop → WSL2 → CUDA passthrough. Not worth it for a pilot; the HX has the cores.
- **The LLM gets the GPU, natively.** LM Studio on Windows talks to CUDA directly. The container
  reaches it at `host.docker.internal`, never `localhost`. This is why `LlmProvider` is an
  OpenAI-compatible HTTP adapter rather than an in-process model.
- **8 GB VRAM caps the model at the 8–9B class.** A 14B at Q4_K_M needs ~9 GB and will spill to
  CPU. The production box still wants the 16 GB card.

---

## Requirements

- Docker Desktop (Windows/macOS) or Docker Engine + Compose v2 (Ubuntu)
- Python 3.12, only if you want to run the test suite outside a container

---

## First run

```powershell
cd local-doc-ocr

# 1. Create your environment file. It is gitignored; never commit it.
Copy-Item .env.example .env

# 2. Edit .env. Set POSTGRES_PASSWORD, and replace the JWT_SECRET_KEY placeholder:
#      python -c "import secrets; print(secrets.token_urlsafe(48))"
#    Startup refuses a secret shorter than 32 characters, so this is not optional.
notepad .env

# 3. Build and start. First build pulls ~400 MB and takes a few minutes.
docker compose up --build -d

# 4. Watch it come up.
docker compose ps
docker compose logs -f api

# 5. Create the schema. Nothing authenticates until this runs.
docker compose exec api alembic upgrade head
```

Expect `api` and `db` both `healthy`. Then:

```powershell
# Liveness. Should return 200.
curl.exe -i http://localhost:8000/health

# Readiness. Probes Postgres with SELECT 1. Should return 200 and database: ok.
curl.exe -i http://localhost:8000/health/ready

# Interactive schema, local environment only.
start http://localhost:8000/docs
```

Shut down with `docker compose down`. Add `-v` to also destroy the database and document
volumes.

---

## Verifying Phase 1

Phase 1 exists to prove four things. Check each one.

### 1. The container builds with PaddleOCR's system dependencies present

The wheels come in Phase 4, but the native libraries they link against are installed now,
because getting them wrong is the classic multi-day rabbit hole.

```powershell
docker compose exec api python -c "import ctypes; ctypes.CDLL('libGL.so.1'); print('libGL ok')"
docker compose exec api pdftoppm -v
```

### 2. The API answers, and readiness reflects the database

```powershell
curl.exe -s http://localhost:8000/health/ready   # {"status":"ready","database":"ok"}

docker compose stop db
curl.exe -i http://localhost:8000/health         # still 200 -- liveness must not touch the DB
curl.exe -i http://localhost:8000/health/ready   # 503, {"status":"degraded",...}
docker compose start db
```

That split matters: if liveness probed the database, a database restart would restart-loop the
API instead of merely marking it unready.

### 3. PHI never reaches a log line

This is the HIPAA control that Phase 1 exists to install, before any PHI can flow.

```powershell
docker compose exec api python -m pytest tests/unit/test_phi_logging.py -v   # 18 passed
```

Eighteen tests across two independent layers:

- **Key-based** — a value under a known-PHI key (`raw_text`, `narrative`, `patient_name`, `ssn`,
  …) is dropped wholesale, whatever it looks like. This is what catches free text.
- **Pattern-based** — every remaining string is scanned for SSNs, phone numbers, emails,
  `MM/DD/YYYY` / `YYYY/MM/DD` dates, and bare 9-digit runs (undashed SSNs, and the
  Medicaid-of-Texas policy ID format). This is what catches PHI arriving under an innocent key,
  which in practice means inside an exception message.

Prove it by hand:

```powershell
docker compose exec api python -c @'
from app.core.logging import configure_logging, get_logger
from app.core.config import get_settings
configure_logging(get_settings())
get_logger("demo").info("upload", ssn="123-45-6789", raw_text="Pt c/o chest pain", note="call (713) 555-0142")
'@
```

You should see `"ssn": "<redacted>"`, `"raw_text": "<redacted>"`, and `"note": "call <phone>"`.

One deliberate gap, asserted by
`test_a_surname_under_a_neutral_key_is_not_caught`: a bare surname under a neutral key survives.
No regex distinguishes a patient's surname from a facility's or a developer's — only the key
does. **When you add a field that can hold PHI, add its key to `SENSITIVE_KEYS` in
[app/core/logging.py](app/core/logging.py).** That is the maintenance obligation this design
buys in exchange for a chokepoint.

### 4. Uvicorn's access log is off

Uvicorn logs the raw request line, query string included. Phase 6 ships
`GET /api/v1/documents?patient_name=Smith`. That request line is a patient name on disk, and no
care at the call site prevents it.

```powershell
curl.exe -s "http://localhost:8000/health?patient_name=Testerson" | Out-Null
docker compose logs api --tail 5 | Select-String "Testerson"   # must print nothing
```

`RequestContextMiddleware` logs `request.url.path`, which excludes the query string.

---

## Authentication (Phase 2)

### Seed the administrator

The password is read from the terminal, or from `ADMIN_PASSWORD` when stdin is not a TTY. It is
never accepted as a command-line argument — `argv` lands in shell history, `ps` output, and
`docker inspect`.

```powershell
# Interactive: prompts twice, echoes nothing
docker compose exec api python -m app.cli seed-admin --email admin@yourdomain.com

# Non-interactive
docker compose exec -e ADMIN_PASSWORD='your-long-password' api `
    python -m app.cli seed-admin --email admin@yourdomain.com
```

Idempotent — a second run reports the account exists and changes nothing. Passwords shorter than
`MIN_PASSWORD_LENGTH` (12) are refused.

`seed-admin` validates the address with the same `EmailStr` that `/auth/login` uses. It did not
always: an earlier version happily created `admin@ocr.local`, which the login endpoint then
rejected forever as a special-use domain. `email-validator` refuses `.local`, `.test`,
`.invalid`, and bare `localhost`. It accepts `example.com` and `.example`.

### Use it

```powershell
$body = '{"email":"admin@yourdomain.com","password":"your-long-password"}'
$r = curl.exe -s -X POST http://localhost:8000/api/v1/auth/login `
        -H 'Content-Type: application/json' -d $body | ConvertFrom-Json

curl.exe -s http://localhost:8000/api/v1/auth/me -H "Authorization: Bearer $($r.access_token)"

# Exchange a refresh token for a new pair
curl.exe -s -X POST http://localhost:8000/api/v1/auth/refresh `
    -H 'Content-Type: application/json' -d "{`"refresh_token`":`"$($r.refresh_token)`"}"
```

### Verifying the security properties

**Failed logins are indistinguishable.** Wrong password and unknown account return the same
status, the same `error`, and the same `message`. They also take the same time — the unknown-user
path verifies against a dummy Argon2 hash rather than returning early. Measured: 0.36s vs 0.34s.
Without it, the unknown-user path returns in ~5 ms and enumerates every valid account.

```powershell
curl.exe -s -X POST http://localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' `
    -d '{"email":"admin@yourdomain.com","password":"wrong"}'
curl.exe -s -X POST http://localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' `
    -d '{"email":"nobody@yourdomain.com","password":"wrong"}'
```

**Failed logins are still audited.** A failed login raises, `get_db` rolls back — and a naive
implementation loses the audit row for the one event an auditor most needs. `AuditService` writes
in its own transaction for exactly this reason.

```powershell
docker compose exec db psql -U ocr -d doc_ocr `
    -c "SELECT id, user_id, action, ip_address, detail FROM audit_logs ORDER BY id DESC LIMIT 5;"
```

`detail` carries a reason (`bad_password`, `unknown_email`), never the submitted email or
password. `ip_address` is `INET`, and an unparseable peer address is stored as `NULL` rather than
aborting the insert.

**Revocation.** There is no token denylist. Incrementing `users.token_version` invalidates every
access and refresh token that user holds, on the next request.

```powershell
docker compose exec db psql -U ocr -d doc_ocr `
    -c "UPDATE users SET token_version = token_version + 1 WHERE email = 'admin@yourdomain.com';"
```

**Passwords.** Argon2id, `m=65536,t=3,p=4`. Verify the database never saw plaintext:

```powershell
docker compose exec db psql -U ocr -d doc_ocr -tAc "SELECT left(password_hash,32) FROM users;"
# $argon2id$v=19$m=65536,t=3,p=4$...
```

---

## Documents (Phase 3)

```powershell
$h = @{ Authorization = "Bearer $($r.access_token)" }

# Upload. doc_type is hfs | epcr | unknown.
curl.exe -s -X POST http://localhost:8000/api/v1/documents -H "Authorization: Bearer $($r.access_token)" `
    -F "file=@C:\path\to\facesheet.pdf;type=application/pdf" -F "doc_type=hfs"

# Metadata (decrypts the filename), then the file itself
curl.exe -s http://localhost:8000/api/v1/documents/<id> -H "Authorization: Bearer $($r.access_token)"
curl.exe -s http://localhost:8000/api/v1/documents/<id>/file -H "Authorization: Bearer $($r.access_token)" -o out.pdf
```

Status stays `pending` — nothing processes it until Phase 4.

### Verifying encryption at rest

Upload a file whose name contains a surname, then look at what actually landed.

```powershell
# The filename column is ciphertext. This must return 0.
docker compose exec db psql -U ocr -d doc_ocr `
    -tAc "SELECT count(*) FROM documents WHERE original_filename_enc::text LIKE '%TESTERSON%';"

# The file on disk does not begin with %PDF.
$k = docker compose exec db psql -U ocr -d doc_ocr -tAc "SELECT storage_key FROM documents LIMIT 1;"
docker compose exec api sh -c "head -c 5 /srv/data/documents/${k:0:2}/${k:2:2}/$k"
```

Verified end to end: a 3-page PDF named `TESTERSON_JANE_facesheet.pdf` uploads, stores as
ciphertext, downloads byte-identical, and its sha256 matches the column.

### The security properties, and how to break them

**Type is decided by magic bytes.** `Content-Type` and the file extension are both
attacker-controlled; only the bytes are not. A zip renamed to `.pdf` and declared as
`application/pdf` returns 415. Everything downstream — PaddleOCR, pypdfium — is a native-code
parser being handed hostile input.

**Caps are security controls.** 25 MB and 50 pages. A 5,000-page PDF is a denial-of-service
against a CPU-only OCR host. The size cap is enforced *while streaming*, because reading the body
first and checking afterwards is the attack, not the defence.

**Tampering is detected, not tolerated.** AES-256-GCM, with the storage key as associated data.
Flip one bit of a stored file and the download returns `document_unreadable`, never a partial
read. Move a file to another key and it fails to authenticate — an attacker cannot swap two
patients' documents.

```powershell
# Prove it. Flip a bit, download, flip it back.
docker compose exec api python -c "p='/srv/data/documents/xx/yy/<key>'; b=bytearray(open(p,'rb').read()); b[-1]^=1; open(p,'wb').write(bytes(b))"
```

A failed decrypt is **not** audited as a download. The trail never claims PHI was disclosed when
it was not.

**Storage keys are UUID hex, never filenames.** There is no `..` to sanitise because there is no
path built from user input. Anything other than 32 lowercase hex characters is refused before
disk is touched.

**`sha256` is unique.** Re-uploading identical bytes returns the existing document id with
`"deduplicated": true` rather than encrypting a second copy under a second key.

### What the encryption does and does not buy you

It protects a leaked database dump, a copied backup, a stolen disk image, and an operator with
filesystem read access but no application key.

It does **not** protect against device theft. On this laptop `STORAGE_ENCRYPTION_KEY` lives in
`.env`, on the same disk as the ciphertext. Anyone who takes the machine takes both. **BitLocker
is the control that covers that**, and no amount of "AES-256" changes it. For a real deployment
the key must be injected by the operator — a Docker secret, a systemd credential, an HSM — and
never written next to the data it protects.

Rotating `STORAGE_ENCRYPTION_KEY` makes every stored document permanently unreadable. There is no
key-versioning scheme in the pilot. Back the key up separately from the ciphertext, or you have
backed up neither.

---

## Review workflow additions (Phase 6+)

Four capabilities layered onto the Phase 6 review UI: correcting a field, reprocessing a document,
browsing the audit trail, and a confidence-ranked review queue. All are exercised from
`http://localhost:8000/ui/`; the API calls below are for scripting or verification.

### Correcting an extracted field

```powershell
curl.exe -s -X PATCH "http://localhost:8000/api/v1/documents/<id>/extraction/fields/last_name" `
    -H "Authorization: Bearer $($r.access_token)" -H 'Content-Type: application/json' `
    -d '{"typed_value":"SMITH"}'
```

Sets `typed_value` and stamps `source: "corrected"` — a human edit is never mistaken for a page
reading. There is no per-field id; the URL segment is the field name, one of `PatientExtraction`'s
keys. Audited as `extraction_field_corrected` with the field *name* only in `detail` — the old and
new values are PHI and never reach the audit row.

```powershell
docker compose exec db psql -U ocr -d doc_ocr `
    -c "SELECT action, detail FROM audit_logs WHERE action = 'extraction_field_corrected' ORDER BY id DESC LIMIT 1;"
```

### Reprocessing a document

```powershell
curl.exe -s -X POST "http://localhost:8000/api/v1/documents/<id>/reprocess" `
    -H "Authorization: Bearer $($r.access_token)"
```

Re-arms a `failed` or `completed` document to `pending` and reschedules the same
`PipelineService.process_document` BackgroundTask the upload path uses — `202` with
`{id, status: "pending"}`. `409` if the document is already mid-pipeline (`pending`/`*_running`),
so two reprocess requests can't both think they own the run. Audited as `document_reprocessed`.

### The audit trail

```powershell
curl.exe -s "http://localhost:8000/api/v1/audit-logs?limit=5" -H "Authorization: Bearer $($r.access_token)"
```

Role-gated (`require_role(ADMIN, AUDITOR)`) — the pilot's seeded admin passes, but this is the
first route in the app that isn't "any authenticated user." Keyset-paginated on `(occurred_at, id)`,
same row-value-comparison pattern as document search — see the `AuditLogCursor` docstring in
`app/schemas/audit_log.py` for why `id` alone isn't a safe substitute for time order here (the
short version: `occurred_at`'s `server_default=func.now()` is transaction-start time, not
statement-execution time, and `AuditService.record_atomic` joins an already-open transaction).
Reading the trail is itself audited (`audit_log_viewed`) — the compliance log the auditor reads is
also a log of who read it.

### Confidence-ranked review queue

`GET /documents` now joins `OcrResult.mean_confidence` (LEFT JOIN, so a document still mid-pipeline
has `mean_confidence: null` rather than being dropped) into each search hit. The review UI's queue
mode sorts the current page of completed hits lowest-confidence-first before stepping through them
— a document more likely to need a correction surfaces first. This is a client-side sort over
whatever page of search results is already loaded, not a server-side ordering across the whole
result set.

---

## Running the tests

217 tests (unit + integration).

Inside the container, which is what CI will do:

```powershell
docker compose exec api python -m pytest -v
docker compose exec api python -m pytest --cov=app --cov-report=term-missing

docker compose exec api python -m pytest tests/unit -v          # no database needed
docker compose exec api python -m pytest tests/integration -v   # needs Postgres
```

Or on the host:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest -v
```

The unit suite needs no database. `tests/conftest.py` seeds dummy Postgres credentials because
`app/db/session.py` builds the engine at import time; nothing in the unit suite connects.

The integration suite does need one. It creates and deletes real rows against the live database,
and it skips itself with a stated reason when Postgres is unreachable — so a bare-host `pytest`
run stays green rather than red for the wrong reason. Deletion order is audit rows first: the
`ON DELETE RESTRICT` on `audit_logs.user_id` is the schema working as intended, refusing to let a
user with history be deleted out from under it.

Lint and typecheck:

```powershell
docker compose exec api ruff check app tests
docker compose exec api ruff format --check app tests
docker compose exec api mypy app
```

---

## Migrations

Alembic is wired to `Base.metadata` and to the settings object, so the database URL — which
carries a password — is never written into the committed `alembic.ini`.

Four migrations exist: `users` + `audit_logs`, then `documents`, then `ocr_results`, then
`extractions` (head `37193a2a6ea8`).

```powershell
docker compose exec api alembic upgrade head          # apply
docker compose exec api alembic revision --autogenerate -m "add something"
docker compose exec api alembic downgrade -1          # roll back one
docker compose exec api alembic current               # what is applied -> 37193a2a6ea8 (head)
```

Every new ORM class must be imported in `app/models/__init__.py`. Autogenerate diffs
`Base.metadata` against the live database; a model that is never imported is never registered,
and its table is silently omitted from the migration it belongs in.

Always read a generated migration before applying it. Autogenerate does not detect column
renames, server-default changes, or `CHECK` constraints.

---

## Architecture

Three protocols and one dispatcher are the whole design. Every deferred feature — Celery, RBAC,
additional document types, the tagging rules engine, the billing system integration — plugs into one of
these four seams.

```
app/
├── core/            config, logging (PHI chokepoint), security, crypto, exceptions, middleware
├── db/              engine, session factory, declarative base
├── models/          SQLAlchemy ORM: user, audit_log, document
├── schemas/         Pydantic I/O: auth, document      (extraction schemas: Phase 5)
├── repositories/    the only place a Session is touched
├── services/        use-cases; depend on protocols, never on adapters
│   ├── document_service.py     upload, dedup, encrypted read-back
│   ├── file_validation.py      magic bytes, size and page caps
│   └── pipeline_service.py     orchestrates OCR -> LLM; the future Celery seam  (Phase 4)
├── storage/         base.py: StorageBackend  | encrypted_local.py  (AES-256-GCM)
├── ocr/             base.py: OcrEngine       | paddle_engine.py                 (Phase 4)
├── llm/             base.py: LlmProvider     | openai_compat.py  (LM Studio)    (Phase 5)
├── api/             deps.py wires adapters into services; v1/ holds routers
└── cli.py           operator commands; `seed-admin` today
```

**The one rule:** `services/` may import `ocr/base.py`, `llm/base.py`, and `storage/base.py` —
never the adapters beside them. Adapters are injected in `api/deps.py`. Break this and the
excluded features stop being cheap to add.

`document_service.py` observes it — it imports `storage.base` and nothing else from `storage/`.
Nothing enforces it yet: the promised `import-linter` contract was not added in Phase 3 and is
still unwritten. Until it exists, the rule is upheld by review.

### Design decisions worth knowing

**Sync SQLAlchemy, not async.** PaddleOCR is synchronous and CPU-bound; async buys nothing here
and costs a session-management idiom. FastAPI runs `def` endpoints in a threadpool.

**Enums are VARCHAR + CHECK, not Postgres ENUMs.** Growing a Postgres ENUM needs
`ALTER TYPE ... ADD VALUE`, which cannot run inside a transaction and therefore cannot run inside
a normal Alembic migration. `AuditAction` gains members in every phase. `create_constraint=True`
is not the default, and without it the column is a bare `VARCHAR(32)` that happily stores
`'wizard'` — the enum would exist only in Python, which is no help to `psql`, to a restore, or to
the next service.

**No `roles` table.** `UserRole` is an enum column on `users`. The pilot seeds one administrator;
adding a prebiller is an INSERT, not a migration. `require_role()` sits in `api/deps.py` and is no
longer purely theoretical — `GET /audit-logs` is the first route to use it
(`require_role(UserRole.ADMIN, UserRole.AUDITOR)`), so a valid bearer token can now get a `403`
there even though every document route still accepts any authenticated user.

**No `patients` table.** The proposal's ER diagram has one. Patient identity resolution is the
prebilling SOP's "merge patient" step — a genuine dedup problem with six competing match keys.
Creating the table now forces a premature answer to *"when are two scans the same person?"*, and
every wrong answer becomes a data migration. Search is served by denormalized, indexed
`patient_last_name` / `patient_dob` / `run_number` columns on `documents`. The patient entity
arrives with the reconciliation phase, when the rules that define identity are actually written.

**Error bodies carry a code, our own message, and a request id.** Never an exception string,
never a traceback, never a database value. FastAPI's default validation handler echoes the
offending input back to the client; on a PHI-bearing upload endpoint that is a disclosure, so it
is replaced in `app/core/exceptions.py`.

---

## Security posture, stated honestly

Implemented:

- Postgres is not published to the host; only the compose network reaches it.
- The API binds `127.0.0.1:8000`. TLS termination is a reverse proxy's job — exposing `8000` on
  `0.0.0.0` would serve PHI over plaintext HTTP to the LAN.
- The container runs as a non-root user (uid 10001).
- `/docs` and `/openapi.json` are served only when `ENVIRONMENT=local`.
- `DEBUG` and `DB_ECHO` are rejected at startup when `ENVIRONMENT=production` (`DB_ECHO` logs
  bound parameters — that is PHI). A `JWT_SECRET_KEY` under 32 characters is rejected everywhere,
  and the `.env.example` placeholder is rejected outside `local`.
- Secrets come from `.env`, which is gitignored and excluded from the Docker build context.
- Passwords are Argon2id, rehashed transparently on login when the parameters have been raised.
- JWTs are verified against an algorithm whitelist of one, so the `alg: none` confusion attack
  fails. A refresh token presented as a bearer credential is rejected on its `typ` claim, rather
  than granting seven days of access.
- Every log record passes the PHI redaction chokepoint, uvicorn's query-string access log
  included. Verified end-to-end over the real OCR→LLM pipeline, not just synthetic log calls —
  `tests/integration/test_pipeline.py::TestPhiNeverReachesLogs`.
- Login, login failure, and token refresh are audited, durably, outside the request transaction.
- Document bodies and filenames are AES-256-GCM ciphertext at rest, with the storage key bound as
  associated data. File type is decided by magic bytes, never by `Content-Type` or extension, and
  the size cap is enforced while streaming rather than after the body is in memory.
- `/auth/login` and `POST /documents` are rate limited per client IP (`app/core/rate_limit.py`) —
  in-memory, correct for the pilot's single uvicorn worker.
- The audit log is append-only, database-enforced: the app's runtime role has no `UPDATE` or
  `DELETE` on `audit_logs` (the `add_app_role` migration), not just an `AuditRepository` that
  happens not to expose those methods.
- Error bodies are audited to never echo the offending input, a database value, or a raw exception
  string, across authentication, validation, not-found, and unhandled-exception responses —
  `tests/integration/test_error_body_scrub.py`.

Not yet true, and it matters:

- **No TLS termination.** The API binds `127.0.0.1:8000` — nothing reaches it except what is
  already on this host — but no reverse proxy is deployed and no traffic is encrypted in transit.
  `client_ip()` (`app/api/deps.py`) already trusts `X-Forwarded-For` from a loopback peer, so a
  same-host proxy becomes usable without further code changes; standing one up is still open. See
  [HIPAA_CHECKLIST.md](HIPAA_CHECKLIST.md) § 164.312(e)(2)(ii).
- **A key in an env file on the same disk as the ciphertext is not protection against disk
  theft.** `STORAGE_ENCRYPTION_KEY` lives in `.env`, beside the data it protects. The unchanged
  `.env.example` placeholder is now rejected outside `local`
  (`Settings._require_a_real_storage_key`), but the key's *location* did not move — full-disk
  encryption (BitLocker on this laptop) is the control that actually covers device theft. Say this
  out loud to the client rather than letting them believe the word "AES-256" settles the question.

Software cannot make an organization HIPAA compliant. This system implements technical
safeguards. Administrative policy, staff training, risk assessment, backup, disaster recovery,
and signed vendor agreements remain the client's responsibility. Full mapping to 45 CFR § 164.312
in [HIPAA_CHECKLIST.md](HIPAA_CHECKLIST.md).

---

## Known limitations (current)

- **No TLS / reverse proxy deployed.** The one open item from Phase 7 — see above and
  [HIPAA_CHECKLIST.md](HIPAA_CHECKLIST.md).
- **The encryption key sits beside the ciphertext.** BitLocker is the real control on this laptop.
  The placeholder key is now rejected outside `local`; its storage location has not moved.
- **Uploads are buffered in memory**, up to `MAX_UPLOAD_BYTES` per concurrent request. Fine for
  25 MB and one user; it is not a streaming pipeline.
- **A rolled-back upload can orphan a file on disk.** The row is written before the bytes, so the
  reverse — a row promising a document that does not exist — cannot happen. An unreferenced
  ciphertext is inert. A reaper is deferred.
- **Every document is visible to every authenticated user.** There is one account, and no
  ownership check on `GET /documents/{id}`. RBAC is post-pilot.
- **Refresh tokens are not rotated on use.** Revocation is all-or-nothing via `token_version`.
- **Search enumerates patients.** Any authenticated user can list every patient by name via
  `GET /documents?patient_name=...`. Fine for the single-operator pilot; a real gap the moment
  there is a second user. Tied to the no-RBAC limitation above.
- **Review UI renders one page at a time.** `GET /documents/{id}/page/{n}` re-rasterizes on demand
  at the OCR DPI so the bbox overlay lines up; there is no server-side page-image cache (the browser
  caches per page). Fine for one operator; revisit if a review view thrashes pages.
- **OCR runs on CPU** (~23 s/page on this laptop) by design; the LLM uses the host GPU via LM
  Studio. `redact_processor` runs on every log record — a regex sweep prohibited alongside
  `DEBUG`/`DB_ECHO` in production anyway.
- **Rate limiting is in-memory and per-process.** Correct for the pilot's single uvicorn worker;
  wrong the moment there is more than one worker, which would need a shared backend (Redis).
- **No emergency access procedure.** One administrator account; see
  [HIPAA_CHECKLIST.md](HIPAA_CHECKLIST.md) § 164.312(a)(2)(ii).

---

## Next phase

Phase 7 is functionally done except for TLS / reverse-proxy deployment (a scoped decision, not an
oversight — see [HIPAA_CHECKLIST.md](HIPAA_CHECKLIST.md) § 164.312(e)(2)(ii)). What remains:

- Stand up a reverse proxy (Caddy or nginx) terminating TLS, pointed at `127.0.0.1:8000`.
  `client_ip()` (`app/api/deps.py`) already trusts `X-Forwarded-For` from a loopback peer, so no
  further application code should need to change.
- An emergency access procedure, once there is more than one operator.

Shorter operational to-dos: LM Studio hardening and the deferred pixel-overlay review view.
