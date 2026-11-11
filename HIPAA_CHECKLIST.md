# HIPAA technical-safeguards checklist

Maps 45 CFR § 164.312 (Technical Safeguards) to what this codebase actually does, with file
references. Written the way [README.md § Security posture](README.md#security-posture-stated-honestly)
is: implemented is implemented, a gap is named as a gap, and nothing here is marketing copy.

Software cannot make an organization HIPAA compliant on its own. This checklist covers the
**technical** safeguards a codebase can implement. Administrative safeguards — risk assessment,
workforce training, sanction policy, business associate agreements, incident response, backup and
disaster recovery, breach notification procedure — are the client's responsibility and are out of
scope for this document. Physical safeguards (facility access, device and media controls) are
partially covered where software has a stake (encryption at rest as a compensating control for a
lost or stolen device) and otherwise out of scope.

Status legend: **Done** — enforced in code, not just documented. **Partial** — real but with a
named limit. **Gap** — not true yet, with the reason and what closes it.

## § 164.312(a)(1) Access control

### (a)(2)(i) Unique user identification — *Required* — **Done**

`users.email` is unique (`app/models/user.py`); every JWT carries `sub` (the user's UUID) and
every audit row carries `user_id`. There is no shared or anonymous credential.

### (a)(2)(ii) Emergency access procedure — *Required* — **Gap**

No break-glass procedure exists. The pilot has exactly one administrator account
(`python -m app.cli seed-admin`); if that operator is unavailable, `reset-password` requires shell
access to the host, not an in-app emergency path. Acceptable for a single-operator pilot; revisit
before a second operator or an on-call rotation exists.

### (a)(2)(iii) Automatic logoff — *Addressable* — **Done**

`access_token_ttl_minutes = 15` (`app/core/config.py`), stated explicitly as the enforcement point
for this control in the field's own comment. Refresh tokens (`refresh_token_ttl_days = 7`) extend a
session but are not rotated on use — see § 164.312(d) below.

### (a)(2)(iv) Encryption and decryption — *Addressable* — **Partial**

Document bodies and filenames are AES-256-GCM at rest (`app/core/crypto.py`,
`app/storage/encrypted_local.py`), with the storage key bound as GCM associated data so swapping
two files on disk fails to authenticate rather than silently returning the wrong patient's
document. The gap: `STORAGE_ENCRYPTION_KEY` still lives in `.env` on the same disk as the
ciphertext (`Settings._require_a_real_storage_key` in `app/core/config.py` only rejects the
shipped placeholder outside `local`, it does not move the key to separate storage). BitLocker,
not this variable's location, is the actual control against device theft — see
[README.md § Security posture](README.md).

## § 164.312(b) Audit controls — *Required* — **Done**

`audit_logs` (`app/models/audit_log.py`) records every authentication event and every PHI
disclosure (document view, OCR-result read, extraction read, page render, download, search) —
`AuditService.record` / `record_atomic` (`app/services/audit_service.py`). Two things make this a
safeguard rather than a convention:

- `AuditRepository` (`app/repositories/audit_repository.py`) exposes exactly one method, `record`
  — no update, no delete, in the Python code.
- The `add_app_role` migration (`alembic/versions/20260711_0800_add_app_role.py`) makes that a
  database-enforced guarantee: the runtime role the app connects as
  (`app/db/session.py::app_database_url`) has `SELECT, INSERT` on `audit_logs` and explicitly not
  `UPDATE` or `DELETE`. A stray `session.execute(update(AuditLog))` now fails at the database, not
  just by nobody having written that code path. Only the owner role (`postgres_user`, used by
  Alembic) can modify or purge audit history — an out-of-band DBA operation, never the running app.

Audit `detail` is PHI-free by rule — identifiers, counts, and reasons only, enforced by convention
and reviewed at every new call site; the search endpoint's audit records `{resource: "search",
count}`, never the search terms.

## § 164.312(c)(1) Integrity

### (c)(2) Mechanism to authenticate ePHI — *Addressable* — **Done**

Two independent mechanisms: `Document.sha256` (`app/models/document.py`) proves the stored
ciphertext still decrypts to the bytes originally uploaded and drives dedup; AES-**GCM** is an AEAD
cipher, so decryption itself fails (`app/core/crypto.py::AesGcmCipher`) if the ciphertext or its
bound associated data (the storage key) has been altered, rather than silently returning corrupted
plaintext.

## § 164.312(d) Person or entity authentication — *Required* — **Done**

Argon2id password hashing with explicit, stated parameters (`app/core/security.py`); JWT `algorithms`
is a whitelist of one, closing the `alg: none` confusion attack; `typ` is checked on decode so a
refresh token cannot be presented as a bearer credential. Login returns one generic 401 for every
failure mode (unknown email, bad password, inactive account) with wall-clock time equalized via
`waste_time_like_a_verification()` — distinguishing them is an account-enumeration oracle.

**Brute-force**, previously an open gap, is closed this phase: `/auth/login` and `POST /documents`
are rate limited per client IP (`app/core/rate_limit.py`, `Settings.rate_limit_login_per_minute` /
`rate_limit_upload_per_minute`), in-memory and per-process — correct for the pilot's single uvicorn
worker, and the first thing that needs a shared backend (Redis) if that ever changes.

## § 164.312(e)(1) Transmission security

### (e)(2)(i) Integrity controls — *Addressable* — **N/A at this layer**

No TLS termination point exists yet (see below), so there is no in-transit integrity control to
name independently of encryption. Revisit alongside TLS.

### (e)(2)(ii) Encryption — *Addressable* — **Gap, deferred by decision**

The API binds `127.0.0.1:8000` — nothing reaches it except what is already on this host — but
there is no TLS termination in front of it, and no reverse proxy is deployed. This was a scoped
decision for this phase, not an oversight: `client_ip()` (`app/api/deps.py`) and the rate limiter's
key function (`app/core/rate_limit.py`) were hardened to trust `X-Forwarded-For` only when the
direct TCP peer is loopback (`app/core/net.py::resolve_client_ip`), so a same-host reverse proxy
(Caddy or nginx) becomes usable by pointing it at `127.0.0.1` with no further code change — but
standing one up, and terminating TLS, remains undone.

## Not part of § 164.312, but adjacent gaps worth naming here

- **No RBAC.** Every authenticated user sees every document (`app/api/deps.py::require_role` is
  wired but unused — one admin account in the pilot). `GET /documents?patient_name=...` lets any
  authenticated user enumerate every patient by name. Post-pilot.
- **Refresh tokens are not rotated on use.** Revocation is all-or-nothing via `users.token_version`.
- **Orphaned ciphertext.** A rolled-back upload can leave an unreferenced encrypted file on disk.
  Inert (nothing references it), but a reaper is still deferred.

## What closed this phase (Phase 7)

- Rate limiting on `/auth/login` and `POST /documents` (slowapi, in-memory).
- `add_app_role` migration: a restricted runtime database role, `REVOKE`-enforced append-only
  `audit_logs`, separate from the migration-owner role.
- `client_ip()` trusts `X-Forwarded-For` only from a loopback peer — proxy-ready, not proxy-reliant.
- `storage_encryption_key` placeholder rejected outside `local`, mirroring the existing
  `jwt_secret_key` check.
- A positive import-linter check (`tests/unit/test_import_contract.py`) — every module in
  `app/{ocr,storage,llm}` must be named in the "services never import adapters" contract or
  declared a plain utility, so a new adapter cannot silently slip past the blocklist.
- A PHI-in-logs test over the real OCR → LLM pipeline, not just synthetic log calls
  (`tests/integration/test_pipeline.py::TestPhiNeverReachesLogs`), and an error-body scrub audit
  across authentication, validation, not-found, and unhandled-exception responses
  (`tests/integration/test_error_body_scrub.py`).
- This checklist.

## What Phase 7 explicitly did not close

- TLS / reverse proxy deployment (scoped out this session — see § 164.312(e)(2)(ii) above).
- Emergency access procedure.
- RBAC, refresh-token rotation, the orphaned-ciphertext reaper.

Revisit before any pilot use beyond this one developer's machine: no real document belongs near
this system before Phase 7's TLS gap closes — that was true of the whole phase and still holds now
that the rest of Phase 7 is done.
