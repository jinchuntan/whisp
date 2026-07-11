/* Unit tests for public/voice.js — the speech queue + exactly-once dedup logic.
 *
 * Framework-free: uses Node's built-in test runner (node:test) + assert, no npm
 * install, no browser. Run with:  node --test public/voice.test.mjs
 * (or from the repo root:  node --test public/ )
 */
import test from "node:test";
import assert from "node:assert/strict";

// voice.js is a UMD module (also loaded as a plain browser <script>). Importing
// it for side-effect attaches the API to globalThis regardless of whether the
// host treats .js as CJS or ESM.
import "./voice.js";
const voice = globalThis.PersephoneVoice;

const { SpokenMemory, SpeechQueue, AutoSpeakController } = voice;

// -- helpers ---------------------------------------------------------------
function memStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    _map: map,
  };
}

// A synchronous fake speaker: speaks immediately and calls onend, recording order.
function syncSpeaker(spokenOut) {
  return {
    speak(text, _opts, handlers) {
      spokenOut.push(text);
      if (handlers && handlers.onend) handlers.onend();
    },
    cancel() {
      spokenOut.push("<cancel>");
    },
  };
}

// A manual speaker where each utterance completes only when finish() is called.
function manualSpeaker() {
  const state = { spoken: [], pendingEnd: null, cancels: 0 };
  return {
    speaker: {
      speak(text, _opts, handlers) {
        state.spoken.push(text);
        state.pendingEnd = handlers && handlers.onend;
      },
      cancel() {
        state.cancels += 1;
        state.pendingEnd = null;
      },
    },
    finish() {
      const cb = state.pendingEnd;
      state.pendingEnd = null;
      if (cb) cb();
    },
    state,
  };
}

const done = (id, text = "answer " + id) => ({ id, status: "done", response_text: text });

// ==========================================================================
// SpokenMemory
// ==========================================================================
test("SpokenMemory persists and dedupes", () => {
  const storage = memStorage();
  const m = new SpokenMemory({ storage, key: "k" });
  m.add("a");
  m.add("a");
  m.add("b");
  assert.equal(m.size, 2);
  assert.ok(m.has("a") && m.has("b"));
  // A fresh instance backed by the same storage keeps the ids (survives refresh).
  const m2 = new SpokenMemory({ storage, key: "k" });
  assert.ok(m2.has("a") && m2.has("b"));
});

test("SpokenMemory prunes oldest beyond max", () => {
  const m = new SpokenMemory({ storage: memStorage(), key: "k", max: 3 });
  ["a", "b", "c", "d", "e"].forEach((x) => m.add(x));
  assert.equal(m.size, 3);
  assert.ok(!m.has("a") && !m.has("b"));
  assert.ok(m.has("c") && m.has("d") && m.has("e"));
});

// ==========================================================================
// SpeechQueue
// ==========================================================================
test("SpeechQueue speaks queued items in order", () => {
  const out = [];
  const q = new SpeechQueue({ speaker: syncSpeaker(out) });
  q.enqueue({ text: "one" });
  q.enqueue({ text: "two" });
  q.enqueue({ text: "three" });
  assert.deepEqual(out, ["one", "two", "three"]);
  assert.equal(q.isSpeaking, false);
});

test("SpeechQueue does not cancel the current utterance when a new one arrives", () => {
  const m = manualSpeaker();
  const q = new SpeechQueue({ speaker: m.speaker });
  q.enqueue({ text: "first" });
  q.enqueue({ text: "second" }); // queued behind first, must NOT cancel it
  assert.deepEqual(m.state.spoken, ["first"]);
  assert.equal(m.state.cancels, 0);
  m.finish(); // first ends -> second starts
  assert.deepEqual(m.state.spoken, ["first", "second"]);
  assert.equal(m.state.cancels, 0);
});

test("SpeechQueue.stop clears pending and cancels", () => {
  const m = manualSpeaker();
  const states = [];
  const q = new SpeechQueue({ speaker: m.speaker, onStateChange: (s) => states.push(s) });
  q.enqueue({ text: "a" });
  q.enqueue({ text: "b" });
  q.stop();
  assert.equal(q.pending, 0);
  assert.equal(q.isSpeaking, false);
  assert.equal(m.state.cancels, 1);
  assert.ok(states.includes("speaking") && states.at(-1) === "idle");
});

// ==========================================================================
// AutoSpeakController — exactly-once + baseline
// ==========================================================================
test("nothing is spoken while voice is disabled", () => {
  const c = new AutoSpeakController({ spokenMemory: new SpokenMemory({ storage: memStorage() }) });
  const speak = c.ingest([done(1), done(2)]);
  assert.equal(speak.length, 0);
  // But they are observed, so enabling later treats them as baseline.
  assert.ok(c.observedDone.has("1"));
});

test("existing answers at enable time are baseline (backlog not read)", () => {
  const c = new AutoSpeakController({ spokenMemory: new SpokenMemory({ storage: memStorage() }) });
  const existing = [done(1), done(2)];
  c.ingest(existing); // first poll, disabled
  c.setEnabled(true, existing); // establishes baseline from what's present
  assert.deepEqual(c.ingest(existing), []); // backlog stays silent
  // A NEW answer completed after enable is spoken exactly once.
  const next = [...existing, done(3)];
  assert.deepEqual(
    c.ingest(next).map((r) => r.id),
    [3]
  );
  assert.deepEqual(c.ingest(next), []); // re-poll: not spoken again
});

test("polling/rerender does not repeat speech", () => {
  const c = new AutoSpeakController({ spokenMemory: new SpokenMemory({ storage: memStorage() }) });
  c.setEnabled(true, []);
  const poll = [done(10)];
  assert.equal(c.ingest(poll).length, 1);
  for (let i = 0; i < 5; i++) assert.equal(c.ingest(poll).length, 0);
});

test("queued / generating / error responses are never spoken", () => {
  const c = new AutoSpeakController({ spokenMemory: new SpokenMemory({ storage: memStorage() }) });
  c.setEnabled(true, []);
  const speak = c.ingest([
    { id: 1, status: "queued", response_text: null },
    { id: 2, status: "generating", response_text: null },
    { id: 3, status: "error", response_text: null },
    { id: 4, status: "done", response_text: "   " }, // empty -> not speakable
    done(5, "real answer"),
  ]);
  assert.deepEqual(
    speak.map((r) => r.id),
    [5]
  );
});

test("multiple new answers queue in input order", () => {
  const c = new AutoSpeakController({ spokenMemory: new SpokenMemory({ storage: memStorage() }) });
  c.setEnabled(true, []);
  const speak = c.ingest([done(1), done(2), done(3)]);
  assert.deepEqual(
    speak.map((r) => r.id),
    [1, 2, 3]
  );
});

test("auto-speak toggle gates auto play and re-baselines on re-enable", () => {
  const c = new AutoSpeakController({ spokenMemory: new SpokenMemory({ storage: memStorage() }) });
  c.setEnabled(true, []);
  c.setAutoSpeak(false, []);
  const arrived = [done(1)];
  assert.equal(c.ingest(arrived).length, 0); // auto-speak off -> silent
  c.setAutoSpeak(true, arrived); // re-baseline: the one that arrived is historical now
  assert.equal(c.ingest(arrived).length, 0);
  assert.deepEqual(
    c.ingest([...arrived, done(2)]).map((r) => r.id),
    [2]
  );
});

test("markReplayed prevents a later auto-speak of the same id", () => {
  const c = new AutoSpeakController({ spokenMemory: new SpokenMemory({ storage: memStorage() }) });
  c.setEnabled(true, []);
  c.markReplayed(7);
  assert.equal(c.ingest([done(7)]).length, 0);
});

test("spoken ids survive a simulated refresh via shared storage", () => {
  const storage = memStorage();
  const c1 = new AutoSpeakController({ spokenMemory: new SpokenMemory({ storage }) });
  c1.setEnabled(true, []);
  assert.equal(c1.ingest([done(99)]).length, 1);
  // New controller (page refresh) with the same sessionStorage: id already spoken.
  const c2 = new AutoSpeakController({ spokenMemory: new SpokenMemory({ storage }) });
  c2.setEnabled(true, []); // baseline empty (nothing passed), but spoken-memory remembers 99
  assert.equal(c2.ingest([done(99)]).length, 0);
});
