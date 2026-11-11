# Frontend guidelines — local-doc-ocr

Read [README.md](README.md) first for what the system does and how to run it. This
doc covers the rules specific to touching `app/static/index.html` — the only frontend that exists
today — or anything you build next to it.

## Handoff — read this first if you're new here

One file: `app/static/index.html`. Vanilla HTML/CSS/JS, no build step, no framework, no bundler,
served straight off FastAPI's `StaticFiles` mount at `/ui`. That's deliberate: it's a thin ops UI
for now, with a real frontend deferred until the reviewer workflow itself has been validated.
Building a component-framework app against a spec that might still change would just mean
rewriting it twice. Don't reach for React, Vue, a bundler, or npm unless someone explicitly tells
you to replace the whole thing. If you *are* asked to build a real frontend eventually, treat this
file as the behavioral spec, not as code you keep extending forever.

**Built and working today:** login with a 15-minute JWT and a visible countdown, upload with a live
pipeline stepper, document search that shows OCR mean confidence per hit, a review panel with
extracted fields plus a page-image bbox overlay, inline per-field correction, a
confidence-ranked review queue that steps through completed docs without bouncing back to search,
reprocess (re-run OCR + extraction on a failed or completed doc), and a role-gated audit-trail
viewer. The visual theme is a light clinical-chart look now — see § Design/UX conventions below —
not the dark dispatch-console theme older commits or screenshots might still show.

A few known gaps, none blocking, roughly in the order they'd matter if you're picking one to work
on next:

- No JS test framework, by design (see § Before you ship), so every change still needs a manual
  browser pass. There's no scripted checklist beyond the bullet list in that section — worth
  writing one down as you run into edge cases.
- The reprocess and audit-trail buttons and panels are unconditionally rendered: no loading
  skeleton, no optimistic UI. That's fine for a single-operator pilot, but worth revisiting once
  this UI has more than one concurrent user.
- The audit-trail panel has no filter UI yet. The backend already supports `user_id`,
  `document_id`, and `action` query params on `GET /audit-logs` — the frontend just doesn't expose
  them.
- Bulk upload, a real retry-vs-reprocess distinction, and a document-scoped "view this doc's audit
  history" shortcut were all explicitly out of scope when the review-queue/reprocess/audit-trail
  work landed. They're listed here because a reviewer asked for them and didn't get them, not
  because they're a hidden spec you're supposed to infer.
- `git log --oneline -20`, and whichever commit introduced a given feature (search the log for
  "reprocess", "audit", "correction", "clinical theme"), will always be more current than any prose
  summary — this one included. If something here disagrees with the code, trust the code and fix
  this file.

## The one rule that overrides everything else: this handles PHI

Everything this UI displays — patient name, DOB, address, SSN, insurance, the scanned document
itself — is PHI. Every guideline below follows from that one fact.

- **Never persist PHI client-side.** No `localStorage`, no `sessionStorage`, no cookies for
  anything, including the bearer token — that lives in memory only, a plain JS variable (see
  `let token = null;` near the top of the script). A page refresh should force re-login. Don't
  "improve" this by persisting the token; it's a deliberate tradeoff, not something that got missed.
- **Clear PHI from the DOM on logout.** `logout()` already clears search hits, the review panel,
  the page image, and the source text. Any new panel that can hold PHI needs to join that
  clear-out. Don't assume the user closing the tab is the only cleanup path you need.
- **Never log PHI to the browser console.** No `console.log(patientData)` while debugging that
  quietly survives into the commit. Console output ends up in screenshots, bug reports, browser
  crash dumps — places you'd rather it didn't.
- **Never put PHI in a URL.** Query params, hash fragments, `window.location` — all of it can end
  up in browser history, proxy logs, or a screen-share. Search already sends `patient_name` as a
  query param, and that's only tolerated because it's audited server-side and because uvicorn's
  access log is disabled while `RequestContextMiddleware` logs `request.url.path`, which excludes
  the query string — so the name never actually lands on disk in plaintext. Don't add a second
  PHI-bearing query param without checking that the same logging story holds for it.
- **No third-party anything.** No CDN scripts, no external fonts, no analytics, no error-reporting
  SaaS like Sentry or LogRocket, no font or icon services. The system's whole premise is that
  nothing leaves the network, and a frontend that phones home to a CDN for a font breaks that even
  if the font itself is harmless — the request alone leaks that this system exists, from where, and
  roughly what it's doing.
- **Downloads, not previews that leak.** The document endpoint sets
  `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff` on purpose, so a PDF or
  image never renders inline in the browser's own origin. Don't work around that with an
  `<iframe>` or `<embed>` pointed at the raw bytes.

## API shape you'll be working against

- **Auth.** `POST /api/v1/auth/login` returns `{access_token, refresh_token}`. The access token
  lasts 15 minutes — that's a HIPAA auto-logoff requirement, so don't quietly lengthen it
  client-side by refreshing forever without ever re-prompting. Every authenticated call needs
  `Authorization: Bearer <token>`, and a 401 simply means log in again — there's no silent session
  extension anywhere. The existing `authed()` helper already treats 401 as a forced logout; any new
  fetch wrapper should follow the same pattern.
- **Errors have one stable shape everywhere:** `{error: "<code>", message: "<ours>", request_id}`.
  It's safe to render `error` or `message` directly since we write both — but don't assume that
  guarantee automatically extends to anything you add server-side later. A 422 validation failure
  also carries `fields: [...]`, dotted paths of what failed; field *values* are never echoed back.
- **Rate limiting is real.** Login and upload are both rate-limited per IP. A 429 carries
  `Retry-After` in seconds plus the usual `{error, message, request_id}` body, with
  `error: "rate_limited"`. Handle it like any other error — the message is written to be shown
  as-is ("Too many requests. Try again shortly.").
- **Document status is a state machine:** `pending → ocr_running → ai_running → completed | failed`.
  Poll `GET /documents/{id}` and don't expect a single request to ever return the final state — OCR
  alone runs about 20 seconds a page on this hardware, and a full pipeline run can take minutes.
  `poll()` and `paintPipe()` are the existing pattern: poll on a timer, update the UI on a state
  *transition*, not on every tick.
- **Extraction fields carry two values, not one.** Read this before building anything that shows
  extracted data:

  ```js
  { value, confidence, source_page, source_bbox,   // grounded: proven on the page
    typed_value, source }                          // what a prebiller types into the billing system
  ```

  `source` is one of `grounded | reformatted | sop_default | skipped | absent | corrected`. Always
  show `typed_value` as the primary display value, and treat `sop_default` as a special case — it's
  a rule supplying a placeholder (DOB `01/01/1900` when none was found, say), not a reading, and it
  has no `source_bbox` because there's nothing on the page to highlight. Never present a
  `sop_default` field as though it came from the document itself. `sourceBadge()` in the current UI
  is the reference implementation for this distinction — copy its logic rather than reinventing it.
- **Insurance is a list, not a flat field.** `PatientExtraction.insurance` is `0..3` slots
  (`fields.insurance[0].policy_id`, etc.) — a face sheet can carry primary/secondary/tertiary
  coverage at once, each with its own `insurance_name`/`policy_id`/`group_number`/`subscriber_name`/
  `relationship_to_subscriber`/`effective_date`/`verified`. Each of those is a normal two-value field
  (same `value`/`typed_value`/`source` shape as everything else) — render a repeating card per slot
  rather than a fixed insurance section, and don't assume slot `[0]` exists on every document.
- **The narrative summary is unverified prose.** `GET .../extraction` may carry
  `narrative_summary` (EPCR documents only, nullable) — an LLM digest of the crew narrative. It is
  generated text the hallucination guard cannot prove: always render it visibly labeled as an
  unverified AI summary (`renderNarrativeSummary()` in the current UI), never mixed into the
  grounded field rows, and never used to prefill a field.
- **Advisory flags ride the extraction response.** `GET .../extraction` (and the PATCH response)
  carries `suggested_flags: [{code, detail}]` — read-time SOP hints (e.g. `cbo_address_used`,
  `bcbstx_hmo_prefix`), computed server-side from the stored fields. `detail` holds field names
  and slot indices only, never document values. Render as informational chips (`renderFlags()` in
  the current UI); an empty list is the normal case, and absence of the key (older cached
  responses) must render the same as empty.
- **A reviewer can correct a field.** `PATCH /documents/{id}/extraction/fields/{field_name}` with
  body `{typed_value}` overwrites the typed value, sets `source: "corrected"` server-side, and
  returns the full `ExtractionRead`, same shape as `GET .../extraction`. There's no per-field id —
  the URL path segment *is* the field name for a top-level field (one of the keys `PatientExtraction`
  returns), or a dotted path `insurance.{index}.{subfield}` for an insurance-slot field (e.g.
  `insurance.0.policy_id`) — a 404 means either an unknown field name or an index past the slots this
  extraction actually has. The request body is the only place the corrected value should ever
  travel; it never belongs in the URL. Redraw just the edited row from the response instead of
  re-fetching the whole document. The correction gets audited as `extraction_field_corrected` with
  the field *name* only in `detail`, never the value — that's enforced server-side, not something
  the frontend has to worry about.
- **The review queue is a feed array plus an index, not a route.** The current UI keeps `hits`
  (everything the last search returned) and derives `queue` from it — a filtered, ordered subset,
  say `status === "completed"` — plus a `queueIdx`. "Next" just increments the index and calls the
  same `openDoc()` a direct click would use.
- **Search results carry OCR confidence now.** `DocumentListItem.mean_confidence` (`float | null`,
  null until OCR has actually run) is joined in server-side —
  `DocumentRepository.search` LEFT JOINs `OcrResult`. This is the document's mean confidence, not
  per-field confidence. The queue sorts on it client-side —
  `queue.sort((a,b) => (a.mean_confidence ?? 1) - (b.mean_confidence ?? 1))`, lowest first — over
  whatever page of search results is already sitting in `hits`. That's a local sort over an
  already-fetched page, not a server-side ordering, so don't assume the queue covers every match if
  the search has more pages than what's loaded.
- **Reprocess.** `POST /documents/{id}/reprocess` (no body) re-arms a `failed` or `completed`
  document back to `pending` and reschedules the pipeline: `202` with `{id, status: "pending"}`, or
  `409` if the document is already mid-pipeline. The upload flow's poll loop
  (`poll()`/`pollTimer`) is specific to that flow, so reprocess runs its own timer
  (`pollReprocess()`/`reprocessPollTimer`) rather than sharing it — the two would otherwise clobber
  each other if both happened to be active at once. On completion, re-fetch `.../extraction` and
  redraw the fields panel instead of telling the reviewer to reopen the document themselves.
- **Audit trail.** `GET /audit-logs` is role-gated (`require_role(ADMIN, AUDITOR)`), so a perfectly
  valid token can still come back with a `403` here even though every document route accepts any
  authenticated user. It's keyset-paginated the same way document search is, but the cursor fields
  are `cursor_occurred_at`/`cursor_id` rather than `cursor_created_at` — `AuditLog.id` alone isn't a
  safe stand-in for time order, and `AuditLogCursor`'s docstring in `app/schemas/audit_log.py`
  explains why if you're curious. Every field on an audit row is PHI-free by construction, so this
  is the one panel in the UI that needs no redaction thinking at all — just render it.
- **Session countdown.** The login response carries `expires_in` — seconds until the access token
  expires — specifically so the client can drive a countdown without polling for it. Compute
  `tokenExpiresAt = Date.now() + expires_in * 1000` once at login and tick a `setInterval` off that;
  don't try to re-derive it from a decoded JWT. When it hits zero, call `logout()` proactively
  rather than waiting for the next request to come back 401. Any poll loop that happens to outlive
  the token — reprocess, upload — will hit `authed()`'s own 401 handling and log out on its own.
  That's expected behavior, not something to work around.
- **The bbox overlay is DPI-locked.** `GET /documents/{id}/page/{n}` renders at the server's
  configured OCR DPI, so `source_bbox` coordinates land correctly once scaled against the image's
  `naturalWidth`/`naturalHeight`. If you ever add client-side resizing or cropping before computing
  overlay positions, scale from `naturalWidth`/`naturalHeight` — never from the displayed CSS size.

## Design/UX conventions already in place

- **Light clinical-chart aesthetic** now, not the dark dispatch-console look this file used to
  describe. A pale teal-wash page (`--bg`), white elevated panels (`--panel`, with a soft
  `box-shadow` rather than a flat border-only tile), and one accent color — `--accent`, a deep
  clinical teal — all as CSS custom properties. Match it, or replace it deliberately; don't leave
  it half-matched.
- **Two type registers, not one — this is the convention that actually matters.** `--sans` is
  chrome: buttons, form labels, `.eyebrow` section headers, the pipeline step names. `--mono` is
  reserved for actual data — extracted field values (`.fv`, `.fv-raw`), OCR text (`.source`,
  `pre.ocr`), audit rows, search-hit identifiers, confidence and stat numbers. It's the same split
  a real paper chart makes: the form label is typeset differently from the reading underneath it.
  When adding a new element, ask whether it's a control/label or a piece of data, and pick the
  family accordingly. Most elements already declare their own `font-family` explicitly for exactly
  this reason, so check the nearest sibling before assuming you'll inherit the right one.
- Keep the status color convention consistent: green (`--go`) means good, exact, or completed;
  amber (`--amber`) means attention, near-match, or low-confidence; red (`--stop`) means failed or
  error; teal (`--accent`) means interactive or reformatted.
- Confidence chips: exact (1.0, green), near (0.7, amber), none (grey). Source badges: grounded
  (reuses the confidence tier), fmt (teal), rule (amber), skip (grey, strikethrough), corr (teal,
  same tone as fmt — a human edit reads as "trust this," not as a separate warning). Chip tints run
  higher opacity than a dark theme would need — roughly .14–.16 alpha instead of .06–.10 — because
  a light tint on a near-white panel washes out at low opacity in a way it never would on a dark
  one. If you add a new chip variant, check it against `.panel`'s actual white rather than eyeballing
  the hex value in isolation.
- The pipeline stepper's signature touch is the connecting line strung through the stage dots
  (`.step::before`) — `--line` by default, `--go` once a stage is reached, reading like a
  vitals-trace advancing rather than four independent dots. Preserve that connector if you touch
  `.pipe`; it's the one deliberately "designed" element in an otherwise plain, utilitarian UI, so
  resist the urge to add more decoration elsewhere to compensate.
- No emoji, no decorative icons beyond the status dots. This is an operational tool, not a consumer
  app — the clinical feel is supposed to come from palette and type discipline, not iconography.
- A destructive-sounding action like reprocess gets the same button treatment as everything else —
  no red "danger" styling. It re-arms a document; it doesn't delete anything. Treat it as routine,
  because it is.
- Any color you add now needs to hold contrast against a *light* background — text colors here are
  tuned to pass roughly AA against white and `--panel-2`. Don't reuse a bright, saturated value that
  only worked because the old theme's background was near-black.

## Before you ship a change here

1. `docker compose exec api ruff check app tests` and `ruff format --check` don't touch HTML or JS
   directly, but if your frontend feature needs a backend endpoint, those gates and the test suite
   (`docker compose exec api python -m pytest -v`) apply to that backend code.
2. Manually drive the flow in a browser — login, upload, poll to completion, search, open a review,
   click a field. There's no JS test framework here by design (see § Handoff — one file, no build
   step), so don't add one just to cover this UI unless it genuinely outgrows being one file.
3. Check that `tests/unit/test_static_ui.py` still passes. It's a canary confirming the UI is still
   mounted and still references the API paths it depends on (`/api/v1/auth/login`,
   `/api/v1/documents`) — if you rename an endpoint, this is what catches the stale reference left
   behind in the HTML.
4. If you touch anything that renders PHI, go back and reread the PHI section above, and check
   specifically: does this leak into the console, into localStorage, into the URL, or into a
   third-party request.

## Branching and pushing your work

Work on a branch, never commit straight to `master`. This repo has had commits land directly on
`master` before (check `git log` if you want to see it), but that's not the convention to copy —
do it properly:

```powershell
cd local-doc-ocr

# 1. Make sure you're starting from an up-to-date master.
git checkout master
git pull

# 2. Create your branch. Name it for what it does, e.g.:
git checkout -b feat/audit-trail-filters

# 3. Work, then check what you're about to commit.
git status
git diff --stat

# 4. Stage and commit. Scope the add to what you actually touched —
#    avoid `git add -A`, it's easy to sweep in a stray .env or similar.
git add app/static/index.html FRONTEND_GUIDELINES.md
git commit -m "Add audit-trail filter UI"

# 5. Push the branch to GitHub. -u sets upstream so future `git push`
#    on this branch doesn't need the full remote/branch name again.
git push -u origin feat/audit-trail-filters
```

Then open a pull request against `master` — either `gh pr create --base master` if you have the
GitHub CLI signed in, or just open the repo on GitHub, where a "Compare & pull request" prompt
shows up automatically once the branch has a push behind it.

Before staging, skim `git status` and `git diff` for anything that shouldn't leave your machine —
`.env`, real documents, anything with PHI in it. None of that belongs in a commit, branch, or PR,
same rule as everywhere else in this doc.

## Who to ask

README.md has the full architecture and PHI rules this frontend needs to respect, and
README.md has the running and login instructions. If a requirement here seems to conflict
with a product ask — "can we add Google Fonts," "can we cache the token so people don't have to
re-login" — treat that as a signal to stop and confirm with whoever owns the HIPAA posture before
proceeding. These aren't arbitrary style choices; they're the controls the system's compliance
story actually depends on.
