/* Persephone Host Console — vanilla JS.
 *
 * Security / engineering rules honoured here:
 *   - No admin API key, no tokens, nothing sensitive in JS or storage. The
 *     session lives entirely in HttpOnly cookies set by the API; every request
 *     uses `credentials: "include"` so the browser attaches them automatically.
 *   - All attendee-supplied text is rendered with textContent / createElement.
 *     There is no innerHTML with dynamic data anywhere in this file.
 *   - Polling never overlaps and pauses while the tab is hidden.
 */
"use strict";

const API = "/api/v1";
const POLL_MS = 1500;
const REQUEST_TIMEOUT_MS = 12000;

const $ = (id) => document.getElementById(id);

// --------------------------------------------------------------------------
// Element handles (the DOM is static; the script is loaded at end of <body>).
// --------------------------------------------------------------------------
const loginView = $("login-view");
const appView = $("app-view");
const loginForm = $("login-form");
const emailInput = $("email");
const passwordInput = $("password");
const togglePw = $("toggle-password");
const loginSubmit = $("login-submit");
const loginError = $("login-error");
const btnLabel = loginSubmit.querySelector(".btn-label");
const spinner = loginSubmit.querySelector(".spinner");

const topbarEvent = $("topbar-event");
const onairPill = $("onair");
const onairText = onairPill.querySelector(".onair-text");
const workerPill = $("worker-pill");
const workerPillText = workerPill.lastElementChild;
const modePill = $("mode-pill");
const agoraWarning = $("agora-warning");
const updated = $("updated");

const profileEl = document.querySelector(".profile");
const profileBtn = $("profile-btn");
const profileMenu = $("profile-menu");
const profileEmail = $("profile-email");
const profileInitials = $("profile-initials");
const logoutBtn = $("logout-btn");

const mQuestions = $("m-questions");
const mAwaiting = $("m-awaiting");
const mTopics = $("m-topics");
const mLoudest = $("m-loudest");
const mWorker = $("m-worker");

const setupCard = $("setup-card");
const eventNameInput = $("event-name");
const createEventBtn = $("create-event");

const eventCard = $("event-card");
const eventTitle = $("event-title");
const joinCode = $("join-code");
const copyJoinBtn = $("copy-join");
const newEventNameInput = $("new-event-name");
const createAnotherBtn = $("create-another");

const roundCard = $("round-card");
const roundClosed = $("round-closed");
const roundOpen = $("round-open");
const roundPromptInput = $("round-prompt");
const openRoundBtn = $("open-round");
const roundCurrentPrompt = $("round-current-prompt");
const closeRoundBtn = $("close-round");
const reclusterBtn = $("recluster");

const questionsWrap = $("questions");
const questionsEmpty = $("questions-empty");
const questionsSkeleton = $("questions-skeleton");
const qCount = $("q-count");

const clustersWrap = $("clusters");
const clustersEmpty = $("clusters-empty");
const cCount = $("c-count");

const connStatus = $("conn-status");

const dialog = $("confirm-dialog");
const confirmTitle = $("confirm-title");
const confirmBody = $("confirm-body");
const confirmOk = $("confirm-ok");

const toastEl = $("toast");

// Voice output panel
const voiceCard = $("voice-card");
const voiceDot = $("voice-dot");
const voiceStatus = $("voice-status");
const voiceEnableBtn = $("voice-enable");
const voiceTestBtn = $("voice-test");
const voiceStopBtn = $("voice-stop");
const voiceAutoSpeak = $("voice-autospeak");
const voiceSelect = $("voice-select");
const voiceRate = $("voice-rate");
const voiceRateVal = $("voice-rate-val");
const voiceHint = $("voice-hint");

// --------------------------------------------------------------------------
// State
// --------------------------------------------------------------------------
let active = false; // console is shown and should be polling
let pollTimer = null;
let pollInFlight = false;
let pollAgain = false;
let currentOpenRoundId = null;
let lastAssistantResponses = []; // assistant responses from the latest poll
let baselinePrimed = false; // backlog marked historical once per session

// --------------------------------------------------------------------------
// API wrapper — single source of truth for network + error handling.
// --------------------------------------------------------------------------
class ApiError extends Error {
  constructor(message, status, kind) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind; // "http" | "unauthorized" | "network" | "timeout"
  }
}

async function api(path, { method = "GET", body = null, timeout = REQUEST_TIMEOUT_MS } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  let res;
  try {
    res = await fetch(`${API}${path}`, {
      method,
      credentials: "include",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : null,
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    if (err && err.name === "AbortError") {
      throw new ApiError("The request timed out.", 0, "timeout");
    }
    throw new ApiError("Can't reach the server.", 0, "network");
  }
  clearTimeout(timer);

  if (res.status === 401) {
    throw new ApiError("Your session has expired.", 401, "unauthorized");
  }

  let data = null;
  if ((res.headers.get("content-type") || "").includes("application/json")) {
    try {
      data = await res.json();
    } catch (_) {
      data = null;
    }
  }

  if (!res.ok) {
    let msg = `Request failed (${res.status}).`;
    if (data) {
      if (typeof data.detail === "string") msg = data.detail;
      else if (typeof data.message === "string") msg = data.message;
    }
    throw new ApiError(msg, res.status, "http");
  }
  return data;
}

// --------------------------------------------------------------------------
// Auth flow
// --------------------------------------------------------------------------
async function bootstrap() {
  try {
    const me = await api("/auth/me");
    if (me && me.authenticated) enterConsole(me.email);
    else showLogin();
  } catch (err) {
    showLogin();
    if (err.kind === "network") {
      showLoginError("Can't reach the server. Check your connection and try again.");
    }
  }
}

function showLogin(message) {
  stopPolling();
  resetVoiceSession(); // cancel speech + clear pending queue (prefs are kept)
  toggleProfile(false);
  appView.hidden = true;
  loginView.hidden = false;
  if (message) showLoginError(message);
  else clearLoginError();
  window.requestAnimationFrame(() => emailInput.focus());
}

function enterConsole(email) {
  loginView.hidden = true;
  appView.hidden = false;
  profileEmail.textContent = email || "";
  profileInitials.textContent = initials(email);
  questionsSkeleton.hidden = false;
  connStatus.textContent = "connecting…";
  baselinePrimed = false; // fresh session: the next poll re-establishes the baseline
  startPolling();
}

function handleSessionExpired() {
  showLogin("Your session expired. Please sign in again.");
}

async function onLogin(e) {
  e.preventDefault();
  const email = emailInput.value.trim();
  const password = passwordInput.value;
  clearLoginError();
  if (!email || !password) {
    showLoginError("Enter your email and password.");
    return;
  }
  setLoginLoading(true);
  try {
    const body = await api("/auth/login", { method: "POST", body: { email, password } });
    passwordInput.value = "";
    enterConsole(body.email);
  } catch (err) {
    if (err.status === 401) showLoginError("Incorrect email or password.");
    else if (err.status === 403) showLoginError("This account isn't authorized for host access.");
    else if (err.status === 503) showLoginError("Login is temporarily unavailable. Please try again shortly.");
    else if (err.kind === "network") showLoginError("Can't reach the server. Check your connection.");
    else if (err.kind === "timeout") showLoginError("The request timed out. Please try again.");
    else showLoginError(err.message || "Something went wrong. Please try again.");
  } finally {
    setLoginLoading(false);
  }
}

async function onLogout() {
  toggleProfile(false);
  try {
    await api("/auth/logout", { method: "POST" });
  } catch (_) {
    /* cookies are cleared server-side regardless; ignore network errors */
  }
  showLogin();
}

function setLoginLoading(on) {
  loginSubmit.disabled = on;
  spinner.hidden = !on;
  btnLabel.textContent = on ? "Signing in…" : "Sign in to host console";
}

function showLoginError(msg) {
  loginError.textContent = msg;
  loginError.hidden = false;
}

function clearLoginError() {
  loginError.hidden = true;
  loginError.textContent = "";
}

// --------------------------------------------------------------------------
// Polling loop — never overlaps, pauses while the tab is hidden.
// --------------------------------------------------------------------------
function startPolling() {
  active = true;
  clearInterval(pollTimer);
  poll();
  pollTimer = setInterval(poll, POLL_MS);
}

function stopPolling() {
  active = false;
  clearInterval(pollTimer);
  pollTimer = null;
}

async function poll() {
  if (!active || document.hidden) return;
  if (pollInFlight) {
    pollAgain = true;
    return;
  }
  pollInFlight = true;
  try {
    const state = await api("/admin/state", { timeout: 8000 });
    renderState(state);
    updated.textContent = clockNow();
    connStatus.textContent = "live · polling";
  } catch (err) {
    if (err.status === 401) {
      handleSessionExpired();
      return;
    }
    connStatus.textContent =
      err.kind === "network" || err.kind === "timeout"
        ? "reconnecting…"
        : `connection error: ${err.message}`;
  } finally {
    pollInFlight = false;
    if (pollAgain) {
      pollAgain = false;
      poll();
    }
  }
}

// --------------------------------------------------------------------------
// Rendering — textContent / createElement only.
// --------------------------------------------------------------------------
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function renderState(state) {
  const questions = state.questions || [];
  const clusters = state.clusters || [];
  const workers = state.workers || [];

  // --- Topbar ---
  topbarEvent.textContent = state.event ? state.event.name : "No active event";

  const onAir = !!state.open_round;
  onairPill.classList.toggle("on", onAir);
  onairPill.classList.toggle("off", !onAir);
  onairText.textContent = onAir ? "ON AIR" : "OFF AIR";

  if (state.worker_online) {
    workerPill.className = "status-pill ok";
    workerPillText.textContent = "worker live";
  } else if (workers.length) {
    workerPill.className = "status-pill bad";
    workerPillText.textContent = "worker offline";
  } else {
    workerPill.className = "status-pill";
    workerPillText.textContent = "no worker";
  }

  modePill.textContent = `mode ${(state.transcription_mode || "—").replace(/_/g, " ")}`;
  agoraWarning.hidden = !state.agora_mode_active;

  // --- Overview metrics (derived only from real state) ---
  mQuestions.textContent = String(questions.length);
  mAwaiting.textContent = String(
    questions.filter((q) => q.status === "done" && !q.answered_at).length
  );
  mTopics.textContent = String(clusters.length);
  mLoudest.textContent = String(
    clusters.reduce((max, c) => Math.max(max, c.question_count || 0), 0)
  );
  mWorker.textContent = state.worker_online ? "Online" : workers.length ? "Offline" : "—";

  // --- Control rail ---
  if (state.event) {
    setupCard.hidden = true;
    eventCard.hidden = false;
    roundCard.hidden = false;
    eventTitle.textContent = state.event.name;
    joinCode.textContent = state.event.join_code;
  } else {
    setupCard.hidden = false;
    eventCard.hidden = true;
    roundCard.hidden = true;
  }

  if (state.open_round) {
    currentOpenRoundId = state.open_round.id;
    roundClosed.hidden = true;
    roundOpen.hidden = false;
    roundCurrentPrompt.textContent = state.open_round.prompt || "No prompt — open floor.";
  } else {
    currentOpenRoundId = null;
    roundClosed.hidden = false;
    roundOpen.hidden = true;
  }
  reclusterBtn.disabled = !state.open_round;

  // --- Live questions ---
  questionsSkeleton.hidden = true;
  reconcile(questionsWrap, questions, (q) => q.id, questionSig, buildQuestionCard);
  qCount.textContent = String(questions.length);
  if (questions.length === 0) {
    questionsEmpty.hidden = false;
    if (state.open_round) {
      setEmpty(questionsEmpty, "Listening…", "Questions appear here the moment someone speaks into a badge.");
    } else if (state.event) {
      setEmpty(questionsEmpty, "No questions yet", "Open a round to start collecting questions from the room.");
    } else {
      setEmpty(questionsEmpty, "No event yet", "Create an event to hand out join codes and go live.");
    }
  } else {
    questionsEmpty.hidden = true;
  }

  // --- Clusters (already ranked by count from the API) ---
  reconcile(clustersWrap, clusters, (c) => c.id, clusterSig, buildClusterCard);
  cCount.textContent = String(clusters.length);
  if (clusters.length === 0) {
    clustersEmpty.hidden = false;
    setEmpty(clustersEmpty, "No topics yet", "As similar questions arrive, the room's themes surface here.");
  } else {
    clustersEmpty.hidden = true;
  }

  // --- Voice: auto-speak newly completed answers (exactly once) ---
  processVoice(collectAssistantResponses(questions));
}

// Flatten assistant answers from the questions, sorted by completion time so
// multiple new answers are spoken in a predictable order.
function collectAssistantResponses(questions) {
  const out = [];
  for (const q of questions) {
    const a = q.assistant_response;
    if (a) {
      out.push({
        id: a.id,
        status: a.status,
        response_text: a.response_text,
        completed_at: a.completed_at,
      });
    }
  }
  out.sort((x, y) => String(x.completed_at || "").localeCompare(String(y.completed_at || "")));
  return out;
}

/* Keyed list reconciliation: unchanged cards are left untouched (no flicker,
 * stable relative-time ticking), changed cards are rebuilt, gone cards removed,
 * and the DOM order is aligned to the API order. */
function reconcile(container, items, keyFn, sigFn, buildFn) {
  const existing = new Map();
  for (const node of Array.from(container.children)) existing.set(node.dataset.key, node);

  const desired = items.map((item) => {
    const key = String(keyFn(item));
    const sig = sigFn(item);
    const old = existing.get(key);
    existing.delete(key);
    if (old && old.dataset.sig === sig) return old;
    if (old) old.remove();
    const node = buildFn(item);
    node.dataset.key = key;
    node.dataset.sig = sig;
    return node;
  });

  for (const node of existing.values()) node.remove(); // stale keys

  for (let i = 0; i < desired.length; i++) {
    if (container.children[i] !== desired[i]) {
      container.insertBefore(desired[i], container.children[i] || null);
    }
  }
}

function questionSig(q) {
  const a = q.assistant_response;
  return [
    q.status,
    q.transcript || "",
    q.provider_used || "",
    q.fallback_used ? 1 : 0,
    q.processing_ms == null ? "" : q.processing_ms,
    q.similar_count == null ? "" : q.similar_count,
    q.safe_error_message || "",
    q.answered_at ? 1 : 0,
    // Assistant answer fields — a change here must rerender the card.
    a ? a.status : "",
    a ? a.response_text || "" : "",
    a ? a.provider || "" : "",
    a ? a.model || "" : "",
    a && a.processing_ms != null ? a.processing_ms : "",
  ].join("~");
}

function buildQuestionCard(q) {
  const card = el("article", `qcard st-${q.status}`);
  if (q.answered_at) card.classList.add("answered");

  const top = el("div", "qcard-top");
  top.appendChild(el("span", `tag tag-status st-${q.status}`, q.status));
  if (q.provider_used) top.appendChild(el("span", "tag tag-provider", providerLabel(q.provider_used)));
  if (q.fallback_used) top.appendChild(el("span", "tag tag-fallback", "fallback"));
  if (typeof q.processing_ms === "number") {
    top.appendChild(el("span", "tag", `${q.processing_ms} ms`));
  }
  if (q.similar_count && q.similar_count > 1) {
    top.appendChild(el("span", "tag tag-similar", `+${q.similar_count - 1} similar`));
  }
  card.appendChild(top);

  card.appendChild(questionText(q));

  // Voice-assistant answer (only meaningful once a question is transcribed).
  if (q.status === "done") card.appendChild(buildAssistantBlock(q));

  const foot = el("div", "qcard-foot");
  foot.appendChild(timeTag(q.created_at));
  if (q.answered_at) {
    foot.appendChild(el("span", "tag st-done", "answered"));
  } else if (q.status === "done") {
    foot.appendChild(
      answerButton(() => api(`/admin/questions/${q.id}/answered`, { method: "POST" }), "Marked answered")
    );
  } else {
    foot.appendChild(el("span")); // keep the flex spacing
  }
  card.appendChild(foot);
  return card;
}

function questionText(q) {
  if (q.status === "done") return el("p", "qtext", q.transcript || "(no words captured)");
  const pending = {
    queued: "Queued — waiting for the worker…",
    transcribing: "Transcribing…",
    empty: "No speech detected.",
    error: q.safe_error_message || "Transcription unavailable.",
  };
  return el("p", "qtext pending", pending[q.status] || "Processing…");
}

// Assistant answer sub-card. All text via textContent (answer is model output,
// still treated as untrusted). Buttons hit the host-only assistant endpoints.
function buildAssistantBlock(q) {
  const a = q.assistant_response;
  const wrap = el("div", "assistant");
  const head = el("div", "assistant-head");
  head.appendChild(el("span", "assistant-label", "Persephone answer"));
  if (a) head.appendChild(el("span", `tag tag-astatus st-${a.status}`, a.status));
  wrap.appendChild(head);

  if (!a) {
    wrap.appendChild(
      assistantButton(q.id, "generate", "Generate answer", "Generating answer…", "btn-ghost")
    );
    return wrap;
  }

  if (a.status === "done") {
    wrap.appendChild(el("p", "assistant-text", a.response_text || ""));
    const meta = el("div", "assistant-meta");
    meta.appendChild(el("span", "tag tag-provider", providerModelLabel(a)));
    if (typeof a.processing_ms === "number") {
      meta.appendChild(el("span", "tag", `${a.processing_ms} ms`));
    }
    wrap.appendChild(meta);
    const actions = el("div", "assistant-actions");
    actions.appendChild(replayButton(a));
    actions.appendChild(
      assistantButton(q.id, "regenerate", "Regenerate", "Regenerating…", "btn-ghost")
    );
    wrap.appendChild(actions);
  } else if (a.status === "error") {
    wrap.appendChild(
      el("p", "assistant-text pending", a.safe_error_message || "Answer generation failed.")
    );
    wrap.appendChild(assistantButton(q.id, "retry", "Retry answer", "Retrying…", "btn-ghost"));
  } else {
    // queued | generating
    const msg = a.status === "generating" ? "Persephone is thinking…" : "Queued for an answer…";
    wrap.appendChild(el("p", "assistant-text pending", msg));
  }
  return wrap;
}

function assistantButton(questionId, action, label, okMsg, variant) {
  const btn = el("button", `btn ${variant || "btn-ghost"} btn-sm`, label);
  btn.type = "button";
  btn.addEventListener("click", () =>
    act(btn, () => api(`/admin/questions/${questionId}/assistant/${action}`, { method: "POST" }), okMsg)
  );
  return btn;
}

function replayButton(a) {
  const btn = el("button", "btn btn-ghost btn-sm", "▶ Replay");
  btn.type = "button";
  btn.disabled = !speechSupported;
  btn.title = speechSupported ? "Speak this answer" : "Speech is unavailable in this browser";
  btn.addEventListener("click", () => replayAnswer(a));
  return btn;
}

function providerModelLabel(a) {
  const p = providerLabel(a.provider || "assistant");
  return a.model ? `${p} · ${a.model}` : p;
}

function clusterSig(c) {
  return [c.canonical_question, c.question_count, c.status].join("~");
}

function buildClusterCard(c) {
  const card = el("article", "ccard");
  if (c.status === "answered") card.classList.add("answered");

  const count = el("div", "ccard-count");
  count.appendChild(el("span", "n", String(c.question_count)));
  count.appendChild(el("span", "l metric-label", "asking"));
  card.appendChild(count);

  card.appendChild(el("p", "ccard-q", c.canonical_question));

  const foot = el("div", "ccard-foot");
  foot.appendChild(timeTag(c.created_at));
  if (c.status === "answered") {
    foot.appendChild(el("span", "tag st-done", "answered"));
  } else {
    foot.appendChild(
      answerButton(() => api(`/admin/clusters/${c.id}/answered`, { method: "POST" }), "Topic marked answered")
    );
  }
  card.appendChild(foot);
  return card;
}

function answerButton(request, okMsg) {
  const btn = el("button", "btn btn-ghost btn-sm", "Mark answered");
  btn.type = "button";
  btn.addEventListener("click", () => act(btn, request, okMsg));
  return btn;
}

function timeTag(iso) {
  const s = el("span", "tag tag-time");
  s.dataset.ts = iso || "";
  s.textContent = relTime(iso);
  return s;
}

function setEmpty(node, title, body) {
  const key = `${title}|${body}`;
  if (node.dataset.msg === key) return; // avoid rebuilding every poll
  node.dataset.msg = key;
  node.textContent = "";
  node.appendChild(el("strong", null, title));
  node.appendChild(document.createTextNode(body));
}

// --------------------------------------------------------------------------
// Actions — disable the trigger while in flight, toast the result, refresh.
// --------------------------------------------------------------------------
async function act(btn, request, okMsg) {
  if (btn) btn.disabled = true;
  try {
    await request();
    if (okMsg) toast(okMsg, "ok");
    poll(); // immediate refresh; poll() de-dupes if one is already in flight
  } catch (err) {
    if (err.status === 401) {
      handleSessionExpired();
      return;
    }
    toast(err.message || "Action failed.", "err");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function onCreateEvent(btn, input) {
  const name = input.value.trim();
  if (!name) {
    toast("Enter an event name first.", "err");
    input.focus();
    return;
  }
  act(
    btn,
    async () => {
      await api("/admin/events", { method: "POST", body: { name } });
      input.value = "";
    },
    "Event created — you're set up."
  );
}

async function onCloseRound() {
  if (!currentOpenRoundId) return;
  const ok = await confirmDialog({
    title: "Close this round?",
    body: "Badges will stop accepting questions for this round. You can open a new round at any time.",
    confirmLabel: "Close round",
  });
  if (!ok) return;
  const roundId = currentOpenRoundId;
  act(closeRoundBtn, () => api(`/admin/rounds/${roundId}/close`, { method: "POST" }), "Round closed.");
}

async function onCopyJoin() {
  const code = joinCode.textContent.trim();
  if (!code) return;
  try {
    if (!navigator.clipboard) throw new Error("no clipboard");
    await navigator.clipboard.writeText(code);
    toast(`Join code ${code} copied.`, "ok");
  } catch (_) {
    toast("Couldn't copy automatically — select the code to copy it.", "err");
  }
}

// --------------------------------------------------------------------------
// Confirmation dialog (native <dialog> = focus trap + Escape handling)
// --------------------------------------------------------------------------
function confirmDialog({ title, body, confirmLabel = "Confirm" }) {
  return new Promise((resolve) => {
    confirmTitle.textContent = title;
    confirmBody.textContent = body;
    confirmOk.textContent = confirmLabel;
    const onClose = () => {
      dialog.removeEventListener("close", onClose);
      resolve(dialog.returnValue === "confirm");
    };
    dialog.addEventListener("close", onClose);
    dialog.returnValue = "";
    dialog.showModal();
  });
}

// --------------------------------------------------------------------------
// Toast
// --------------------------------------------------------------------------
let toastTimer = null;
function toast(msg, kind = "ok") {
  toastEl.textContent = msg;
  toastEl.className = `toast ${kind === "err" ? "err" : "ok"}`;
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toastEl.hidden = true;
  }, 3200);
}

// --------------------------------------------------------------------------
// Profile menu + password toggle
// --------------------------------------------------------------------------
function toggleProfile(force) {
  const show = force !== undefined ? force : profileMenu.hidden;
  profileMenu.hidden = !show;
  profileBtn.setAttribute("aria-expanded", String(show));
}

function togglePassword() {
  const showing = passwordInput.type === "text";
  passwordInput.type = showing ? "password" : "text";
  togglePw.textContent = showing ? "Show" : "Hide";
  togglePw.setAttribute("aria-pressed", String(!showing));
  togglePw.setAttribute("aria-label", showing ? "Show password" : "Hide password");
  passwordInput.focus();
}

// --------------------------------------------------------------------------
// Time helpers
// --------------------------------------------------------------------------
function relTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

function clockNow() {
  return new Date().toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function providerLabel(p) {
  const v = String(p).toLowerCase();
  if (v.includes("agora")) return "Agora";
  if (v.includes("whisper")) return "Faster-Whisper";
  if (v === "ollama") return "Ollama";
  if (v === "openai_compatible") return "OpenAI-compatible";
  if (v === "mock") return "Mock";
  return p;
}

function initials(email) {
  if (!email) return "·";
  const local = String(email).split("@")[0] || "";
  const c = local.replace(/[^a-z0-9]/gi, "").charAt(0);
  return c ? c.toUpperCase() : "·";
}

// Relative-time ticker: keeps "12s ago" fresh without re-polling or rebuilding
// cards. Runs only while the console is visible.
setInterval(() => {
  if (!active || document.hidden) return;
  document.querySelectorAll("[data-ts]").forEach((node) => {
    const t = relTime(node.dataset.ts);
    if (node.textContent !== t) node.textContent = t;
  });
}, 1000);

// --------------------------------------------------------------------------
// Voice output (browser Web Speech API). The speech queue + exactly-once dedup
// logic lives in voice.js (window.PersephoneVoice) and is unit-tested; here we
// only wire it to the DOM and the poll loop. No tokens/secrets are stored — just
// harmless voice preferences (localStorage) and spoken-answer ids (sessionStorage).
// --------------------------------------------------------------------------
const VOICE_PREFS_KEY = "persephone_voice_prefs";
const speechSupported =
  typeof window !== "undefined" && "speechSynthesis" in window && !!window.PersephoneVoice;

let voice = null; // { queue, controller, prefs, enabled }

function loadVoicePrefs() {
  const prefs = { voiceURI: "", rate: 1, autoSpeak: true };
  try {
    const raw = localStorage.getItem(VOICE_PREFS_KEY);
    if (raw) Object.assign(prefs, JSON.parse(raw));
  } catch (_) {
    /* ignore */
  }
  return prefs;
}

function saveVoicePrefs() {
  if (!voice) return;
  try {
    localStorage.setItem(VOICE_PREFS_KEY, JSON.stringify(voice.prefs));
  } catch (_) {
    /* ignore */
  }
}

function initVoice() {
  if (!speechSupported) {
    setVoiceStatus("unavailable");
    for (const b of [voiceEnableBtn, voiceTestBtn, voiceStopBtn]) b.disabled = true;
    voiceAutoSpeak.disabled = true;
    voiceSelect.disabled = true;
    voiceRate.disabled = true;
    voiceHint.textContent =
      "This browser doesn't support speech synthesis. The dashboard still works — open it in Chrome or Edge to hear answers.";
    return;
  }
  const V = window.PersephoneVoice;
  const prefs = loadVoicePrefs();
  const speaker = V.createBrowserSpeaker(window.speechSynthesis, window.SpeechSynthesisUtterance);
  const queue = new V.SpeechQueue({
    speaker,
    onStateChange: (s) =>
      setVoiceStatus(s === "speaking" ? "speaking" : voice && voice.enabled ? "ready" : "disabled"),
  });
  const memory = new V.SpokenMemory({ storage: window.sessionStorage });
  const controller = new V.AutoSpeakController({ spokenMemory: memory });
  voice = { queue, controller, prefs, enabled: false };

  voiceAutoSpeak.checked = prefs.autoSpeak !== false;
  voiceRate.value = String(prefs.rate || 1);
  voiceRateVal.textContent = `${Number(prefs.rate || 1).toFixed(1)}×`;
  controller.setAutoSpeak(voiceAutoSpeak.checked, lastAssistantResponses);

  populateVoices();
  // Voices load asynchronously in most browsers.
  window.speechSynthesis.onvoiceschanged = populateVoices;
  setVoiceStatus("disabled");
}

function populateVoices() {
  if (!voice) return;
  const voices = window.speechSynthesis.getVoices() || [];
  const prev = voice.prefs.voiceURI;
  voiceSelect.textContent = "";
  const auto = el("option", null, "Browser default");
  auto.value = "";
  voiceSelect.appendChild(auto);
  for (const v of voices) {
    const opt = el("option", null, `${v.name} (${v.lang})`);
    opt.value = v.voiceURI;
    voiceSelect.appendChild(opt);
  }
  voiceSelect.value = prev || "";
}

function setVoiceStatus(kind) {
  const map = {
    disabled: ["voice-off", "Voice disabled"],
    ready: ["voice-ready", "Voice ready"],
    speaking: ["voice-speaking", "Persephone is speaking…"],
    unavailable: ["voice-err", "Speech unavailable"],
  };
  const [cls, text] = map[kind] || map.disabled;
  voiceDot.className = `voice-dot ${cls}`;
  voiceStatus.textContent = text;
}

function currentSpeechOpts() {
  return { rate: Number(voice.prefs.rate || 1), voiceURI: voice.prefs.voiceURI || "" };
}

function speakNow(text) {
  if (!voice || !text) return;
  const o = currentSpeechOpts();
  voice.queue.enqueue({ text, rate: o.rate, voiceURI: o.voiceURI });
}

function onEnableVoice() {
  if (!voice) return;
  if (!voice.enabled) {
    voice.enabled = true;
    voice.controller.setEnabled(true, lastAssistantResponses);
    voiceEnableBtn.textContent = "Disable voice";
    voiceEnableBtn.classList.remove("btn-primary");
    voiceTestBtn.disabled = false;
    voiceStopBtn.disabled = false;
    setVoiceStatus("ready");
    // Required user gesture: browsers may block speech until an interaction.
    speakNow("Persephone voice output is ready.");
  } else {
    voice.enabled = false;
    voice.controller.setEnabled(false, lastAssistantResponses);
    voice.queue.stop();
    voiceEnableBtn.textContent = "Enable voice";
    voiceEnableBtn.classList.add("btn-primary");
    voiceTestBtn.disabled = true;
    voiceStopBtn.disabled = true;
    setVoiceStatus("disabled");
  }
}

function onTestVoice() {
  speakNow("This is Persephone testing voice output through your selected speaker.");
}

function stopVoice() {
  if (voice) voice.queue.stop();
}

function replayAnswer(a) {
  if (!speechSupported) {
    toast("Speech isn't available in this browser.", "err");
    return;
  }
  if (voice) voice.controller.markReplayed(a.id);
  speakNow(a.response_text);
}

// Reset speech on logout/session-loss: cancel + clear the queue, drop the enabled
// state (a fresh session re-requires the Enable gesture). Preferences are kept.
function resetVoiceSession() {
  baselinePrimed = false;
  lastAssistantResponses = [];
  if (!voice) return;
  voice.queue.stop();
  voice.enabled = false;
  voice.controller.setEnabled(false, []);
  voiceEnableBtn.textContent = "Enable voice";
  voiceEnableBtn.classList.add("btn-primary");
  voiceTestBtn.disabled = true;
  voiceStopBtn.disabled = true;
  setVoiceStatus("disabled");
}

// Feed the controller after each poll: auto-speak newly completed answers once.
function processVoice(responses) {
  lastAssistantResponses = responses;
  if (!voice) return;
  if (!baselinePrimed) {
    // On (re)entry, everything already present is historical — never read the backlog.
    voice.controller.primeBaseline(responses);
    baselinePrimed = true;
  }
  const toSpeak = voice.controller.ingest(responses);
  for (const r of toSpeak) speakNow(r.response_text);
}

// --------------------------------------------------------------------------
// Wire-up
// --------------------------------------------------------------------------
loginForm.addEventListener("submit", onLogin);
togglePw.addEventListener("click", togglePassword);
logoutBtn.addEventListener("click", onLogout);

profileBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleProfile();
});
document.addEventListener("click", (e) => {
  if (!profileMenu.hidden && !profileEl.contains(e.target)) toggleProfile(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") toggleProfile(false);
});

createEventBtn.addEventListener("click", () => onCreateEvent(createEventBtn, eventNameInput));
createAnotherBtn.addEventListener("click", () => onCreateEvent(createAnotherBtn, newEventNameInput));
copyJoinBtn.addEventListener("click", onCopyJoin);

openRoundBtn.addEventListener("click", () => {
  const prompt = roundPromptInput.value.trim();
  act(
    openRoundBtn,
    async () => {
      await api("/admin/rounds", { method: "POST", body: { prompt: prompt || null } });
      roundPromptInput.value = "";
    },
    "Round open — you're ON AIR."
  );
});
closeRoundBtn.addEventListener("click", onCloseRound);
reclusterBtn.addEventListener("click", () =>
  act(reclusterBtn, () => api("/admin/recluster", { method: "POST" }), "Recomputing topics…")
);

// Voice output controls.
voiceEnableBtn.addEventListener("click", onEnableVoice);
voiceTestBtn.addEventListener("click", onTestVoice);
voiceStopBtn.addEventListener("click", stopVoice);
voiceAutoSpeak.addEventListener("change", () => {
  if (!voice) return;
  voice.prefs.autoSpeak = voiceAutoSpeak.checked;
  voice.controller.setAutoSpeak(voiceAutoSpeak.checked, lastAssistantResponses);
  saveVoicePrefs();
});
voiceSelect.addEventListener("change", () => {
  if (!voice) return;
  voice.prefs.voiceURI = voiceSelect.value;
  saveVoicePrefs();
});
voiceRate.addEventListener("input", () => {
  if (!voice) return;
  const rate = Number(voiceRate.value) || 1;
  voice.prefs.rate = rate;
  voiceRateVal.textContent = `${rate.toFixed(1)}×`;
  saveVoicePrefs();
});

// Refresh immediately when the operator returns to the tab.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && active) poll();
});

initVoice();
bootstrap();
