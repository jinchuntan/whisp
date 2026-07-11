# Whisp — Architecture

This document records the major architectural decisions and the reasoning behind
them. It complements `IMPLEMENTATION_PLAN.md` (scope/phases) and `API.md` (contract).

## System overview

```
 ┌────────────┐   audio/wav (≤4.5MB)   ┌──────────────────────────┐
 │ ESP32-S3   │ ─────────────────────▶ │  Vercel: FastAPI API      │
 │ badge      │   POST /api/v1/questions│  (whisp_api + main.py)   │
 │ (firmware) │ ◀───────────────────── │  202 {question_id,poll}   │
 └────────────┘   poll GET /questions/id└─────────┬────────────────┘
        ▲                                          │ insert row (queued)
        │ shows transcript                         │ upload wav (private)
        │ + "N asked similar"                      ▼
 ┌────────────┐                        ┌──────────────────────────┐
 │ Host        │  admin API (Bearer)   │  Supabase                 │
 │ dashboard   │ ◀────────────────────▶│  Postgres + Storage       │
 │ (public/)   │  poll admin/state     │  (state + whisp-audio)    │
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
`whisp-audio` bucket for WAVs. The browser never queries privileged tables directly;
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

### D7 — Agora isolated behind a boundary, with an honest mock
All Agora code lives in `providers/agora.py` (+ helpers). Nothing contacts Agora at
import time or as a health check. The **REST control-plane** (v7.x `join`/query/
`leave`, HTTP Basic auth) is implemented against verified endpoints. The **media
bridge** (pushing PCM into the RTC channel so Agora's bot can hear it) is defined as a
`MediaBridge` protocol; the real implementation requires the Linux-only
`agora-python-server-sdk` + real credentials + credit, so it is left as a documented
boundary rather than fabricated. A deterministic `MockAgoraProvider` powers tests and
credential-free demos. See `docs/AGORA_SETUP.md` for the exact blocker.

### D8 — Auth: separate prototype keys, constant-time compare
Badge endpoints require `X-Whisp-Key: <BADGE_API_KEY>`; mutating admin endpoints
require `Authorization: Bearer <ADMIN_API_KEY>`. Keys are compared with
`hmac.compare_digest`. This is explicitly **hackathon** auth; the roadmap is
per-badge credentials and a real admin session. Documented in `docs/API.md` and the
security section of `README.md`.

### D9 — Observability by question id
Structured logs trace a question through upload → storage → claim → each provider
attempt → completion → cluster assignment, tagged with `question_id`, badge id,
provider, duration, and a **safe** status. Credentials, raw auth headers, and full
provider payloads are never logged. Worker heartbeats (`worker_heartbeats`) let the
dashboard show worker online/offline, mode, version, and last-seen.

## Module map

```
main.py                       # Vercel entrypoint: `from whisp_api.app import app`
whisp_api/
  app.py        # create_app(): routers, DI, local static serving
  config.py     # Settings (pydantic-settings) from env
  auth.py       # badge + admin dependencies (constant-time)
  database.py   # Database gateway (Supabase PostgREST + rpc) + protocol
  storage.py    # Storage gateway (Supabase Storage)
  models.py     # enums + domain constants (statuses, modes)
  schemas.py    # Pydantic request/response models
  routes/       # health, badge, questions, admin
worker/whisp_worker/
  config.py     # worker Settings
  worker.py     # main loop: heartbeat, claim, transcribe, cluster, cleanup
  queue.py      # JobQueue: rpc claim, status/result writes, attempts, heartbeat
  audio.py      # download, WAV validate/parse, PCM conversion
  clustering.py # Clusterer + EmbeddingModel protocol
  providers/
    base.py         # TranscriptionProvider protocol, TranscriptionResult, errors
    router.py       # ProviderRouter, MODE_ORDER
    faster_whisper.py
    agora.py        # AgoraProvider (REST + boundary) + MockAgoraProvider
```

## Data flow for one question

1. Badge holds button → records 16 kHz mono PCM16 into PSRAM → builds WAV.
2. `POST /api/v1/questions` (`X-Whisp-Key`, `X-Badge-Id`, optional `X-Round-Id`).
3. API validates auth, size, RIFF/WAVE, badge id, round → uploads WAV to
   `whisp-audio/<badge>/<uuid>.wav` → inserts `questions` row `queued` → `202`.
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
