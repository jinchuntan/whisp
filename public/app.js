/* Whisp host console — vanilla JS. Safe DOM rendering (textContent only). */
"use strict";

const API = "/api/v1";
const POLL_MS = 1500;
const KEY_STORAGE = "whisp_admin_key";

const $ = (id) => document.getElementById(id);
let pollTimer = null;
let openRoundId = null;

// --------------------------------------------------------------------------
// Auth helpers
// --------------------------------------------------------------------------
function getKey() {
  return sessionStorage.getItem(KEY_STORAGE) || "";
}
function setKey(k) {
  sessionStorage.setItem(KEY_STORAGE, k);
}
function clearKey() {
  sessionStorage.removeItem(KEY_STORAGE);
}

async function api(path, { method = "GET", body = null } = {}) {
  const headers = { Authorization: `Bearer ${getKey()}` };
  if (body) headers["Content-Type"] = "application/json";
  const res = await fetch(`${API}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : null,
  });
  if (res.status === 401) {
    logout("Session key rejected. Please log in again.");
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try {
      const data = await res.json();
      if (data && data.detail) msg = typeof data.detail === "string" ? data.detail : msg;
      if (data && data.message) msg = data.message;
    } catch (_) {
      /* ignore */
    }
    throw new Error(msg);
  }
  if (res.status === 204) return null;
  return res.json();
}

// --------------------------------------------------------------------------
// Login / logout
// --------------------------------------------------------------------------
function showConsole() {
  $("login").hidden = true;
  $("console").hidden = false;
}
function logout(message) {
  clearKey();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
  $("console").hidden = true;
  $("login").hidden = false;
  const err = $("login-error");
  if (message) {
    err.textContent = message;
    err.hidden = false;
  }
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = $("admin-key").value.trim();
  const err = $("login-error");
  err.hidden = true;
  if (!key) return;
  setKey(key);
  try {
    await api("/admin/state"); // validates the key
    $("admin-key").value = "";
    showConsole();
    start();
  } catch (_) {
    err.textContent = "Invalid admin key.";
    err.hidden = false;
    clearKey();
  }
});

$("logout").addEventListener("click", () => logout(null));

// --------------------------------------------------------------------------
// Actions
// --------------------------------------------------------------------------
function banner(message, kind = "error") {
  const b = $("banner");
  b.textContent = message;
  b.className = `banner ${kind}`;
  b.hidden = false;
  setTimeout(() => {
    b.hidden = true;
  }, 4000);
}

$("create-event").addEventListener("click", async () => {
  const name = $("event-name").value.trim();
  if (!name) return banner("Enter an event name first.");
  try {
    await api("/admin/events", { method: "POST", body: { name } });
    $("event-name").value = "";
    banner("Event created and set active.", "info");
    refresh();
  } catch (e) {
    banner(e.message);
  }
});

$("open-round").addEventListener("click", async () => {
  const prompt = $("round-prompt").value.trim();
  try {
    await api("/admin/rounds", { method: "POST", body: { prompt: prompt || null } });
    $("round-prompt").value = "";
    banner("Round opened — ON AIR.", "info");
    refresh();
  } catch (e) {
    banner(e.message);
  }
});

$("close-round").addEventListener("click", async () => {
  if (!openRoundId) return;
  try {
    await api(`/admin/rounds/${openRoundId}/close`, { method: "POST" });
    banner("Round closed.", "info");
    refresh();
  } catch (e) {
    banner(e.message);
  }
});

$("recluster").addEventListener("click", async () => {
  try {
    await api("/admin/recluster", { method: "POST" });
    banner("Reclustering requested — the worker will regroup questions.", "info");
  } catch (e) {
    banner(e.message);
  }
});

async function markQuestionAnswered(id) {
  try {
    await api(`/admin/questions/${id}/answered`, { method: "POST" });
    refresh();
  } catch (e) {
    banner(e.message);
  }
}

async function markClusterAnswered(id) {
  try {
    await api(`/admin/clusters/${id}/answered`, { method: "POST" });
    refresh();
  } catch (e) {
    banner(e.message);
  }
}

// --------------------------------------------------------------------------
// Rendering (safe — textContent + createElement only)
// --------------------------------------------------------------------------
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function relTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  return `${Math.round(mins / 60)}h ago`;
}

function questionCard(q) {
  const card = el("div", "card");
  if (q.answered_at) card.classList.add("answered");

  const top = el("div", "card-top");
  top.appendChild(el("span", `tag tag-${q.status}`, q.status));
  if (q.provider_used) {
    const label = q.provider_used === "agora" ? "Agora" : "Faster-Whisper";
    top.appendChild(el("span", "tag tag-provider", label));
  }
  if (q.fallback_used) top.appendChild(el("span", "tag tag-fallback", "fallback"));
  if (typeof q.processing_ms === "number") {
    top.appendChild(el("span", "tag", `${q.processing_ms} ms`));
  }
  if (q.similar_count && q.similar_count > 1) {
    top.appendChild(el("span", "tag tag-provider", `+${q.similar_count - 1} similar`));
  }
  card.appendChild(top);

  let text;
  if (q.status === "done") {
    text = el("div", "card-text", q.transcript || "(empty)");
  } else if (q.status === "error") {
    text = el("div", "card-text placeholder", q.safe_error_message || "Transcription unavailable");
  } else if (q.status === "empty") {
    text = el("div", "card-text placeholder", "No speech detected");
  } else if (q.status === "transcribing") {
    text = el("div", "card-text placeholder", "Transcribing…");
  } else {
    text = el("div", "card-text placeholder", "Queued — waiting for the worker…");
  }
  card.appendChild(text);

  const foot = el("div", "card-foot");
  foot.appendChild(el("span", "tag tag-time", relTime(q.created_at)));
  if (q.status === "done" && !q.answered_at) {
    const btn = el("button", "btn btn-ghost btn-sm", "Mark answered");
    btn.addEventListener("click", () => markQuestionAnswered(q.id));
    foot.appendChild(btn);
  } else if (q.answered_at) {
    foot.appendChild(el("span", "tag tag-done", "answered"));
  } else {
    foot.appendChild(el("span", ""));
  }
  card.appendChild(foot);
  return card;
}

function clusterCard(c, rank) {
  const card = el("div", "card");
  if (c.status === "answered") card.classList.add("answered");

  const top = el("div", "card-top");
  top.appendChild(el("span", "cluster-rank", `#${rank}`));
  const countWrap = el("span", "");
  countWrap.appendChild(el("span", "cluster-count", String(c.question_count)));
  countWrap.appendChild(el("span", "cluster-count-label", " asking"));
  top.appendChild(countWrap);
  card.appendChild(top);

  card.appendChild(el("div", "card-text", c.canonical_question));

  const foot = el("div", "card-foot");
  foot.appendChild(el("span", "tag tag-time", relTime(c.created_at)));
  if (c.status !== "answered") {
    const btn = el("button", "btn btn-ghost btn-sm", "Mark answered");
    btn.addEventListener("click", () => markClusterAnswered(c.id));
    foot.appendChild(btn);
  } else {
    foot.appendChild(el("span", "tag tag-done", "answered"));
  }
  card.appendChild(foot);
  return card;
}

function renderState(state) {
  // Mode + agora warning
  $("mode-pill").textContent = `mode: ${state.transcription_mode}`;
  $("agora-warning").hidden = !state.agora_mode_active;

  // Worker
  const wp = $("worker-pill");
  if (state.worker_online) {
    let seen = "";
    const w = (state.workers || []).find((x) => x.online) || (state.workers || [])[0];
    if (w && w.last_seen_at) seen = ` · ${relTime(w.last_seen_at)}`;
    wp.textContent = `worker: online${seen}`;
    wp.className = "pill pill-ok";
  } else if ((state.workers || []).length) {
    const w = state.workers[0];
    wp.textContent = `worker: offline · ${relTime(w.last_seen_at)}`;
    wp.className = "pill pill-off";
  } else {
    wp.textContent = "worker: none";
    wp.className = "pill pill-off";
  }

  // Event
  if (state.event) {
    $("event-info").hidden = false;
    $("event-title").textContent = state.event.name;
    $("event-code").textContent = state.event.join_code;
  } else {
    $("event-info").hidden = true;
  }

  // Round + ON AIR
  const onair = $("onair");
  if (state.open_round) {
    openRoundId = state.open_round.id;
    onair.textContent = "ON AIR";
    onair.className = "onair on";
    $("close-round").disabled = false;
    $("recluster").disabled = false;
    $("round-info").hidden = false;
    $("round-current-prompt").textContent = state.open_round.prompt || "(no prompt)";
  } else {
    openRoundId = null;
    onair.textContent = "OFF AIR";
    onair.className = "onair off";
    $("close-round").disabled = true;
    $("recluster").disabled = true;
    $("round-info").hidden = true;
  }

  // Questions
  const qWrap = $("questions");
  qWrap.textContent = "";
  const questions = state.questions || [];
  $("question-count").textContent = String(questions.length);
  $("questions-empty").hidden = questions.length > 0;
  questions.forEach((q) => qWrap.appendChild(questionCard(q)));

  // Clusters (already ranked by count from the API)
  const cWrap = $("clusters");
  cWrap.textContent = "";
  const clusters = state.clusters || [];
  $("cluster-count").textContent = String(clusters.length);
  $("clusters-empty").hidden = clusters.length > 0;
  clusters.forEach((c, i) => cWrap.appendChild(clusterCard(c, i + 1)));

  $("conn").textContent = `live · updated ${new Date().toLocaleTimeString()}`;
}

// --------------------------------------------------------------------------
// Poll loop
// --------------------------------------------------------------------------
async function refresh() {
  try {
    const state = await api("/admin/state");
    renderState(state);
  } catch (e) {
    if (e.message !== "unauthorized") $("conn").textContent = `connection error: ${e.message}`;
  }
}

function start() {
  refresh();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(refresh, POLL_MS);
}

// Auto-resume if a key is already in this tab.
if (getKey()) {
  api("/admin/state")
    .then(() => {
      showConsole();
      start();
    })
    .catch(() => logout(null));
}
