/* Persephone voice output — speech queue + exactly-once deduplication.
 *
 * This module is deliberately FRAMEWORK-FREE and side-effect-free so it can be
 * unit-tested under Node (see voice.test.mjs) as well as loaded as a plain
 * <script> in the browser. The browser-specific bits (speechSynthesis) live
 * behind an injectable "speaker" adapter, so the queue/dedup logic is pure.
 *
 * Nothing here stores tokens/passwords/API keys — only harmless voice
 * preferences (localStorage) and spoken-response ids (sessionStorage).
 */
(function (root, factory) {
  "use strict";
  const mod = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = mod;
  else (root || globalThis).PersephoneVoice = mod;
})(
  typeof self !== "undefined"
    ? self
    : typeof globalThis !== "undefined"
      ? globalThis
      : this,
  function () {
    "use strict";

  // A response is speakable only when it is a COMPLETED answer with real text.
  // queued / generating / error are never spoken.
  function isSpeakable(r) {
    return !!(
      r &&
      r.id != null &&
      r.status === "done" &&
      typeof r.response_text === "string" &&
      r.response_text.trim().length > 0
    );
  }

  // -------------------------------------------------------------------------
  // SpokenMemory — a bounded, persisted set of already-spoken response ids.
  // Backed by sessionStorage in the browser so a tab refresh does not re-read
  // the backlog; prunes oldest ids so it can never grow without bound.
  // -------------------------------------------------------------------------
  class SpokenMemory {
    constructor({ storage = null, key = "persephone_spoken_ids", max = 500 } = {}) {
      this.storage = storage;
      this.key = key;
      this.max = max;
      this._order = [];
      this._set = new Set();
      this._load();
    }

    _load() {
      let raw = null;
      try {
        raw = this.storage ? this.storage.getItem(this.key) : null;
      } catch (_) {
        raw = null;
      }
      if (raw) {
        try {
          const arr = JSON.parse(raw);
          if (Array.isArray(arr)) {
            for (const id of arr) {
              const s = String(id);
              if (!this._set.has(s)) {
                this._set.add(s);
                this._order.push(s);
              }
            }
          }
        } catch (_) {
          /* corrupt value -> start empty */
        }
      }
    }

    _persist() {
      if (!this.storage) return;
      try {
        this.storage.setItem(this.key, JSON.stringify(this._order));
      } catch (_) {
        /* storage full / unavailable -> keep in-memory only */
      }
    }

    has(id) {
      return this._set.has(String(id));
    }

    add(id) {
      const s = String(id);
      if (this._set.has(s)) return;
      this._set.add(s);
      this._order.push(s);
      while (this._order.length > this.max) {
        const evicted = this._order.shift();
        this._set.delete(evicted);
      }
      this._persist();
    }

    get size() {
      return this._set.size;
    }
  }

  // -------------------------------------------------------------------------
  // SpeechQueue — FIFO of utterances spoken one at a time via a speaker adapter.
  // New answers are appended (never cancel the current one); stop() clears all.
  // -------------------------------------------------------------------------
  class SpeechQueue {
    constructor({ speaker, onStateChange = null } = {}) {
      this.speaker = speaker;
      this.onStateChange = onStateChange;
      this._items = [];
      this._speaking = false;
    }

    get isSpeaking() {
      return this._speaking;
    }

    get pending() {
      return this._items.length;
    }

    enqueue(item) {
      if (!item || !item.text) return;
      this._items.push(item);
      if (!this._speaking) this._drain();
    }

    _setSpeaking(v) {
      if (this._speaking === v) return;
      this._speaking = v;
      if (this.onStateChange) this.onStateChange(v ? "speaking" : "idle");
    }

    _drain() {
      if (this._speaking) return;
      const item = this._items.shift();
      if (!item) {
        this._setSpeaking(false);
        return;
      }
      this._setSpeaking(true);
      const done = () => {
        this._speaking = false;
        // Continue with the next queued item, if any.
        if (this._items.length) this._drain();
        else this._setSpeaking(false);
      };
      try {
        this.speaker.speak(
          item.text,
          { rate: item.rate, voiceURI: item.voiceURI },
          { onend: done, onerror: done }
        );
      } catch (_) {
        done();
      }
    }

    stop() {
      this._items = [];
      try {
        this.speaker.cancel();
      } catch (_) {
        /* ignore */
      }
      this._setSpeaking(false);
    }
  }

  // -------------------------------------------------------------------------
  // AutoSpeakController — decides which newly-completed answers to auto-speak,
  // exactly once. Everything present when speech is (re)enabled becomes a
  // historical baseline that is never auto-spoken; only answers completed after
  // that point are queued. Manual replay bypasses this entirely.
  // -------------------------------------------------------------------------
  class AutoSpeakController {
    constructor({ spokenMemory } = {}) {
      this.spoken = spokenMemory || new SpokenMemory();
      this.enabled = false; // voice output unlocked (user gesture done)
      this.autoSpeak = true; // auto-play new answers
      this._baseline = new Set(); // ids considered historical (never auto-spoken)
      this.observedDone = new Set(); // every completed id ever seen
    }

    get speakingAllowed() {
      return this.enabled && this.autoSpeak;
    }

    // Mark everything currently present as historical, so the backlog is never
    // read aloud. Called on first load and whenever auto-speak (re)starts.
    refreshBaseline(responses) {
      for (const r of responses || []) {
        if (isSpeakable(r)) {
          this._baseline.add(String(r.id));
          this.observedDone.add(String(r.id));
        }
      }
    }

    primeBaseline(responses) {
      this.refreshBaseline(responses);
    }

    setEnabled(value, responses) {
      const was = this.speakingAllowed;
      this.enabled = !!value;
      if (this.speakingAllowed && !was) this.refreshBaseline(responses);
    }

    setAutoSpeak(value, responses) {
      const was = this.speakingAllowed;
      this.autoSpeak = !!value;
      if (this.speakingAllowed && !was) this.refreshBaseline(responses);
    }

    // Process a poll's responses; return the ones to speak now (in input order).
    // The caller should pass responses sorted by completion time for predictable
    // ordering. Idempotent across polls: an id is returned at most once.
    ingest(responses) {
      const toSpeak = [];
      for (const r of responses || []) {
        if (!isSpeakable(r)) continue;
        const id = String(r.id);
        this.observedDone.add(id);
        if (!this.speakingAllowed) continue;
        if (this._baseline.has(id)) continue;
        if (this.spoken.has(id)) continue;
        this.spoken.add(id);
        toSpeak.push(r);
      }
      return toSpeak;
    }

    // Manual replay: mark spoken (so a later poll does not also auto-speak it)
    // but do NOT gate on baseline/enabled — replay is itself a user gesture.
    markReplayed(id) {
      this.spoken.add(String(id));
      this.observedDone.add(String(id));
    }
  }

  // -------------------------------------------------------------------------
  // Browser speaker adapter around window.speechSynthesis (not used in tests).
  // -------------------------------------------------------------------------
  function createBrowserSpeaker(synth, UtteranceCtor) {
    return {
      speak(text, opts, handlers) {
        const u = new UtteranceCtor(text);
        if (opts && typeof opts.rate === "number") u.rate = opts.rate;
        if (opts && opts.voiceURI) {
          const voices = synth.getVoices ? synth.getVoices() : [];
          const match = voices.find((v) => v.voiceURI === opts.voiceURI);
          if (match) u.voice = match;
        }
        if (handlers) {
          u.onend = handlers.onend || null;
          u.onerror = handlers.onerror || null;
        }
        synth.speak(u);
      },
      cancel() {
        synth.cancel();
      },
    };
  }

  return { isSpeakable, SpokenMemory, SpeechQueue, AutoSpeakController, createBrowserSpeaker };
});
