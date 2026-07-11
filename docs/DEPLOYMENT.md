# Persephone Deployment Guide

A practical guide to deploying Persephone across its three runtime boundaries.

> Command blocks are labelled **Windows PowerShell** or **WSL / bash**. Run each in the indicated shell.

## 1. Overview

Persephone has three runtime boundaries:

1. **Vercel (web/API + static dashboard)** — the FastAPI app and the host dashboard (`public/`). Serves `/api/...` and the dashboard at `/`.
2. **Supabase (state + storage)** — Postgres for persistent state and a private Storage bucket (`persephone-audio`) for audio.
3. **WSL2 worker** — the transcription worker. Polls Supabase, downloads audio, transcribes, clusters, and heartbeats. It runs outside Vercel.

Badges talk to the system over **HTTP polling** — there is no inbound socket to the badge and no inbound exposure required for the worker.

## 2. Supabase Setup

1. Create a Supabase project.
2. Open the **SQL editor** and run `supabase/migrations/001_initial_schema.sql`. This applies the Postgres schema, the `claim_next_question()` RPC, and creates the private `persephone-audio` bucket.
3. Confirm the private **`persephone-audio`** bucket exists. The migration creates it; if it is missing, create it manually under **Storage** with **Public = OFF**.
4. **RLS is enabled on all tables**, so only the `service_role` key (used server-side) can read or write data.
5. Find your credentials under **Project Settings -> API**:
   - `SUPABASE_URL`
   - the **service_role** key (`SUPABASE_SERVICE_ROLE_KEY`) — server-side only
   - the **anon / public** key (`SUPABASE_ANON_KEY`) — used server-side for host login
6. **Provision host login.** Under **Authentication -> Users -> Add user**, create
   an email + password for each host and confirm the email. Under
   **Authentication -> Providers -> Email**, set **Enable sign-ups = OFF** — Persephone
   has no public registration. List each host email in `ADMIN_EMAIL_ALLOWLIST`
   (comma-separated, case-insensitive); only those users can reach host routes.

## 3. Deploy Web/API to Vercel

Connect the Git repo or use the CLI:

- **Git:** connect `github.com/jinchuntan/whisp` in the Vercel dashboard, or
- **CLI:**

**Windows PowerShell / WSL / bash**
```bash
vercel
```

Vercel **auto-detects FastAPI** from `requirements.txt`; the root `main.py` exposes `app`. **No build command is needed.**

**How static serving works.** Vercel serves the files in `public/` as static assets
at their paths automatically (`/styles.css`, `/app.js`, `/index.html`), and routes
everything else — including bare `/` **and** `/api/*` — to the FastAPI function.
The one gap is the root `/`: Vercel's FastAPI preset sends it to the function
regardless of any `vercel.json` `rewrites`, so it returns FastAPI's `Not Found`.

The deterministic fix lives in the app, not in Vercel config — a tiny redirect
route (`persephone_api/app.py`):

```python
@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/index.html")   # 307 -> statically-served dashboard
```

`/` → **307** → `/index.html`, which Vercel serves statically. `/styles.css`,
`/app.js`, and `/api/*` are unaffected. `index.html` references its assets
root-relatively (`/styles.css`, `/app.js`) and the dashboard calls `/api/v1/...`,
so everything resolves. Locally the same route redirects to the StaticFiles-served
`/index.html`. **No `vercel.json` is required.**

- **`.vercelignore`** excludes `worker/`, `firmware/`, `tests/`, `docs/`,
  `supabase/`, `requirements-dev.txt`, `pyproject.toml`, caches, and any local
  `.env` — but **not** `public/`. Excluding `pyproject.toml` forces Vercel to read
  **`requirements.txt`** as the single dependency source, and the heavy worker can
  never ship to Vercel.
- If a deploy ever regresses, use the **Instant Rollback** button in the Vercel
  dashboard to revert to the previous deployment.

### Serverless-compatibility audit

The FastAPI app is serverless-safe by design (verified):

| Concern | Status |
| --- | --- |
| Transcription in the request path | **No** — the API only enqueues to Supabase and reads state; the worker transcribes. |
| Filesystem writes | **None** — audio goes straight to Supabase Storage; nothing is written to disk (`/tmp` unused). |
| Background threads / tasks / loops | **None** — no `threading`, `create_task`, `BackgroundTasks`, or polling loops in `persephone_api/`. |
| Module-level state assuming a long-lived process | **Safe** — only `@lru_cache` singletons (settings + Supabase client), rebuilt per cold start. No in-memory question store. |
| Secrets | All from `os.environ` (see below); none in code or committed files. |

### Vercel runtime constraints (respected)

- Only `/tmp` is writable — the API writes nothing.
- No background threads — the API starts none.
- 4.5 MB request body limit — badge WAVs are ~320 KB and the API caps uploads at
  `MAX_AUDIO_BYTES` (default 4 MB), comfortably under the limit.
- ~300s max function duration on Hobby — the API responds in well under a second.

### CORS, cookies & dashboard auth on a Vercel domain

The dashboard (`public/`) is served from the **same** Vercel domain and calls the
API with **relative** `/api/v1/...` paths (and `credentials: "include"`), so it is
same-origin — no CORS preflight is involved. The badge is not a browser, so CORS
does not apply to it.

Host auth is a **server-side Supabase Auth** flow: the browser posts email/password
to `/api/v1/auth/login`, and the API sets the Supabase access/refresh tokens as
**HttpOnly, SameSite=Lax** cookies (Secure in production). JavaScript never sees the
tokens; there is no `Authorization` header and nothing in `localStorage` /
`sessionStorage`. State-changing requests are additionally CSRF-checked against the
`Origin` / `Referer` header.

**Set `CORS_ALLOW_ORIGINS` to your exact dashboard origin in production** (e.g.
`https://your-project.vercel.app`). Browsers refuse wildcard (`*`) CORS on
credentialed requests, and the same list drives the CSRF origin allowlist. The
default `*` is for local development only, where the app disables `allow_credentials`
for CORS but same-origin cookies still work.

## 4. Configure Vercel Environment Variables

Set these under **Project Settings -> Environment Variables**, or via the CLI:

**Windows PowerShell / WSL / bash**
```bash
vercel env add SUPABASE_URL
vercel env add SUPABASE_SERVICE_ROLE_KEY
vercel env add SUPABASE_ANON_KEY
vercel env add SUPABASE_AUDIO_BUCKET
vercel env add BADGE_API_KEY
vercel env add ADMIN_EMAIL_ALLOWLIST
vercel env add SESSION_COOKIE_SECURE
vercel env add CORS_ALLOW_ORIGINS
vercel env add TRANSCRIPTION_MODE
vercel env add WORKER_OFFLINE_SECONDS
vercel env add MAX_AUDIO_BYTES
```

Web/API variables:

| Variable | Required | Notes |
| --- | --- | --- |
| `SUPABASE_URL` | yes | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | Server-side service_role key (never in browser) |
| `SUPABASE_ANON_KEY` | yes | Public anon key; used server-side for host login |
| `SUPABASE_AUDIO_BUCKET` | no | Default `persephone-audio` |
| `BADGE_API_KEY` | yes | Auth for badge requests (`X-Persephone-Key`) |
| `ADMIN_EMAIL_ALLOWLIST` | yes | Comma-separated host emails allowed to sign in |
| `SESSION_COOKIE_SECURE` | prod | `true` in production (HTTPS); `false` for local http |
| `SESSION_COOKIE_SAMESITE` | no | `lax` (default) or `strict` |
| `SESSION_MAX_AGE_SECONDS` | no | Refresh-cookie lifetime; default `604800` (7 days) |
| `CORS_ALLOW_ORIGINS` | prod | Exact dashboard origin(s); wildcard rejected with cookies |
| `TRANSCRIPTION_MODE` | no | Display only on the API; default `faster_whisper_only` |
| `WORKER_OFFLINE_SECONDS` | no | Default `20` |
| `MAX_AUDIO_BYTES` | no | Default `4194304` |
| `ADMIN_API_KEY` | no | Legacy shim; only with `ALLOW_LEGACY_ADMIN_KEY=true` |
| `ALLOW_LEGACY_ADMIN_KEY` | no | Default `false`; keep off in production |

Notes:

- These are read via `os.environ` at runtime and **only apply to NEW deployments** — redeploy after changing them.
- **NEVER** put `SUPABASE_SERVICE_ROLE_KEY` or Agora secrets in browser or firmware code. The `SUPABASE_ANON_KEY` is public by design but is only used server-side here.
- Passwords, cookies, tokens, and the service-role key are never logged.

## 5. Run the Worker in WSL2

The worker runs in WSL2 Ubuntu.

**WSL / bash**
```bash
cd worker
python3 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip
# CPU-only machines: install the CPU PyTorch wheel FIRST so faster-whisper /
# sentence-transformers don't pull in multi-GB CUDA packages.
python -m pip install --index-url https://download.pytorch.org/whl/cpu torch
pip install -r requirements.txt   # heavy: faster-whisper + sentence-transformers
cp .env.example .env
# edit .env: set SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, and TRANSCRIPTION_MODE
python run_worker.py
```

- The **first run downloads** the Faster-Whisper model (example `.env` uses `small` — more accurate than `base` for whispered badge audio, ~2–3× slower on CPU) and the `all-MiniLM-L6-v2` embedding model to the Hugging Face cache (`~/.cache/huggingface`).
- The worker is **outbound-only** — it does not need inbound internet.
- The same worker can process jobs created by the Vercel API because **both talk to the same Supabase project**.

## 6. Confirming End-to-End

1. In the dashboard, **sign in** with your host email/password, then **create an event** and **open a round**.
2. POST a sample WAV to `/api/v1/questions`.
3. Watch the worker log **claim + transcribe**.
4. Confirm the transcript appears on the dashboard and via `GET /api/v1/questions/{id}`.

## 7. Moving the Worker Off WSL2 (Later)

The worker can later move from WSL2 to a persistent Linux container or VM (systemd or Docker), using the **same environment variables**. Nothing about the Vercel or Supabase setup changes.

## 8. Switching Transcription Modes

To change modes:

1. Change `TRANSCRIPTION_MODE` in the worker's `.env` (valid values: `faster_whisper_only`, `faster_whisper_first`, `agora_first`, `agora_only`).
2. Optionally update the Vercel `TRANSCRIPTION_MODE` variable so the dashboard displays the correct mode.
3. **Restart the worker.**

The default `faster_whisper_only` protects Agora credits. Agora runs **only in the
WSL2 worker** (never on Vercel). To use it: install the optional SDK
(`pip install -r worker/requirements-agora.txt`, Linux/WSL2 only), set the Agora
credentials + `AGORA_LIVE_ENABLED=true` in `worker/.env`, and start the worker via
`bash worker/scripts/run_worker_agora.sh` (it sets `LD_LIBRARY_PATH`). It is off by
default, guarded by a daily credit ceiling, and falls back to Faster-Whisper in
`agora_first`. Turn `AGORA_LIVE_ENABLED=false` after a demo. Full setup, the manual
canary live-test, and safe error codes are in [`docs/AGORA_SETUP.md`](AGORA_SETUP.md).

## 9. Retention

Audio auto-deletes after `AUDIO_RETENTION_HOURS` (default **24**) via the worker's periodic sweep. **Deleting audio does not delete transcripts** — transcripts remain in Postgres.
