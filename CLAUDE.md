# CLAUDE.md — working notes for AI assistants on this repo

Persephone is an anonymous voice Q&A system for conferences. Read `docs/ARCHITECTURE.md`
and `docs/IMPLEMENTATION_PLAN.md` before making changes.

## Three runtime boundaries (keep them separate)

1. **Web/API** (`persephone_api/`, `main.py`, `public/`) — deploys to **Vercel**.
   Lightweight only. **Never** add faster-whisper, torch, sentence-transformers,
   numpy, or any Agora media SDK to root `requirements.txt`. No transcription, no
   background threads, no local file persistence (Vercel FS is ephemeral).
2. **Worker** (`worker/`) — runs in **WSL2/Linux**. Owns all heavy ML + Agora.
3. **Storage** (`supabase/`) — Postgres + private `persephone-audio` bucket.

Badges ↔ API use **HTTP polling** (no MQTT/WebSockets). Upload is async: store +
`202 {question_id, poll_url}`; the worker transcribes; the badge polls.

## Golden rules

- **Provider routing is decided ONLY by `TRANSCRIPTION_MODE`** via
  `worker/persephone_worker/providers/router.py` (`MODE_ORDER`). Don't branch on
  provider name in business logic. `faster_whisper_only` must never construct or
  call Agora — a test enforces this (`worker/tests/test_router.py`).
- **Agora is real but credit-gated. Never spend credit without explicit opt-in.**
  The REST control-plane and the SDK-backed media bridge (`agora_bridge.py`: real
  RTC PCM publish + caption collection) are implemented, but live RTC/STT refuses to
  run unless `AGORA_LIVE_ENABLED=true` (default false) AND a real bridge is supplied;
  the default `UnavailableMediaBridge` raises before any paid REST call. Tests use
  fakes only — never load the native SDK, contact Agora, or spend credit. The single
  intentional credit-spending path is the guarded `persephone_worker.agora_canary`.
  Verified live end-to-end (2026-07-12, SDK v4.4.32). Two integration details can
  vary by account/SDK version and are isolated: REST join-body field names
  (`AgoraRestClient.build_join_body`) and caption field numbers (`agora_captions.py`).
  Note: PCM must be pushed as `bytearray`, and the `AgoraService` must be released
  before process exit. See `docs/AGORA_SETUP.md`.
- **Heavy imports are lazy** (inside methods), so tests run without the packages
  and never download a model. Keep it that way.
- **DB/Storage go through gateways** (`database.py`, `storage.py`, worker
  `queue.py`) that are faked in tests. Use dependency injection.
- **Secrets**: only via env / `.env` (gitignored). Never log raw auth headers,
  tokens, or full provider payloads. `config.h` (firmware) is gitignored.
- **Rendering**: dashboard uses `textContent`/`createElement` only — attendee
  text is untrusted. No `innerHTML` with dynamic data.

## Commands (WSL2)

```bash
make install          # web/API + dev deps into ./.venv
make test             # API/contract tests
make test-worker      # worker tests (no heavy deps needed)
make check            # ruff + format-check + mypy + both suites
make dev              # uvicorn on :8000
cd worker && python run_worker.py
```

## Tests must not

consume Agora credit, contact real Agora, require real Supabase, download a
Whisper model, or need the ESP32. Everything uses fakes + DI.

## Where things live

- API contract: `docs/API.md` · schemas: `persephone_api/schemas.py`
- DB schema + job-claim RPC: `supabase/migrations/001_initial_schema.sql`
- Provider router/modes: `worker/persephone_worker/providers/`
- Clustering: `worker/persephone_worker/clustering.py`
- Firmware: `firmware/persephone_badge/` (pins are proven — don't change casually)
