# Persephone API (v1)

Base path: `/api/v1`. All examples assume the API runs at `$BASE`
(e.g. `http://localhost:8000` locally, or `https://your-project.vercel.app`).

## Authentication

| Caller | Mechanism | Secret |
|--------|-----------|--------|
| Badge  | `X-Persephone-Key: <BADGE_API_KEY>` header | shared badge key, constant-time compared |
| Host (dashboard) | HttpOnly session cookies from `POST /auth/login` | Supabase email/password + `ADMIN_EMAIL_ALLOWLIST` |
| Host (legacy) | `Authorization: Bearer <ADMIN_API_KEY>` | off by default (`ALLOW_LEGACY_ADMIN_KEY`); tests/CLI only |

Host auth is a **server-side Supabase Auth** flow. The browser posts
email/password to `/auth/login`; the API validates against Supabase, checks the
email allowlist, and sets the access/refresh tokens as **HttpOnly, SameSite=Lax**
cookies (Secure in production). Tokens are never returned to JavaScript. Session
requests use `credentials: "include"`; state-changing requests are CSRF-checked via
the `Origin`/`Referer` header. Badge key auth is a header, so it is CSRF-safe and
unaffected.

Errors are returned as `{"detail": "..."}` (FastAPI) or
`{"ok": false, "error": ..., "message": ...}` for internal errors; stack traces,
secrets, passwords, and tokens are never exposed or logged.

---

## Auth endpoints

### `POST /api/v1/auth/login`  → `200`
Body `{ "email": "...", "password": "..." }`. On success sets `persephone_at` /
`persephone_rt` HttpOnly cookies and returns `{ "authenticated": true, "email": "..." }`.
`401` for wrong credentials, `403` if the email is not on the allowlist (no cookies
set), `503` if login is not configured / Supabase is unreachable.

### `GET /api/v1/auth/me`
Returns the current session status; transparently refreshes an expired access token
using the refresh cookie. No error for anonymous callers:
```json
{ "authenticated": false, "email": null, "role": null }
```
or when signed in: `{ "authenticated": true, "email": "...", "role": "host" }`.

### `POST /api/v1/auth/logout`  → `200`
Revokes the Supabase session (best-effort) and clears the cookies. Returns
`{ "ok": true }`.

---

## Health

### `GET /api/v1/health`
No auth. Does not touch Supabase or Agora.

```bash
curl $BASE/api/v1/health
```
```json
{
  "status": "ok",
  "service": "persephone-api",
  "version": "0.1.0",
  "time": "2026-07-11T12:00:00Z",
  "supabase_configured": true
}
```

---

## Badge endpoints

### `GET /api/v1/badge/state?badge_id=badge-001`
Header: `X-Persephone-Key`. Returns the active event/round and any "similar question"
notifications for this badge; also updates the badge's `last_seen_at`.

```bash
curl -H "X-Persephone-Key: $BADGE_API_KEY" \
     "$BASE/api/v1/badge/state?badge_id=badge-001"
```
```json
{
  "event_id": "uuid|null",
  "event_name": "DevConf 2026",
  "round_id": "uuid|null",
  "round_prompt": "What should we build next?",
  "accepting": true,
  "notifications": [
    {"question_id": "uuid", "cluster_id": "uuid", "similar_count": 3,
     "canonical_question": "How can AI improve participation?"}
  ],
  "server_time": "2026-07-11T12:00:00Z"
}
```

### `POST /api/v1/questions`
Headers: `Content-Type: audio/wav`, `X-Persephone-Key`, `X-Badge-Id`, optional
`X-Round-Id`. Body: raw standard PCM16 mono WAV bytes (≤ `MAX_AUDIO_BYTES`,
default 4 MB — under Vercel's 4.5 MB limit).

Validates auth, body size, RIFF/WAVE + mono PCM16 structure, badge-id format, and
round validity, then stores the audio privately, inserts a `queued` job, and
returns **202** immediately (no transcription in-request).

```bash
curl -X POST "$BASE/api/v1/questions" \
  -H "Content-Type: audio/wav" \
  -H "X-Persephone-Key: $BADGE_API_KEY" \
  -H "X-Badge-Id: badge-001" \
  --data-binary @sample.wav
```
```json
{ "ok": true, "question_id": "uuid", "status": "queued",
  "poll_url": "/api/v1/questions/uuid" }
```

Error statuses: `400` (bad WAV / badge-id / closed round), `401` (bad key),
`413` (too large), `502` (storage failure).

### `GET /api/v1/questions/{question_id}`
Header: `X-Persephone-Key`. Poll for the result. One of:

```json
{ "question_id": "...", "status": "queued" }
```
```json
{ "question_id": "...", "status": "transcribing" }
```
```json
{
  "question_id": "...",
  "status": "done",
  "transcript": "How can AI improve audience participation?",
  "provider": "faster_whisper",
  "fallback_used": false,
  "similar_count": 3,
  "cluster_id": "uuid"
}
```
```json
{ "question_id": "...", "status": "empty", "message": "No speech detected" }
```
```json
{ "question_id": "...", "status": "error", "message": "Transcription unavailable" }
```

`claimed` and `transcribing` internal states both surface as `queued`/
`transcribing` to the badge. Unknown id → `404`.

---

## Admin endpoints

All require a host session (the `persephone_at`/`persephone_rt` cookies from `/auth/login`).
State-changing calls also require a matching `Origin`/`Referer` (CSRF). The `curl`
examples below use `-b cookies.txt` after logging in with
`curl -c cookies.txt -X POST "$BASE/api/v1/auth/login" -H 'Content-Type: application/json' -d '{"email":"...","password":"..."}'`.
(With `ALLOW_LEGACY_ADMIN_KEY=true`, `Authorization: Bearer <ADMIN_API_KEY>` also
works for tests/CLI.)

### `GET /api/v1/admin/state`
The dashboard's single source of truth (poll every ~1.5 s).

```json
{
  "server_time": "2026-07-11T12:00:00Z",
  "transcription_mode": "faster_whisper_only",
  "agora_mode_active": false,
  "event": { "id": "...", "name": "...", "join_code": "AB12CD", "active": true },
  "open_round": { "id": "...", "event_id": "...", "prompt": "...", "status": "open" },
  "rounds": [ /* RoundOut[] */ ],
  "questions": [
    { "id": "...", "status": "done", "badge_id": "badge-001",
      "transcript": "...", "provider_used": "faster_whisper",
      "fallback_used": false, "processing_ms": 1300, "round_id": "...",
      "cluster_id": "...", "similar_count": 3, "created_at": "...",
      "answered_at": null }
  ],
  "clusters": [
    { "id": "...", "canonical_question": "...", "question_count": 3,
      "status": "open", "created_at": "..." }
  ],
  "workers": [
    { "worker_id": "host-123", "version": "0.1.0",
      "transcription_mode": "faster_whisper_only", "status": "idle",
      "last_seen_at": "...", "online": true }
  ],
  "worker_online": true
}
```

### `POST /api/v1/admin/events`  → `201`
```bash
curl -X POST "$BASE/api/v1/admin/events" \
  -b cookies.txt -H "Origin: $BASE" \
  -H "Content-Type: application/json" -d '{"name": "DevConf 2026"}'
```
Creates an event (random 6-char `join_code`) and makes it the active event.

### `POST /api/v1/admin/rounds`  → `201`
Body `{ "prompt": "…", "event_id": "…"? }`. Opens a round for the active event
(closing any already-open round first). `prompt` is optional.

### `POST /api/v1/admin/rounds/{id}/close`  → `200`
Closes the round. `404` if unknown.

### `POST /api/v1/admin/questions/{id}/answered`  → `200`
Marks a question answered (`{"ok": true}`). `404` if unknown.

### `POST /api/v1/admin/clusters/{id}/answered`  → `200`
Marks a cluster answered. `404` if unknown.

### `POST /api/v1/admin/recluster[?round_id=…]`  → `200`
Requests the worker regroup the round's questions. Defaults to the active event's
open round. `400` if there's no round to recluster.

### `GET /api/v1/admin/worker-health`  → `200`
```json
{ "server_time": "...", "worker_online": true,
  "workers": [ { "worker_id": "...", "online": true, "last_seen_at": "...",
                 "transcription_mode": "faster_whisper_only" } ] }
```
`online` = heartbeat within `WORKER_OFFLINE_SECONDS` (default 20 s).

---

## Interactive docs

FastAPI serves Swagger UI at `/api/docs` and the schema at `/api/openapi.json`.
