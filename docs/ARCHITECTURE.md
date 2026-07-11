# Persephone — Architecture

This document records the major architectural decisions and the reasoning behind
them. It complements `IMPLEMENTATION_PLAN.md` (scope/phases) and `API.md` (contract).

## System overview

```
 ┌────────────┐   audio/wav (≤4.5MB)   ┌──────────────────────────┐
 │ ESP32-S3   │ ─────────────────────▶ │  Vercel: FastAPI API      │
 │ badge      │   POST /api/v1/questions│  (persephone_api + main.py)   │
 │ (firmware) │ ◀───────────────────── │  202 {question_id,poll}   │
 └────────────┘   poll GET /questions/id└─────────┬────────────────┘
        ▲                                          │ insert row (queued)
        │ shows transcript                         │ upload wav (private)
        │ + "N asked similar"                      ▼
 ┌────────────┐                        ┌──────────────────────────┐
 │ Host        │ login (cookie session)│  Supabase                 │
 │ dashboard   │ ◀────────────────────▶│  Postgres + Storage       │
 │ (public/)   │  poll admin/state     │  (state + persephone-audio)    │
 └────────────┘                        └─────────┬────────────────┘
                                        claim job │  ▲ write result,
                                  (rpc SKIP LOCKED)▼  │ attempts, clusters
                                       ┌──────────────────────────┐
                                       │  WSL2 worker              │
                                       │  faster-whisper / Agora   │
                                       │  + sentence-transformers  │
                                       └──────────────────────────┘
```

## Decision log

### D1 — Async upload-and-poll, not synchronous transcription
Vercel functions cannot run long tasks or background threads and have a 4.5 MB body
limit and ~300 s ceiling. Transcription (especially Agora's channel dance) is slow
and belongs off the request path. So upload **only** persists audio + a `queued`
row and returns `202` immediately. The badge and dashboard poll. This is the single
biggest divergence from the reference prototype (which returned a job id but was
otherwise synchronous/in-memory).

Trade-off: the badge must poll (implemented at ~800–1000 ms with a ~30 s cap).
Benefit: the API is stateless, fast, restart-safe, and Vercel-safe.

### D2 — Postgres for state, private Storage for audio
The reference kept state in a Python dict and audio on local disk — neither survives
a serverless cold start or restart, and Vercel's filesystem is ephemeral (`/tmp`
only). We use Supabase Postgres for all structured state and a **private**
`persephone-audio` bucket for WAVs. The browser never queries privileged tables directly;
it goes through our API. RLS is enabled on every table so anonymous/direct access is
denied by default; the service_role key (server-only) bypasses RLS intentionally.

### D3 — Atomic job claim via `FOR UPDATE SKIP LOCKED`
Multiple workers (or a restarted worker) must never double-process a job, and a
crashed worker must not strand one. A Postgres function `claim_next_question` selects
the oldest claimable row (`queued`, or `claimed/transcribing` with an expired lease)
`FOR UPDATE SKIP LOCKED`, marks it `claimed` with a fresh `lease_expires_at` +
`worker_id`, and returns it — all in one statement, invoked via `supabase.rpc(...)`.
Running it inside a function keeps the lock/commit in a single transaction, which is
safe even through Supabase's transaction pooler.

### D4 — Provider router keyed only by `TRANSCRIPTION_MODE`
`providers/router.py` maps the mode string to an ordered list of provider **names**
(`MODE_ORDER`), then constructs providers lazily from a factory registry and runs
them in order with per-provider timeouts. A provider that raises, times out, or
returns empty text triggers the next provider **only if the ordered list has one** —
so `*_only` modes get no fallback for free. Consequences:
- Switching providers = change one env var. No business-logic edits.
- `faster_whisper_only` never constructs the Agora factory → Agora is never imported,
  no channel is joined, no credit is consumed. A unit test asserts the Agora factory
  is never called in that mode.
- Every attempt (provider, order, status, latency, safe error, safe metadata) is
  recorded to `transcription_attempts` regardless of outcome.

### D5 — Gateways + dependency injection for testability
`Database` and `Storage` are thin classes wrapping the Supabase client. Routes get
them via FastAPI dependency overrides; the worker gets them via constructor args.
Tests inject `FakeDatabase`/`FakeStorage` and fake providers/embedders. Heavy imports
(`faster_whisper`, `sentence_transformers`, Agora SDK) happen **inside methods**, not
at module top level, so importing the modules under test never requires the heavy
packages and never downloads a model.

### D6 — Clustering without pgvector or paid LLMs
The worker embeds a completed transcript with a local `all-MiniLM-L6-v2` model and
compares (cosine) against existing clusters in the same round. Above the configurable
threshold (`0.78`) → join the nearest cluster; otherwise create a new one with this
transcript as the canonical question. Cluster embeddings are stored as
`double precision[]` so **no pgvector extension is required**; the similarity math
runs in the worker. pgvector is documented as an optional future upgrade for
DB-side nearest-neighbour search. `question_count` is maintained transactionally via
an RPC. `similar_count` returned to the badge = the cluster's member count.

### D7 — Agora isolated behind a boundary, credit-safe, with a real media bridge
All Agora code lives in `providers/agora.py` + `agora_bridge.py` + `agora_token.py`
+ `agora_captions.py`. Nothing contacts Agora at import time or as a health check;
every SDK import is lazy. The **REST control-plane** (`join`/query/`leave`, HTTP
Basic auth) and the **real media bridge** (`AgoraPcmMediaBridge`: one `AgoraService`
per process, per-question RTC connection, real-time 10 ms PCM publishing, caption
collection off the data stream) are both implemented against the installed
`agora-python-server-sdk`. RTC tokens use a vendored copy of Agora's official
**AccessToken2** builder. Live RTC/STT is gated behind `AGORA_LIVE_ENABLED`
(default off) and a daily credit ceiling, joins+connects the publisher **before**
starting the paid STT task, and defaults to `UnavailableMediaBridge` so no credit is
ever spent by accident. Tests use fakes only — no native libs, no network, no
credit. A `MockAgoraProvider` remains for credential-free demos. The one manual,
credit-spending path is `agora_canary.py`. **Verified live (2026-07-12, SDK
v4.4.32):** a speech clip transcribed end-to-end via `agora`. The two former
live-verify items (REST join-body field names, caption field numbers) are confirmed
and isolated; PCM is pushed as `bytearray` and the `AgoraService` is released before
exit. See `docs/AGORA_SETUP.md`.

### D8 — Auth: badge key (headers) + host session (cookies)
Badge endpoints require `X-Persephone-Key: <BADGE_API_KEY>`, compared with
`hmac.compare_digest` — a header, so CSRF-safe and unchanged. Host/admin endpoints
require a **server-side Supabase Auth session**: `/api/v1/auth/login` validates
email/password against Supabase, enforces `ADMIN_EMAIL_ALLOWLIST`, and stores the
access/refresh tokens in **HttpOnly, SameSite** cookies (Secure in production).
Tokens never reach JavaScript; the access token is refreshed transparently via the
refresh cookie. State-changing session requests are CSRF-checked against
`Origin`/`Referer`. A legacy shared `ADMIN_API_KEY` remains available only for
tests/CLI behind `ALLOW_LEGACY_ADMIN_KEY` (default off). Implemented in
`persephone_api/auth.py`, `persephone_api/supabase_auth.py`, and `persephone_api/routes/auth.py`;
documented in `docs/API.md` and the security section of `README.md`. **Roadmap:**
per-badge credentials.

### D9 — Observability by question id
Structured logs trace a question through upload → storage → claim → each provider
attempt → completion → cluster assignment, tagged with `question_id`, badge id,
provider, duration, and a **safe** status. Credentials, raw auth headers, and full
provider payloads are never logged. Worker heartbeats (`worker_heartbeats`) let the
dashboard show worker online/offline, mode, version, and last-seen.

### D10 — Voice assistant: worker-side answers + browser speech (see VOICE_ASSISTANT.md)
A chatbot answers each transcribed question and the host dashboard speaks it aloud
through the browser's Web Speech API (Windows routes that audio to a paired
Bluetooth speaker). Design choices mirror the transcription pipeline:
- **Answer generation runs in the worker, never on Vercel.** The LLM call is slow
  and belongs off the request path — the API only reads/writes the queue.
- **Selection is keyed only by `CHATBOT_MODE`** (`disabled | mock | ollama |
  openai_compatible`), fully independent of `TRANSCRIPTION_MODE`/Agora. `disabled`
  makes zero chatbot network calls. Providers use `httpx` (no heavy SDK) behind an
  injectable transport, so tests never touch the network or spend LLM credit.
- **A second job table, `assistant_responses`** (one row per question, unique
  `question_id`), with its own atomic `claim_next_assistant_response`
  (`FOR UPDATE SKIP LOCKED` + lease reclamation) and an **idempotent
  reconciliation** RPC `enqueue_missing_assistant_responses` — so a worker that
  dies right after transcription still gets exactly one answer job (crash-safe,
  not reliant on an in-memory call).
- **Two independent async loops** in the worker share the queue via
  `asyncio.gather`, so a slow LLM never blocks or extends a transcription lease. A
  chatbot failure never changes a question's status — transcription/clustering stay
  usable. Retry ceiling = `CHATBOT_MAX_ATTEMPTS`; then a public-safe error.
- **Speech is browser-only.** No cloud TTS, **no Agora credit**, no microphone
  access. Exactly-once playback: spoken ids in `sessionStorage`, a baseline marks
  the backlog historical on enable/refresh, and only newly-completed answers are
  spoken. The dedup/queue logic lives in `public/voice.js` and is unit-tested under
  Node (`public/voice.test.mjs`). Answers are host-only — the badge poll response
  is unchanged.

## Module map

```
main.py                       # Vercel entrypoint: `from persephone_api.app import app`
persephone_api/
  app.py           # create_app(): routers, DI, CORS, local static serving
  config.py        # Settings (pydantic-settings) from env
  auth.py          # badge key + host session dependencies, cookies, CSRF
  supabase_auth.py # server-side Supabase Auth REST client (login/user/refresh/logout)
  database.py      # Database gateway (Supabase PostgREST + rpc) + protocol
  storage.py       # Storage gateway (Supabase Storage)
  models.py        # enums + domain constants (statuses, modes)
  schemas.py       # Pydantic request/response models
  routes/          # health, auth, badge, questions, admin
worker/persephone_worker/
  config.py     # worker Settings (transcription + chatbot)
  worker.py     # transcription loop: heartbeat, claim, transcribe, cluster, cleanup
  assistant.py  # AssistantProcessor: reconcile → claim → generate → store (2nd loop)
  queue.py      # JobQueue: rpc claim (questions + assistant_responses), writes
  audio.py      # download, WAV validate/parse, PCM conversion
  clustering.py # Clusterer + EmbeddingModel protocol
  providers/
    base.py         # TranscriptionProvider protocol, TranscriptionResult, errors
    router.py       # ProviderRouter, MODE_ORDER
    faster_whisper.py
    agora.py        # AgoraProvider (REST + boundary) + MockAgoraProvider
  chatbot/
    base.py         # ChatbotProvider protocol, result, errors, prompt, validation
    providers.py    # Mock / Ollama / OpenAI-compatible (httpx, injectable transport)
    __init__.py     # build_chatbot_provider(settings) factory (None when disabled)
public/
  voice.js        # UMD speech queue + exactly-once dedup (browser + Node-testable)
  voice.test.mjs  # node --test unit tests for voice.js
```

## Data flow for one question

1. Badge holds button → records 16 kHz mono PCM16 into PSRAM → builds WAV.
2. `POST /api/v1/questions` (`X-Persephone-Key`, `X-Badge-Id`, optional `X-Round-Id`).
3. API validates auth, size, RIFF/WAVE, badge id, round → uploads WAV to
   `persephone-audio/<badge>/<uuid>.wav` → inserts `questions` row `queued` → `202`.
4. Worker `claim_next_question` → `claimed` (lease). Downloads WAV to `/tmp`.
5. Router runs providers per mode; each attempt logged to `transcription_attempts`.
6. On success: `questions` → `done` with transcript/provider/fallback/latency.
   Empty → `empty`; all providers failed → `error` (safe message).
7. Worker clusters the transcript → assigns/creates cluster → updates counts +
   `questions.cluster_id`.
8. Badge polling sees `done` with `transcript`, `provider`, `fallback_used`,
   `similar_count`, `cluster_id`. Dashboard shows the question + cluster cards.
9. Retention job deletes WAVs older than `AUDIO_RETENTION_HOURS` (default 24) without
   deleting transcripts/questions.
