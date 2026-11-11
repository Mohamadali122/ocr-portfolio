"use strict";

const $ = (id) => document.getElementById(id);
const STEPS = ["pending", "ocr_running", "ai_running", "completed"];
const LOW = 0.8;

let token = null;
let refreshToken = null;
let refreshInFlight = false;
let lastActivityRefreshAt = 0;
// Idle-timeout, not a fixed session length: real user activity (clicks,
// keypresses) silently mints a fresh access token via /auth/refresh, so an
// active reviewer is never logged out mid-session. Background polling
// (poll(), pollPipeline(), pollReprocess()) never fires a click/keydown, so
// an unattended tab still logs out once the current access token expires --
// the HIPAA 164.312(a)(2)(iii) automatic-logoff behavior CLAUDE.md documents
// stays intact; this only stops it from firing on someone still at the desk.
const ACTIVITY_REFRESH_THROTTLE_MS = 60000;
let pollTimer = null;
let reprocessPollTimer = null;
let pipelineTimer = null;
let isAdmin = false;
let auditCursor = null;
let auditLastDay = null;
let searchCursor = null;
let searchParams = "";
let claimSearchCursor = null;
let claimSearchParams = "";
let lastStatus = null;
let tokenExpiresAt = null;
let sessionTimerInterval = null;
let ocrLines = [];
let uploadedId = null;
let hits = [];
let queue = [];
let queueIdx = 0;

let review = {
  id: null,
  status: null,
  fields: {},
  page: 1,
  maxPage: 1,
  selKey: null,
  natural: {},
  cache: {},
};

const sections = ["uploadSection", "claimsSection", "searchSection", "reviewSection", "auditSection", "pipelineSection"];

let claimId = null;
let claimPollTimer = null;

function setPanelState(id, state) {
  const panel = $(id);
  if (!panel) return;
  if (state) panel.dataset.state = state;
  else delete panel.dataset.state;
}



function switchTab(sectionId) {
  const navLinks = Array.from(document.querySelectorAll(".nav-link"));
  navLinks.forEach((link) => {
    link.classList.toggle("is-active", link.dataset.section === sectionId);
  });
  sections.forEach((id) => {
    const node = $(id);
    if (node) {
      if (id === sectionId) {
        node.classList.remove("hidden");
        // Force reflow for animation restart
        void node.offsetWidth;
      } else {
        node.classList.add("hidden");
      }
    }
  });
}

function setupNav() {
  const navLinks = Array.from(document.querySelectorAll(".nav-link"));
  navLinks.forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      if (link.getAttribute("aria-disabled") === "true") return;
      switchTab(link.dataset.section);
    });
  });
}

function setReviewNavEnabled(enabled) {
  const link = document.querySelector('.nav-link[data-section="reviewSection"]');
  if (!link) return;
  link.setAttribute("aria-disabled", enabled ? "false" : "true");
  link.tabIndex = enabled ? 0 : -1;
}

function setMsgTone(node, tone) {
  node.classList.remove("err", "ok", "info");
  if (tone) node.classList.add(tone);
}

function renderInlineState(id, text, tone) {
  const node = $(id);
  setMsgTone(node, tone);
  node.textContent = text;
}

async function login(email, password) {
  const res = await fetch("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) return false;
  const data = await res.json();
  token = data.access_token;
  refreshToken = data.refresh_token;
  tokenExpiresAt = Date.now() + data.expires_in * 1000;
  return true;
}

async function refreshSession() {
  if (!token || !refreshToken || refreshInFlight) return;
  refreshInFlight = true;
  try {
    const res = await fetch("/api/v1/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!res.ok) return; // refresh token itself expired/revoked -- let the
    // access-token countdown run out and log out normally, same as any idle session.
    const data = await res.json();
    token = data.access_token;
    refreshToken = data.refresh_token;
    tokenExpiresAt = Date.now() + data.expires_in * 1000;
  } catch {
    /* network hiccup -- same fallback as above */
  } finally {
    refreshInFlight = false;
  }
}

function onUserActivity() {
  if (!token) return;
  const now = Date.now();
  if (now - lastActivityRefreshAt < ACTIVITY_REFRESH_THROTTLE_MS) return;
  lastActivityRefreshAt = now;
  refreshSession();
}

function startSessionTimer() {
  if (sessionTimerInterval) clearInterval(sessionTimerInterval);
  $("sessionTimer").classList.remove("hidden");
  updateSessionTimer();
  sessionTimerInterval = setInterval(updateSessionTimer, 1000);
}

function updateSessionTimer() {
  const remaining = Math.max(0, Math.round((tokenExpiresAt - Date.now()) / 1000));
  const minutes = Math.floor(remaining / 60);
  const seconds = remaining % 60;
  const pill = $("sessionTimer");
  pill.textContent = "session " + minutes + ":" + String(seconds).padStart(2, "0");
  pill.classList.toggle("low", remaining <= 60);
  if (remaining <= 0) logout("Session expired. Sign in again.");
}

function clearPageCache() {
  for (const url of Object.values(review.cache)) URL.revokeObjectURL(url);
  review.cache = {};
  review.natural = {};
}

function resetPageImage() {
  $("pageImg").src = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
  $("pageImg").classList.add("hidden");
  $("boxLayer").innerHTML = "";
  $("pageMsg").textContent = "Open a document to load the page image.";
  $("pageMsg").classList.remove("hidden");
}

function resetReviewState() {
  clearPageCache();
  review = {
    id: null,
    status: null,
    fields: {},
    page: 1,
    maxPage: 1,
    selKey: null,
    natural: {},
    cache: {},
  };
  ocrLines = [];
  setReviewNavEnabled(false);
  $("review").classList.add("hidden");
  $("fields").innerHTML = "";
  $("fieldsMsg").classList.add("hidden");
  $("source").innerHTML = "";
  resetPageImage();
  $("srcHint").textContent = "";
  $("reviewName").textContent = "-";
  $("pageCounter").textContent = "Page 1 of 1";
  $("queueBar").classList.add("hidden");
  $("reprocessBtn").classList.add("hidden");
  $("reprocessMsg").classList.add("hidden");
}

function logout(reason) {
  token = null;
  refreshToken = null;
  lastActivityRefreshAt = 0;
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  if (reprocessPollTimer) {
    clearTimeout(reprocessPollTimer);
    reprocessPollTimer = null;
  }
  if (pipelineTimer) {
    clearTimeout(pipelineTimer);
    pipelineTimer = null;
  }
  if (claimPollTimer) {
    clearTimeout(claimPollTimer);
    claimPollTimer = null;
  }
  claimId = null;
  $("claimDetailPanel").classList.add("hidden");
  $("claimReprocessBtn").classList.add("hidden");
  $("claimReprocessMsg").classList.add("hidden");
  $("claimUploadMsg").textContent = "";
  isAdmin = false;
  $("pipelineNavLink").classList.add("hidden");
  $("pipelineNavBadge").classList.add("hidden");
  $("pipelineSummary").textContent = "-";
  for (const id of ["pipelineInFlight", "pipelineInterrupted", "pipelineFailed"]) {
    $(id).innerHTML = "";
  }
  $("bulkReprocessBtn").disabled = true;
  $("bulkReprocessMsg").classList.add("hidden");
  if (sessionTimerInterval) {
    clearInterval(sessionTimerInterval);
    sessionTimerInterval = null;
  }
  tokenExpiresAt = null;
  auditCursor = null;
  auditLastDay = null;
  searchCursor = null;
  uploadedId = null;
  hits = [];
  queue = [];
  queueIdx = 0;

  $("sessionTimer").textContent = "";
  $("sessionTimer").classList.remove("low");
  $("sessionTimer").classList.add("hidden");
  $("who").textContent = "";
  $("hits").innerHTML = "";
  $("moreBtn").classList.add("hidden");
  $("startQueueBtn").classList.add("hidden");
  $("hitsEmpty").classList.add("hidden");
  $("searchMsg").classList.add("hidden");
  $("auditHits").innerHTML = "";
  $("auditMoreBtn").classList.add("hidden");
  $("auditEmpty").classList.add("hidden");
  $("auditMsg").classList.add("hidden");
  $("auditAction").value = "";
  $("results").classList.add("hidden");
  $("reviewUploadBtn").classList.add("hidden");
  $("failBox").classList.add("hidden");
  $("mainNav").classList.add("hidden");
  $("loadingSkeleton").classList.add("hidden");
  $("log").innerHTML = "<div>Idle: no document submitted.</div>";
  $("uploadMsg").textContent = "";
  resetReviewState();
  $("app").classList.add("hidden");
  $("logoutBtn").classList.add("hidden");
  const pw = $("password");
  pw.value = "";
  if (pw.type !== "password") {
    pw.type = "password";
    const toggle = $("passwordToggle");
    toggle.setAttribute("aria-pressed", "false");
    toggle.setAttribute("aria-label", "Show password");
    toggle.querySelector(".eye-open").classList.remove("hidden");
    toggle.querySelector(".eye-closed").classList.add("hidden");
  }
  $("login").classList.remove("hidden");
  renderInlineState("loginMsg", reason || "", reason ? "err" : "");
}

async function authed(url, opts) {
  opts = opts || {};
  const res = await fetch(url, {
    ...opts,
    headers: { ...(opts.headers || {}), Authorization: "Bearer " + token },
  });
  if (res.status === 401) {
    logout("Session expired. Sign in again.");
    throw new Error("401");
  }
  return res;
}

function label(key) {
  // Insurance slot keys are dotted paths (insurance.0.policy_id); label the leaf.
  const leaf = key.includes(".") ? key.split(".").pop() : key;
  return leaf.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// The API returns `fields.insurance` as a list of 0-3 slot objects. Flatten
// each slot into dotted keys matching the PATCH path the backend expects
// (`insurance.0.policy_id`), so rows, edits, and bbox drawing stay uniform.
function flattenFields(fields) {
  const flat = {};
  for (const [key, value] of Object.entries(fields)) {
    if (key === "insurance") continue;
    flat[key] = value;
  }
  for (const [index, slot] of (fields.insurance || []).entries()) {
    for (const [sub, field] of Object.entries(slot)) {
      flat["insurance." + index + "." + sub] = field;
    }
  }
  return flat;
}

function sourceBadge(field) {
  const chip = document.createElement("span");
  let cls;
  let text;
  switch (field.source) {
    case "grounded":
      cls = field.confidence >= 1 ? "exact" : field.confidence > 0 ? "near" : "none";
      text = cls;
      break;
    case "reformatted":
      cls = "fmt";
      text = "fmt";
      break;
    case "sop_default":
      cls = "rule";
      text = "rule";
      break;
    case "skipped":
      cls = "skip";
      text = "skip";
      break;
    case "corrected":
      cls = "corr";
      text = "corr";
      break;
    default:
      cls = "none";
      text = "none";
  }
  chip.className = "chip " + cls;
  chip.textContent = text;
  return chip;
}

function paintPipe(status) {
  const failed = status === "failed" || status === "extraction_failed";
  const idx = STEPS.indexOf(status);
  document.querySelectorAll("#pipe .step").forEach((el, i) => {
    el.classList.remove("active", "done", "fail");
    if (failed) {
      if (i < STEPS.length - 1) el.classList.add("done");
      if (i === STEPS.length - 1) el.classList.add("fail");
    } else if (status === "completed") {
      el.classList.add("done");
    } else if (idx >= 0 && i < idx) {
      el.classList.add("done");
    } else if (i === idx) {
      el.classList.add("active");
    }
  });
}

function logLine(text, now) {
  const log = $("log");
  const line = document.createElement("div");
  if (now) line.className = "now";
  line.textContent = new Date().toLocaleTimeString() + "  " + text;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function resetPipelinePanel() {
  lastStatus = null;
  $("log").innerHTML = "";
  $("results").classList.add("hidden");
  $("reviewUploadBtn").classList.add("hidden");
  $("failBox").classList.add("hidden");
  $("loadingSkeleton").classList.add("hidden");
  document.querySelectorAll("#pipe .step").forEach((el) =>
    el.classList.remove("active", "done", "fail")
  );
}

async function showOcr(id) {
  let res;
  try {
    res = await authed("/api/v1/documents/" + id + "/ocr");
  } catch {
    return;
  }
  if (!res.ok) {
    logLine("OCR result unavailable (" + res.status + ")");
    return;
  }
  const result = await res.json();
  const pct = (result.mean_confidence * 100).toFixed(1) + "%";
  $("confVal").textContent = pct;
  $("pageVal").textContent = result.page_count;
  $("durVal").textContent = (result.duration_ms / 1000).toFixed(1) + "s";
  $("confStat").classList.toggle("low", result.mean_confidence < LOW);
  $("lowBadge").classList.toggle("hidden", result.mean_confidence >= LOW);
  if (result.mean_confidence < LOW) {
    logLine("Low confidence " + pct + ". Verify against source.", true);
  }
  $("ocrText").textContent = result.raw_text || "(no text recognised)";
  $("results").classList.remove("hidden");
}

function showFailure(reason) {
  const box = $("failBox");
  box.textContent = "OCR failed: " + (reason || "no reason recorded.");
  box.classList.remove("hidden");
}

async function poll(id) {
  let res;
  try {
    res = await authed("/api/v1/documents/" + id);
  } catch {
    return;
  }
  if (!res.ok) {
    logLine("Status check failed (" + res.status + ").");
    return;
  }
  const doc = await res.json();
  if (doc.status !== lastStatus) {
    logLine(lastStatus + " -> " + doc.status, true);
    lastStatus = doc.status;
    paintPipe(doc.status);
    
    const skelText = document.querySelector("#loadingSkeleton .skeleton-text");
    if (skelText) {
      if (doc.status === "ocr_running") skelText.textContent = "Running Optical Character Recognition...";
      else if (doc.status === "ai_running") skelText.textContent = "Extracting fields with AI model...";
      else skelText.textContent = "Processing document...";
    }

    const lines = $("log").querySelectorAll(".now");
    lines.forEach((line, index) => {
      if (index < lines.length - 1) line.classList.remove("now");
    });
  }
  if (doc.status === "completed") {
    $("loadingSkeleton").classList.add("hidden");
    setPanelState("pipelinePanel", "");
    await showOcr(id);
    uploadedId = id;
    $("reviewUploadBtn").classList.remove("hidden");
    renderInlineState("uploadMsg", "Completed. Ready for review.", "ok");
    return;
  }
  if (doc.status === "failed" || doc.status === "extraction_failed") {
    $("loadingSkeleton").classList.add("hidden");
    setPanelState("pipelinePanel", "");
    showFailure(doc.failure_reason);
    renderInlineState(
      "uploadMsg",
      doc.status === "extraction_failed"
        ? "No structured data could be extracted. Nothing to review."
        : "Processing failed. Review the failure reason.",
      "err"
    );
    return;
  }
  pollTimer = setTimeout(() => poll(id), 1500);
}

function buildQuery() {
  const params = new URLSearchParams();
  const name = $("qName").value.trim();
  const run = $("qRun").value.trim();
  const dob = $("qDob").value;
  if (name) params.set("patient_name", name);
  if (run) params.set("run_number", run);
  if (dob) {
    params.set("dob_from", dob);
    params.set("dob_to", dob);
  }
  return params;
}

function renderHit(doc) {
  const item = document.createElement("li");
  item.className = "hit";
  const name =
    [doc.patient_last_name, doc.patient_first_name].filter(Boolean).join(", ") ||
    "(no name extracted)";
  const nm = document.createElement("div");
  nm.className = "nm";
  nm.textContent = name;
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.innerHTML = '<span class="st ' + doc.status + '">' + doc.status + "</span>";
  const sub = document.createElement("div");
  sub.className = "sub";
  sub.textContent = [
    doc.doc_type,
    doc.patient_dob ? "DOB " + doc.patient_dob : null,
    doc.run_number ? "run " + doc.run_number : null,
    doc.mean_confidence != null ? "conf " + doc.mean_confidence.toFixed(2) : null,
  ]
    .filter(Boolean)
    .join("  ·  ");
  const sub2 = document.createElement("div");
  sub2.className = "meta sub";
  sub2.textContent = new Date(doc.created_at).toLocaleDateString();
  item.append(nm, meta, sub, sub2);
  item.addEventListener("click", () => openDocDirect(doc));
  return item;
}

function updateQueueBar() {
  const hasQueue = queue.length > 0;
  $("queueBar").classList.toggle("hidden", !hasQueue);
  if (!hasQueue) return;
  $("queueCount").textContent = queueIdx + 1 + " of " + queue.length;
  $("queuePrevBtn").classList.toggle("hidden", queueIdx <= 0);
  $("queueNextBtn").classList.toggle("hidden", queueIdx >= queue.length - 1);
}

function updatePageToolbar() {
  $("pageCounter").textContent = "Page " + review.page + " of " + review.maxPage;
  $("pagePrevBtn").disabled = review.page <= 1;
  $("pageNextBtn").disabled = review.page >= review.maxPage;
}

function updateReprocessBtn() {
  const canReprocess =
    review.status === "failed" ||
    review.status === "extraction_failed" ||
    review.status === "completed";
  $("reprocessBtn").classList.toggle("hidden", !canReprocess);
  $("reprocessBtn").disabled = false;
  $("reprocessMsg").classList.add("hidden");
}

function updateMarkReviewedBtn() {
  // Only extraction_failed: the completeness gate found something missing
  // and there's an Extraction row to correct. A plain FAILED document never
  // got one saved (see PipelineService._run_extraction) -- nothing to review.
  const canMarkReviewed = review.status === "extraction_failed";
  $("markReviewedBtn").classList.toggle("hidden", !canMarkReviewed);
  $("markReviewedBtn").disabled = false;
  $("markReviewedMsg").classList.add("hidden");
}

function clearSearchMessages() {
  $("searchMsg").classList.add("hidden");
  $("searchMsg").textContent = "";
}

function renderSearchMessage(text, tone) {
  const msg = $("searchMsg");
  setMsgTone(msg, tone);
  msg.textContent = text;
  msg.classList.remove("hidden");
}

async function runSearch(fresh) {
  const button = fresh ? $("searchBtn") : $("moreBtn");
  button.disabled = true;
  setPanelState("searchPanel", "loading");
  clearSearchMessages();
  try {
    const params = new URLSearchParams(searchParams);
    if (searchCursor) {
      params.set("cursor_created_at", searchCursor.created_at);
      params.set("cursor_id", searchCursor.id);
    }
    let res;
    try {
      res = await authed("/api/v1/documents?" + params.toString());
    } catch {
      return;
    }
    if (!res.ok) {
      let message = "Search failed (" + res.status + ").";
      try {
        message = (await res.json()).message || message;
      } catch {
        /* keep default */
      }
      renderSearchMessage(message, "err");
      return;
    }
    const page = await res.json();
    if (fresh) hits = [];
    for (const doc of page.items) {
      hits.push(doc);
      $("hits").appendChild(renderHit(doc));
    }
    searchCursor = page.next_cursor;
    $("moreBtn").classList.toggle("hidden", !page.next_cursor);
    $("hitsEmpty").classList.toggle("hidden", !(fresh && page.items.length === 0));
    $("startQueueBtn").classList.toggle("hidden", !hits.some((doc) => doc.status === "completed"));
    if (fresh && page.items.length === 0) {
      renderSearchMessage("No documents match the current filters.", "info");
    }
  } finally {
    setPanelState("searchPanel", "");
    button.disabled = false;
  }
}

function renderAuditRow(row) {
  const div = document.createElement("div");
  div.className = "audit-row";
  const act = document.createElement("div");
  act.className = "act";
  act.textContent = row.action;
  const when = document.createElement("div");
  when.className = "when";
  // Date lives in the day header above; the row only needs the time.
  when.textContent = new Date(row.occurred_at).toLocaleTimeString();
  const sub = document.createElement("div");
  sub.className = "sub";
  sub.textContent = [row.document_id ? "doc " + row.document_id.slice(0, 8) : null, row.ip_address || null]
    .filter(Boolean)
    .join("  ·  ");
  div.append(act, when, sub);
  return div;
}

function auditDayLabel(isoString) {
  const day = new Date(isoString);
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  const sameDay = (a, b) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (sameDay(day, today)) return "Today";
  if (sameDay(day, yesterday)) return "Yesterday";
  return day.toLocaleDateString(undefined, {
    weekday: "short",
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function renderAuditDayHeader(label) {
  const div = document.createElement("div");
  div.className = "audit-day";
  div.textContent = label;
  return div;
}

async function loadAuditLogs(fresh) {
  const button = fresh ? $("auditLoadBtn") : $("auditMoreBtn");
  button.disabled = true;
  setPanelState("auditPanel", "loading");
  $("auditMsg").classList.add("hidden");
  try {
    if (fresh) {
      $("auditHits").innerHTML = "";
      auditCursor = null;
      auditLastDay = null;
      $("auditEmpty").classList.add("hidden");
    }
    const params = new URLSearchParams();
    const actionFilter = $("auditAction").value;
    if (actionFilter) params.set("action", actionFilter);
    if (auditCursor) {
      params.set("cursor_occurred_at", auditCursor.occurred_at);
      params.set("cursor_id", auditCursor.id);
    }
    let res;
    try {
      res = await authed("/api/v1/audit-logs?" + params.toString());
    } catch {
      return;
    }
    if (res.status === 403) {
      $("auditEmpty").textContent = "Not permitted to view the audit trail.";
      $("auditEmpty").classList.remove("hidden");
      $("auditMoreBtn").classList.add("hidden");
      return;
    }
    if (!res.ok) {
      renderInlineState("auditMsg", "Audit trail unavailable (" + res.status + ").", "err");
      $("auditMsg").classList.remove("hidden");
      return;
    }
    const page = await res.json();
    for (const row of page.items) {
      const dayLabel = auditDayLabel(row.occurred_at);
      if (dayLabel !== auditLastDay) {
        $("auditHits").appendChild(renderAuditDayHeader(dayLabel));
        auditLastDay = dayLabel;
      }
      $("auditHits").appendChild(renderAuditRow(row));
    }
    auditCursor = page.next_cursor;
    $("auditMoreBtn").classList.toggle("hidden", !page.next_cursor);
    $("auditEmpty").classList.toggle("hidden", !(fresh && page.items.length === 0));
  } finally {
    setPanelState("auditPanel", "");
    button.disabled = false;
  }
}

function renderSource(pages) {
  ocrLines = [];
  const source = $("source");
  source.innerHTML = "";
  for (const pg of pages) {
    for (const line of pg.lines || []) {
      const div = document.createElement("div");
      div.className = "ln";
      div.dataset.page = pg.page;
      div.dataset.text = line.text || "";
      div.textContent = "p" + pg.page + "  " + (line.text || "");
      source.appendChild(div);
      ocrLines.push(div);
    }
  }
}

function renderFieldValue(wrapper, field) {
  wrapper.innerHTML = "";
  const hasRaw = !!(field.value && String(field.value).trim());
  const hasTyped = !!(field.typed_value && String(field.typed_value).trim());
  const value = document.createElement("div");
  value.className = "fv";
  if (hasTyped) {
    value.textContent = field.typed_value;
  } else if (field.source === "skipped" && hasRaw) {
    value.textContent = "(not entered)";
    value.classList.add("skipped-text");
  } else {
    value.textContent = "-";
  }
  wrapper.appendChild(value);
  if (hasRaw && field.value !== field.typed_value) {
    const raw = document.createElement("div");
    raw.className = "fv-raw";
    raw.textContent = "on page: " + field.value;
    wrapper.appendChild(raw);
  }
}

function startEdit(key, wrapper, editButton) {
  const field = review.fields[key];
  wrapper.innerHTML = "";
  const inputRow = document.createElement("div");
  inputRow.className = "fv-input-row";
  inputRow.addEventListener("click", (event) => event.stopPropagation());
  const input = document.createElement("input");
  input.type = "text";
  input.value = field.typed_value || "";
  input.maxLength = 255;
  const saveButton = document.createElement("button");
  saveButton.type = "button";
  saveButton.textContent = "Save";
  const cancelButton = document.createElement("button");
  cancelButton.type = "button";
  cancelButton.textContent = "Cancel";
  cancelButton.className = "button-inline";
  inputRow.append(input, saveButton, cancelButton);
  const error = document.createElement("div");
  error.className = "msg err hidden";
  wrapper.append(inputRow, error);
  editButton.classList.add("hidden");
  input.focus();
  input.select();

  const cancel = () => {
    renderFieldValue(wrapper, field);
    editButton.classList.remove("hidden");
  };

  cancelButton.addEventListener("click", cancel);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") cancel();
    if (event.key === "Enter") {
      event.preventDefault();
      save();
    }
  });
  saveButton.addEventListener("click", save);

  async function save() {
    saveButton.disabled = true;
    cancelButton.disabled = true;
    error.classList.add("hidden");
    let res;
    try {
      res = await authed("/api/v1/documents/" + review.id + "/extraction/fields/" + key, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ typed_value: input.value.trim() || null }),
      });
    } catch {
      return;
    }
    if (!res.ok) {
      let message = "Save failed.";
      try {
        message = (await res.json()).message || message;
      } catch {
        /* keep default */
      }
      error.textContent = message;
      error.classList.remove("hidden");
      saveButton.disabled = false;
      cancelButton.disabled = false;
      return;
    }
    review.fields = flattenFields((await res.json()).fields);
    editButton.closest(".frow").replaceWith(renderRow(key, review.fields[key]));
  }
}

function rowHasValue(field) {
  const hasRaw = !!(field.value && String(field.value).trim());
  const hasTyped = !!(field.typed_value && String(field.typed_value).trim());
  return hasRaw || hasTyped;
}

function renderRow(key, field) {
  const row = document.createElement("div");
  const hasRaw = !!(field.value && String(field.value).trim());
  const interactive = hasRaw;
  row.className = "frow" + (rowHasValue(field) ? "" : " null");
  const labelNode = document.createElement("div");
  labelNode.className = "fk";
  labelNode.textContent = label(key);
  const valueWrapper = document.createElement("div");
  valueWrapper.className = "fv-wrap";
  renderFieldValue(valueWrapper, field);
  const editButton = document.createElement("button");
  editButton.type = "button";
  editButton.className = "button-inline";
  editButton.textContent = "Edit";
  editButton.addEventListener("click", (event) => {
    event.stopPropagation();
    startEdit(key, valueWrapper, editButton);
  });
  const actions = document.createElement("div");
  actions.className = "frow-actions";
  actions.append(sourceBadge(field), editButton);
  row.append(labelNode, valueWrapper, actions);
  if (interactive) row.addEventListener("click", () => selectField(row, review.fields[key], key));
  return row;
}

// LLM digest of the crew narrative (EPCR only). Generated prose the
// hallucination guard cannot prove -- always labeled unverified.
function renderNarrativeSummary(summary) {
  const box = $("narrativeSummary");
  box.innerHTML = "";
  if (!summary) {
    box.classList.add("hidden");
    return;
  }
  const label = document.createElement("div");
  label.className = "eyebrow";
  label.textContent = "AI narrative summary — unverified";
  const body = document.createElement("div");
  body.className = "narrative-summary-body";
  body.textContent = summary;
  box.appendChild(label);
  box.appendChild(body);
  box.classList.remove("hidden");
}

// Advisory SOP flags computed server-side (suggested_flags on the extraction).
// Codes and field references only -- never document values.
function renderFlags(flags) {
  const box = $("flagChips");
  box.innerHTML = "";
  if (!flags || !flags.length) {
    box.classList.add("hidden");
    return;
  }
  for (const flag of flags) {
    const chip = document.createElement("span");
    chip.className = "chip rule";
    chip.textContent = flag.code.replace(/_/g, " ");
    chip.title = flag.detail;
    box.appendChild(chip);
  }
  box.classList.remove("hidden");
}

// A document can only be matched against others if it carries a run_number,
// or a full last+first+DOB triple -- same requirement as
// DocumentRepository.find_related. Checked client-side so a doc with neither
// skips the /related call entirely instead of showing an ambiguous "no
// related documents".
function hasIdentity(doc) {
  return !!doc.run_number || !!(doc.patient_last_name && doc.patient_first_name && doc.patient_dob);
}

const MISMATCH_FIELD_LABELS = {
  patient_last_name: "Last name",
  patient_first_name: "First name",
  patient_dob: "Date of birth",
  run_number: "Run number",
};

// Same encounter, other documents (app.js's counterpart to renderHit): other
// COMPLETED documents sharing this one's run_number, or its name+DOB, plus
// any identity field that disagrees between them. Computed at read time by
// GET /documents/{id}/related -- see PatientGroupingService.
function renderRelated(data) {
  const box = $("relatedPanel");
  box.innerHTML = "";

  const label = document.createElement("div");
  label.className = "eyebrow";
  label.textContent = "Related documents";
  box.appendChild(label);

  if (data.mismatches && data.mismatches.length) {
    const warn = document.createElement("div");
    warn.className = "mismatch-box";
    for (const m of data.mismatches) {
      const row = document.createElement("div");
      row.textContent =
        (MISMATCH_FIELD_LABELS[m.field] || m.field) +
        " disagrees across related documents: " +
        m.values.join(", ");
      warn.appendChild(row);
    }
    box.appendChild(warn);
  }

  if (!data.related || !data.related.length) {
    const empty = document.createElement("p");
    empty.className = "sub";
    empty.textContent = "No related documents found.";
    box.appendChild(empty);
  } else {
    const list = document.createElement("ul");
    list.className = "hits related-hits";
    for (const doc of data.related) {
      list.appendChild(renderHit(doc));
    }
    box.appendChild(list);
  }

  box.classList.remove("hidden");
}

function renderRelatedMessage(text) {
  const box = $("relatedPanel");
  box.innerHTML = "";
  const label = document.createElement("div");
  label.className = "eyebrow";
  label.textContent = "Related documents";
  const msg = document.createElement("p");
  msg.className = "sub";
  msg.textContent = text;
  box.append(label, msg);
  box.classList.remove("hidden");
}

function renderFields(fields) {
  const box = $("fields");
  box.innerHTML = "";
  const empty = [];
  let lastSlot = null;
  for (const [key, field] of Object.entries(fields)) {
    if (!rowHasValue(field)) {
      empty.push([key, field]);
      continue;
    }
    if (key.startsWith("insurance.")) {
      const slot = key.split(".")[1];
      if (slot !== lastSlot) {
        const heading = document.createElement("div");
        heading.className = "fgroup";
        heading.textContent = "Insurance " + (Number(slot) + 1);
        box.appendChild(heading);
        lastSlot = slot;
      }
    }
    box.appendChild(renderRow(key, field));
  }
  if (empty.length) {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "button-inline fields-empty-toggle";
    toggle.textContent = "Show " + empty.length + " empty field" + (empty.length === 1 ? "" : "s");
    let shown = false;
    toggle.addEventListener("click", () => {
      shown = !shown;
      toggle.textContent = shown
        ? "Hide " + empty.length + " empty field" + (empty.length === 1 ? "" : "s")
        : "Show " + empty.length + " empty field" + (empty.length === 1 ? "" : "s");
      box.querySelectorAll(".frow.null").forEach((row) => row.classList.toggle("hidden", !shown));
    });
    box.appendChild(toggle);
    for (const [key, field] of empty) {
      const row = renderRow(key, field);
      row.classList.add("hidden");
      box.appendChild(row);
    }
  }
}

function boxRect(bbox, natural) {
  const xs = bbox.map((p) => p[0]);
  const ys = bbox.map((p) => p[1]);
  const minx = Math.min(...xs);
  const miny = Math.min(...ys);
  const maxx = Math.max(...xs);
  const maxy = Math.max(...ys);
  return {
    left: (minx / natural.w) * 100,
    top: (miny / natural.h) * 100,
    width: ((maxx - minx) / natural.w) * 100,
    height: ((maxy - miny) / natural.h) * 100,
  };
}

function drawBoxesInto(layer, page, { scroll } = {}) {
  layer.innerHTML = "";
  const natural = review.natural[page];
  if (!natural || !natural.w || !natural.h) return;
  for (const [key, field] of Object.entries(review.fields)) {
    if (field.source_page !== page || !field.source_bbox) continue;
    const rect = boxRect(field.source_bbox, natural);
    const box = document.createElement("div");
    box.className = "bbox" + (key === review.selKey ? " sel" : "");
    box.style.left = rect.left + "%";
    box.style.top = rect.top + "%";
    box.style.width = rect.width + "%";
    box.style.height = rect.height + "%";
    layer.appendChild(box);
    if (scroll && key === review.selKey) box.scrollIntoView({ block: "center" });
  }
}

function drawBoxes(page) {
  drawBoxesInto($("boxLayer"), page, { scroll: true });
  if (isLightboxOpen() && page === review.page) drawBoxesInto($("lightboxBoxes"), page);
}

function isLightboxOpen() {
  return !$("pageLightbox").classList.contains("hidden");
}

function openLightbox() {
  const pageImg = $("pageImg");
  if (pageImg.classList.contains("hidden") || !pageImg.src) return;
  $("lightboxImg").src = pageImg.src;
  drawBoxesInto($("lightboxBoxes"), review.page);
  $("pageLightbox").classList.remove("hidden");
  $("lightboxCloseBtn").focus();
  document.body.classList.add("lightbox-open");
}

function closeLightbox() {
  $("pageLightbox").classList.add("hidden");
  document.body.classList.remove("lightbox-open");
  $("pageExpandBtn").focus();
}

function findBestMatchingLine(field) {
  if (!field.value) return null;
  const needle = String(field.value).trim().toLowerCase();
  if (!needle) return null;
  return ocrLines.find((line) => {
    const samePage = !field.source_page || String(line.dataset.page) === String(field.source_page);
    return samePage && line.dataset.text.toLowerCase().includes(needle);
  });
}

function selectField(row, field, key) {
  document.querySelectorAll(".frow.sel").forEach((node) => node.classList.remove("sel"));
  row.classList.add("sel");
  review.selKey = key;
  $("srcHint").textContent = field.source_page ? ". page " + field.source_page : "";
  ocrLines.forEach((line) => line.classList.remove("hit"));
  const bestLine = findBestMatchingLine(field);
  if (bestLine) {
    bestLine.classList.add("hit");
    bestLine.scrollIntoView({ block: "nearest" });
  }
  if (field.source_page && field.source_page !== review.page) loadPage(field.source_page);
  else drawBoxes(review.page);
}

async function loadPage(page) {
  review.page = page;
  updatePageToolbar();
  const image = $("pageImg");
  const msg = $("pageMsg");
  msg.classList.add("hidden");
  if (review.cache[page]) {
    image.src = review.cache[page];
    image.classList.remove("hidden");
    if (review.natural[page]) drawBoxes(page);
    return;
  }
  setPanelState("review", "loading");
  let res;
  try {
    res = await authed("/api/v1/documents/" + review.id + "/page/" + page);
  } catch {
    return;
  } finally {
    setPanelState("review", "");
  }
  if (!res.ok) {
    image.src = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
    image.classList.add("hidden");
    $("boxLayer").innerHTML = "";
    msg.textContent = "Page image unavailable (" + res.status + ").";
    msg.classList.remove("hidden");
    return;
  }
  const url = URL.createObjectURL(await res.blob());
  review.cache[page] = url;
  image.src = url;
  image.classList.remove("hidden");
}

async function openDoc(doc) {
  const name =
    [doc.patient_last_name, doc.patient_first_name].filter(Boolean).join(", ") ||
    doc.doc_type ||
    "document";
  clearPageCache();
  if (reprocessPollTimer) {
    clearTimeout(reprocessPollTimer);
    reprocessPollTimer = null;
  }
  review = {
    id: doc.id,
    status: doc.status,
    fields: {},
    page: 1,
    maxPage: 1,
    selKey: null,
    natural: {},
    cache: {},
  };
  setReviewNavEnabled(true);
  updateReprocessBtn();
  updateMarkReviewedBtn();
  $("reviewName").textContent = name;
  $("review").classList.remove("hidden");
  $("reviewSection").scrollIntoView({ behavior: "smooth", block: "start" });
  $("fields").innerHTML = "";
  $("source").innerHTML = "";
  resetPageImage();
  $("srcHint").textContent = "";
  $("fieldsMsg").classList.add("hidden");
  $("relatedPanel").innerHTML = "";
  $("relatedPanel").classList.add("hidden");
  ocrLines = [];
  setPanelState("review", "loading");
  try {
    try {
      const ocrRes = await authed("/api/v1/documents/" + doc.id + "/ocr");
      if (ocrRes.ok) {
        const ocr = await ocrRes.json();
        renderSource(ocr.page_texts || []);
        review.maxPage = Math.max(1, ocr.page_count || (ocr.page_texts || []).length || 1);
      }
    } catch {
      return;
    }

    let extractionRes;
    try {
      extractionRes = await authed("/api/v1/documents/" + doc.id + "/extraction");
    } catch {
      return;
    }
    if (extractionRes.status === 404) {
      const msg = $("fieldsMsg");
      msg.textContent = "No extraction yet. Document status is " + (doc.status || "pending") + ".";
      msg.classList.remove("hidden");
      renderFlags([]);
      renderNarrativeSummary(null);
    } else if (extractionRes.ok) {
      const extraction = await extractionRes.json();
      review.fields = flattenFields(extraction.fields);
      renderFields(review.fields);
      renderFlags(extraction.suggested_flags);
      renderNarrativeSummary(extraction.narrative_summary);
    } else {
      const msg = $("fieldsMsg");
      msg.textContent = "Extraction unavailable (" + extractionRes.status + ").";
      msg.classList.remove("hidden");
    }

    if (!hasIdentity(doc)) {
      renderRelatedMessage("Not enough identifying data extracted to search for related documents.");
    } else {
      try {
        const relatedRes = await authed("/api/v1/documents/" + doc.id + "/related");
        if (relatedRes.ok) {
          renderRelated(await relatedRes.json());
        }
      } catch {
        // Non-fatal: the related panel just stays hidden.
      }
    }

    updatePageToolbar();
    await loadPage(1);
  } finally {
    setPanelState("review", "");
  }
}

function openDocDirect(doc) {
  queue = [];
  queueIdx = 0;
  updateQueueBar();
  openDoc(doc);
}

async function pollReprocess(documentId) {
  let res;
  try {
    res = await authed("/api/v1/documents/" + documentId);
  } catch {
    return;
  }
  if (!res.ok) return;
  const doc = await res.json();
  if (review.id !== documentId) return;
  const terminal = doc.status === "completed" || doc.status === "failed" || doc.status === "extraction_failed";
  if (!terminal) {
    reprocessPollTimer = setTimeout(() => pollReprocess(documentId), 2000);
    return;
  }
  review.status = doc.status;
  updateReprocessBtn();
  const msg = $("reprocessMsg");
  msg.classList.remove("hidden");
  if (doc.status === "failed" || doc.status === "extraction_failed") {
    setMsgTone(msg, "err");
    msg.textContent =
      doc.status === "extraction_failed"
        ? "Reprocessing finished: still nothing to extract."
        : "Reprocessing failed: " + (doc.failure_reason || "no reason recorded.");
    return;
  }
  setMsgTone(msg, "ok");
  msg.textContent = "Reprocessing complete.";
  let fres;
  try {
    fres = await authed("/api/v1/documents/" + documentId + "/extraction");
  } catch {
    return;
  }
  if (fres.ok) {
    const extraction = await fres.json();
    review.fields = flattenFields(extraction.fields);
    renderFields(review.fields);
    renderFlags(extraction.suggested_flags);
    renderNarrativeSummary(extraction.narrative_summary);
  }
  try {
    const ores = await authed("/api/v1/documents/" + documentId + "/ocr");
    if (ores.ok) {
      const ocr = await ores.json();
      renderSource(ocr.page_texts || []);
      review.maxPage = Math.max(1, ocr.page_count || (ocr.page_texts || []).length || 1);
      updatePageToolbar();
      drawBoxes(review.page);
    }
  } catch {
    /* ignore post-refresh source failure */
  }
}

function renderPipelineRow(item, checkbox) {
  const row = document.createElement("div");
  row.className = "pipeline-row";
  if (checkbox) {
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "pipeline-check";
    cb.value = item.id;
    cb.addEventListener("change", updateBulkReprocessBtn);
    row.appendChild(cb);
  }
  const main = document.createElement("div");
  main.className = "pipeline-row-main pipeline-row-clickable";
  main.addEventListener("click", () => {
    // The pipeline view is its own tab, distinct from Review -- switchTab
    // makes #reviewSection itself visible (openDoc only un-hides the inner
    // #review panel), so the opened document is actually seen, not just loaded.
    switchTab("reviewSection");
    openDocDirect(item);
  });
  const st = document.createElement("span");
  st.className = "st " + item.status;
  st.textContent = item.status;
  const meta = document.createElement("span");
  meta.className = "sub";
  meta.textContent = item.uploader_email + "  ·  " + new Date(item.created_at).toLocaleString();
  main.append(st, meta);
  if (item.failure_reason) {
    const reason = document.createElement("div");
    reason.className = "sub pipeline-reason";
    reason.textContent = item.failure_reason;
    main.appendChild(reason);
  }
  row.appendChild(main);
  return row;
}

function updateBulkReprocessBtn() {
  const checked = document.querySelectorAll(".pipeline-check:checked");
  $("bulkReprocessBtn").disabled = checked.length === 0;
}

function renderPipelineSection(listId, emptyId, items, checkbox) {
  const box = $(listId);
  box.innerHTML = "";
  for (const item of items) box.appendChild(renderPipelineRow(item, checkbox));
  $(emptyId).classList.toggle("hidden", items.length > 0);
}

async function pollPipeline() {
  if (!isAdmin) return;
  if (pipelineTimer) {
    clearTimeout(pipelineTimer);
    pipelineTimer = null;
  }
  let res;
  try {
    res = await authed("/api/v1/admin/pipeline");
  } catch {
    return;
  }
  if (res.ok) {
    const data = await res.json();
    const inFlightCount = data.in_flight.length;
    const failedCount = data.interrupted.length + data.failed.length;
    $("pipelineSummary").textContent =
      inFlightCount +
      " in flight  ·  " +
      data.interrupted.length +
      " interrupted by restart  ·  " +
      data.failed.length +
      " other failures  ·  " +
      data.extraction_failed.length +
      " extraction incomplete";
    const badgeCount = inFlightCount + failedCount + data.extraction_failed.length;
    $("pipelineNavBadge").textContent = String(badgeCount);
    $("pipelineNavBadge").classList.toggle("hidden", badgeCount === 0);
    renderPipelineSection("pipelineInFlight", "pipelineInFlightEmpty", data.in_flight, false);
    renderPipelineSection("pipelineInterrupted", "pipelineInterruptedEmpty", data.interrupted, true);
    renderPipelineSection("pipelineFailed", "pipelineFailedEmpty", data.failed, true);
    renderPipelineSection(
      "pipelineExtractionFailed",
      "pipelineExtractionFailedEmpty",
      data.extraction_failed,
      true
    );
    updateBulkReprocessBtn();
  }
  pipelineTimer = setTimeout(pollPipeline, 7000);
}

function renderClaimDocs(members) {
  const container = $("claimDocsList");
  container.innerHTML = "";
  members.forEach((m) => {
    const row = document.createElement("div");
    row.className = "pipeline-row";
    row.textContent = "Document " + m.document_id.slice(0, 8) + "  ·  position " + m.position + "  ·  " + m.status;
    container.appendChild(row);
  });
}

async function dismissClaimTag(tagName) {
  try {
    await authed("/api/v1/claims/" + claimId + "/tags/" + encodeURIComponent(tagName), {
      method: "DELETE",
    });
  } catch {
    return;
  }
  loadClaimTags(claimId);
}

function renderClaimTags(data) {
  const chips = $("claimTagChips");
  chips.innerHTML = "";
  data.assigned.forEach((tag) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.title = tag.reason + (tag.rule_id ? "\n\nRule: " + tag.rule_id : "");
    chip.append(document.createTextNode(tag.tag_name + " " + tag.confidence.toFixed(2)));
    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "chip-dismiss";
    dismiss.setAttribute("aria-label", "Dismiss " + tag.tag_name);
    dismiss.textContent = "×";
    dismiss.addEventListener("click", (event) => {
      event.stopPropagation();
      dismissClaimTag(tag.tag_name);
    });
    chip.appendChild(dismiss);
    chips.appendChild(chip);
  });
  $("claimTagsEmpty").classList.toggle("hidden", data.assigned.length > 0);

  const manual = $("claimManualTags");
  manual.innerHTML = "";
  data.manual_review.forEach((tag) => {
    const row = document.createElement("div");
    row.className = "pipeline-row";
    row.title = tag.reason;
    row.textContent = tag.tag_name;
    manual.appendChild(row);
  });
}

function renderClaimHit(claim) {
  const name =
    [claim.patient_last_name, claim.patient_first_name].filter(Boolean).join(", ") ||
    "(no name extracted)";

  const row = document.createElement("tr");
  row.tabIndex = 0;
  row.setAttribute("role", "button");
  row.setAttribute("aria-label", "Open claim for " + name);

  const idCell = document.createElement("td");
  idCell.textContent = claim.id;

  const nameCell = document.createElement("td");
  nameCell.textContent = name;

  const statusCell = document.createElement("td");
  statusCell.innerHTML = '<span class="st ' + claim.status + '">' + claim.status + "</span>";

  const runCell = document.createElement("td");
  runCell.textContent = claim.run_number || "-";

  const dobCell = document.createElement("td");
  dobCell.textContent = claim.patient_dob || "-";

  const createdCell = document.createElement("td");
  createdCell.textContent = new Date(claim.created_at).toLocaleDateString();

  row.append(idCell, nameCell, statusCell, runCell, dobCell, createdCell);

  const select = () => {
    claimId = claim.id;
    if (claimPollTimer) clearTimeout(claimPollTimer);
    pollClaim(claim.id);
  };
  row.addEventListener("click", select);
  row.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      select();
    }
  });
  return row;
}

function clearClaimSearchMessages() {
  $("claimSearchMsg").classList.add("hidden");
  $("claimSearchMsg").textContent = "";
}

function renderClaimSearchMessage(text, tone) {
  const msg = $("claimSearchMsg");
  setMsgTone(msg, tone);
  msg.textContent = text;
  msg.classList.remove("hidden");
}

async function runClaimSearch(fresh) {
  const button = fresh ? $("claimSearchBtn") : $("claimMoreBtn");
  button.disabled = true;
  clearClaimSearchMessages();
  try {
    const params = new URLSearchParams(claimSearchParams);
    if (claimSearchCursor) {
      params.set("cursor_created_at", claimSearchCursor.created_at);
      params.set("cursor_id", claimSearchCursor.id);
    }
    let res;
    try {
      res = await authed("/api/v1/claims?" + params.toString());
    } catch {
      return;
    }
    if (!res.ok) {
      let message = "Search failed (" + res.status + ").";
      try {
        message = (await res.json()).message || message;
      } catch {
        /* keep default */
      }
      renderClaimSearchMessage(message, "err");
      return;
    }
    const page = await res.json();
    if (fresh) $("claimHits").innerHTML = "";
    for (const claim of page.items) {
      $("claimHits").appendChild(renderClaimHit(claim));
    }
    claimSearchCursor = page.next_cursor;
    $("claimMoreBtn").classList.toggle("hidden", !page.next_cursor);
    $("claimHitsEmpty").classList.toggle("hidden", !(fresh && page.items.length === 0));
    if (fresh && page.items.length === 0) {
      renderClaimSearchMessage("No claims match the current filters.", "info");
    }
  } finally {
    button.disabled = false;
  }
}

async function loadClaimTags(id) {
  let res;
  try {
    res = await authed("/api/v1/claims/" + id + "/tags");
  } catch {
    return;
  }
  if (!res.ok) return;
  renderClaimTags(await res.json());
}

async function loadClaimDetail(id) {
  let res;
  try {
    res = await authed("/api/v1/claims/" + id);
  } catch {
    return null;
  }
  if (!res.ok) {
    let message = "Could not open claim (" + res.status + ").";
    try {
      message = (await res.json()).message || message;
    } catch {
      /* keep default */
    }
    renderInlineState("claimUploadMsg", message, "err");
    return null;
  }
  const claim = await res.json();
  $("claimDetailPanel").classList.remove("hidden");
  $("claimStatusTitle").textContent = "Claim " + id.slice(0, 8) + " — " + claim.status;
  renderClaimDocs(claim.members);
  $("claimReprocessBtn").classList.toggle("hidden", !_CLAIM_TERMINAL_STATUSES.includes(claim.status));
  $("claimReprocessMsg").classList.add("hidden");
  return claim;
}

const _CLAIM_TERMINAL_STATUSES = ["completed", "partial", "failed"];

async function reprocessClaim(id, force) {
  const btn = $("claimReprocessBtn");
  const msg = $("claimReprocessMsg");
  btn.disabled = true;
  setMsgTone(msg, "info");
  msg.textContent = "Reprocessing...";
  msg.classList.remove("hidden");

  let res;
  try {
    res = await authed(
      "/api/v1/claims/" + id + "/reprocess" + (force ? "?force=true" : ""),
      { method: "POST" }
    );
  } catch {
    btn.disabled = false;
    msg.classList.add("hidden");
    return;
  }

  if (res.status === 409 && !force) {
    // The server, not this client, decides whether a member document has a
    // manual correction reprocessing would discard -- its message already
    // names the count. Defer to it rather than guessing client-side.
    let message = "Reprocessing would discard manual corrections on this claim.";
    try {
      message = (await res.json()).message || message;
    } catch {
      /* keep default */
    }
    btn.disabled = false;
    msg.classList.add("hidden");
    if (confirm(message + " Continue anyway?")) {
      await reprocessClaim(id, true);
    }
    return;
  }

  if (!res.ok) {
    let message = "Reprocess failed.";
    try {
      message = (await res.json()).message || message;
    } catch {
      /* keep default */
    }
    setMsgTone(msg, "err");
    msg.textContent = message;
    btn.disabled = false;
    return;
  }

  btn.classList.add("hidden");
  msg.classList.add("hidden");
  if (claimPollTimer) clearTimeout(claimPollTimer);
  pollClaim(id);
}

async function pollClaim(id) {
  const claim = await loadClaimDetail(id);
  if (!claim) return;
  if (_CLAIM_TERMINAL_STATUSES.includes(claim.status)) {
    await loadClaimTags(id);
    return;
  }
  claimPollTimer = setTimeout(() => pollClaim(id), 2000);
}

function wireClaimEvents() {
  $("claimUploadForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const files = $("claimFiles").files;
    if (!files.length) return;
    const button = $("claimUploadBtn");
    button.disabled = true;
    renderInlineState("claimUploadMsg", "Uploading " + files.length + " file(s)...", "info");
    const body = new FormData();
    Array.from(files).forEach((f) => body.append("files", f));
    let res;
    try {
      res = await authed("/api/v1/claims", { method: "POST", body });
    } catch {
      button.disabled = false;
      return;
    }
    button.disabled = false;
    if (!res.ok) {
      let message = "Upload failed.";
      try {
        message = (await res.json()).message || message;
      } catch {
        /* keep default */
      }
      renderInlineState("claimUploadMsg", message, "err");
      return;
    }
    const result = await res.json();
    claimId = result.claim_id;
    let msg = "Claim created: " + result.accepted.length + " file(s) accepted";
    if (result.rejected.length) msg += ", " + result.rejected.length + " rejected";
    renderInlineState("claimUploadMsg", msg + ".", result.rejected.length ? "err" : "ok");
    if (claimPollTimer) clearTimeout(claimPollTimer);
    pollClaim(claimId);
  });

  $("claimLookupBtn").addEventListener("click", () => {
    const id = $("claimLookupId").value.trim();
    if (!id) return;
    claimId = id;
    if (claimPollTimer) clearTimeout(claimPollTimer);
    pollClaim(id);
  });

  $("claimSearchForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const params = new URLSearchParams();
    const name = $("cqName").value.trim();
    const run = $("cqRun").value.trim();
    const dob = $("cqDob").value;
    if (name) params.set("patient_name", name);
    if (run) params.set("run_number", run);
    if (dob) {
      params.set("dob_from", dob);
      params.set("dob_to", dob);
    }
    params.set("status", $("cqStatus").value);
    claimSearchParams = params.toString();
    claimSearchCursor = null;
    await runClaimSearch(true);
  });

  $("claimMoreBtn").addEventListener("click", () => runClaimSearch(false));

  $("claimReprocessBtn").addEventListener("click", () => {
    if (!claimId) return;
    reprocessClaim(claimId, false);
  });
}

function syncThemeToggle() {
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  const toggle = $("themeToggle");
  toggle.setAttribute("aria-pressed", String(dark));
  toggle.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
  toggle.querySelector(".theme-sun").classList.toggle("hidden", dark);
  toggle.querySelector(".theme-moon").classList.toggle("hidden", !dark);
}

function wireEvents() {
  $("logoutBtn").addEventListener("click", () => logout("Signed out."));
  wireClaimEvents();

  $("bulkReprocessBtn").addEventListener("click", async () => {
    const ids = Array.from(document.querySelectorAll(".pipeline-check:checked")).map(
      (cb) => cb.value
    );
    if (!ids.length) return;
    if (!confirm("Reprocess " + ids.length + " document(s)? This re-runs OCR and extraction.")) {
      return;
    }
    const button = $("bulkReprocessBtn");
    const msg = $("bulkReprocessMsg");
    button.disabled = true;
    setMsgTone(msg, "info");
    msg.textContent = "Reprocessing...";
    msg.classList.remove("hidden");
    let res;
    try {
      res = await authed("/api/v1/admin/reprocess-failed", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ document_ids: ids }),
      });
    } catch {
      return;
    }
    if (!res.ok) {
      let message = "Bulk reprocess failed.";
      try {
        message = (await res.json()).message || message;
      } catch {
        /* keep default */
      }
      setMsgTone(msg, "err");
      msg.textContent = message;
      button.disabled = false;
      return;
    }
    const result = await res.json();
    setMsgTone(msg, "ok");
    msg.textContent =
      "Reprocessing " +
      result.reprocessed.length +
      " document(s)" +
      (result.skipped.length
        ? "; " + result.skipped.length + " already in flight, skipped."
        : ".");
    pollPipeline();
  });

  $("themeToggle").addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
    syncThemeToggle();
  });

  $("passwordToggle").addEventListener("click", () => {
    const input = $("password");
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    const toggle = $("passwordToggle");
    toggle.setAttribute("aria-pressed", String(show));
    toggle.setAttribute("aria-label", show ? "Hide password" : "Show password");
    toggle.querySelector(".eye-open").classList.toggle("hidden", show);
    toggle.querySelector(".eye-closed").classList.toggle("hidden", !show);
    input.focus();
  });

  $("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("loginBtn");
    button.disabled = true;
    renderInlineState("loginMsg", "Signing in...", "info");
    try {
      const ok = await login($("email").value.trim(), $("password").value);
      if (!ok) {
        renderInlineState("loginMsg", "Login failed.", "err");
        return;
      }
      $("who").textContent = $("email").value.trim();
      $("login").classList.add("hidden");
      $("app").classList.remove("hidden");
      $("logoutBtn").classList.remove("hidden");
      $("mainNav").classList.remove("hidden");
      switchTab("uploadSection");
      $("password").value = "";
      renderInlineState("loginMsg", "", "");
      startSessionTimer();
      try {
        const meRes = await authed("/api/v1/auth/me");
        if (meRes.ok) {
          const me = await meRes.json();
          isAdmin = me.role === "admin";
          if (isAdmin) {
            $("pipelineNavLink").classList.remove("hidden");
            pollPipeline();
          }
        }
      } catch {
        /* role check is best-effort; the admin bar just stays hidden */
      }
    } catch {
      renderInlineState("loginMsg", "Network error.", "err");
    } finally {
      button.disabled = false;
    }
  });

  $("uploadForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = $("file").files[0];
    if (!file) return;
    const button = $("uploadBtn");
    button.disabled = true;
    renderInlineState("uploadMsg", "Uploading...", "info");
    resetPipelinePanel();
    setPanelState("pipelinePanel", "loading");
    $("loadingSkeleton").classList.remove("hidden");
    const fd = new FormData();
    fd.append("file", file);
    fd.append("doc_type", $("docType").value);
    try {
      const res = await authed("/api/v1/documents", { method: "POST", body: fd });
      if (!res.ok) {
        let message =
          res.status === 415
            ? "Unsupported file type (checked by magic bytes)."
            : res.status === 413
              ? "File too large (25 MB cap)."
              : "Upload rejected (" + res.status + ").";
        try {
          message = (await res.json()).message || message;
        } catch {
          /* keep default */
        }
        renderInlineState("uploadMsg", message, "err");
        return;
      }
      const doc = await res.json();
      renderInlineState(
        "uploadMsg",
        doc.deduplicated ? "Already on file. Showing existing document." : "Accepted. Processing...",
        "ok"
      );
      logLine("submitted -> " + doc.status, true);
      lastStatus = doc.status;
      paintPipe(doc.status);
      poll(doc.id);
    } catch {
      if (token) renderInlineState("uploadMsg", "Network error.", "err");
    } finally {
      button.disabled = false;
    }
  });

  $("reviewUploadBtn").addEventListener("click", () => {
    if (uploadedId) openDocDirect({ id: uploadedId, status: "completed" });
  });

  $("searchForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    searchParams = buildQuery().toString();
    searchCursor = null;
    $("hits").innerHTML = "";
    $("startQueueBtn").classList.add("hidden");
    await runSearch(true);
  });

  $("moreBtn").addEventListener("click", () => runSearch(false));

  $("startQueueBtn").addEventListener("click", () => {
    queue = hits
      .filter((doc) => doc.status === "completed")
      .sort((a, b) => (a.mean_confidence ?? 1) - (b.mean_confidence ?? 1));
    queueIdx = 0;
    if (!queue.length) return;
    updateQueueBar();
    openDoc(queue[queueIdx]);
  });

  $("queuePrevBtn").addEventListener("click", () => {
    if (queueIdx <= 0) return;
    queueIdx--;
    updateQueueBar();
    openDoc(queue[queueIdx]);
  });

  $("queueNextBtn").addEventListener("click", () => {
    if (queueIdx >= queue.length - 1) return;
    queueIdx++;
    updateQueueBar();
    openDoc(queue[queueIdx]);
  });

  $("reprocessBtn").addEventListener("click", async () => {
    // Reprocess re-runs OCR + LLM from scratch and replaces the Extraction
    // row outright -- any reviewer correction (source "corrected") is
    // discarded along with it. The server refuses this by default
    // (app/api/v1/documents.py::reprocess_document) regardless of what this
    // client thinks -- `force` is this dialog's confirmation of that refusal,
    // not the actual guard.
    const hasCorrections = Object.values(review.fields || {}).some(
      (field) => field && field.source === "corrected"
    );
    if (hasCorrections) {
      const proceed = confirm(
        "This document has manually corrected fields. Reprocessing re-runs OCR " +
          "and AI extraction from scratch and will discard those corrections. Continue?"
      );
      if (!proceed) return;
    }
    const button = $("reprocessBtn");
    const msg = $("reprocessMsg");
    button.disabled = true;
    setMsgTone(msg, "info");
    msg.textContent = "Reprocessing...";
    msg.classList.remove("hidden");
    let res;
    try {
      const url =
        "/api/v1/documents/" + review.id + "/reprocess" + (hasCorrections ? "?force=true" : "");
      res = await authed(url, { method: "POST" });
    } catch {
      return;
    }
    if (!res.ok) {
      let message = "Reprocess failed.";
      try {
        message = (await res.json()).message || message;
      } catch {
        /* keep default */
      }
      setMsgTone(msg, "err");
      msg.textContent = message;
      button.disabled = false;
      return;
    }
    review.status = "pending";
    button.classList.add("hidden");
    pollReprocess(review.id);
  });

  $("markReviewedBtn").addEventListener("click", async () => {
    const button = $("markReviewedBtn");
    const msg = $("markReviewedMsg");
    button.disabled = true;
    setMsgTone(msg, "info");
    msg.textContent = "Marking reviewed...";
    msg.classList.remove("hidden");
    let res;
    try {
      res = await authed("/api/v1/documents/" + review.id + "/mark-reviewed", { method: "POST" });
    } catch {
      return;
    }
    if (!res.ok) {
      let message = "Failed to mark reviewed.";
      try {
        message = (await res.json()).message || message;
      } catch {
        /* keep default */
      }
      setMsgTone(msg, "err");
      msg.textContent = message;
      button.disabled = false;
      return;
    }
    review.status = "completed";
    button.classList.add("hidden");
    updateReprocessBtn(); // status is now "completed" -- Reprocess becomes available
    setMsgTone(msg, "ok");
    msg.textContent = "Marked reviewed.";
    msg.classList.remove("hidden");
  });

  $("auditLoadBtn").addEventListener("click", () => loadAuditLogs(true));
  $("auditMoreBtn").addEventListener("click", () => loadAuditLogs(false));
  $("auditAction").addEventListener("change", () => loadAuditLogs(true));
  $("pagePrevBtn").addEventListener("click", () => {
    if (review.page > 1) loadPage(review.page - 1);
  });
  $("pageNextBtn").addEventListener("click", () => {
    if (review.page < review.maxPage) loadPage(review.page + 1);
  });

  $("pageImg").addEventListener("load", () => {
    const img = $("pageImg");
    review.natural[review.page] = { w: img.naturalWidth, h: img.naturalHeight };
    drawBoxes(review.page);
    if (isLightboxOpen()) $("lightboxImg").src = img.src;
  });

  $("pageImg").addEventListener("click", openLightbox);
  $("pageExpandBtn").addEventListener("click", openLightbox);
  $("lightboxCloseBtn").addEventListener("click", closeLightbox);
  $("pageLightbox").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeLightbox();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && isLightboxOpen()) closeLightbox();
  });
}

function init() {
  setupNav();
  setReviewNavEnabled(false);
  updatePageToolbar();
  wireEvents();
  syncThemeToggle();
  document.addEventListener("click", onUserActivity);
  document.addEventListener("keydown", onUserActivity);
}

init();
