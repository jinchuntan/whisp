# Whisp Deployment Guide

A practical guide to deploying Whisp across its three runtime boundaries.

> Command blocks are labelled **Windows PowerShell** or **WSL / bash**. Run each in the indicated shell.

## 1. Overview

Whisp has three runtime boundaries:

1. **Vercel (web/API + static dashboard)** — the FastAPI app and the host dashboard (`public/`). Serves `/api/...` and the dashboard at `/`.
2. **Supabase (state + storage)** — Postgres for persistent state and a private Storage bucket (`whisp-audio`) for audio.
3. **WSL2 worker** — the transcription worker. Polls Supabase, downloads audio, transcribes, clusters, and heartbeats. It runs outside Vercel.

Badges talk to the system over **HTTP polling** — there is no inbound socket to the badge and no inbound exposure required for the worker.

## 2. Supabase Setup

1. Create a Supabase project.
2. Open the **SQL editor** and run `supabase/migrations/001_initial_schema.sql`. This applies the Postgres schema, the `claim_next_question()` RPC, and creates the private `whisp-audio` bucket.
3. Confirm the private **`whisp-audio`** bucket exists. The migration creates it; if it is missing, create it manually under **Storage** with **Public = OFF**.
4. **RLS is enabled on all tables**, so only the `service_role` key (used server-side) can read or write data.
5. Find your credentials under **Project Settings -> API**:
   - `SUPABASE_URL`
   - the **service_role** key (`SUPABASE_SERVICE_ROLE_KEY`)

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
route (`whisp_api/app.py`):

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
| Background threads / tasks / loops | **None** — no `threading`, `create_task`, `BackgroundTasks`, or polling loops in `whisp_api/`. |
| Module-level state assuming a long-lived process | **Safe** — only `@lru_cache` singletons (settings + Supabase client), rebuilt per cold start. No in-memory question store. |
| Secrets | All from `os.environ` (see below); none in code or committed files. |

### Vercel runtime constraints (respected)

- Only `/tmp` is writable — the API writes nothing.
- No background threads — the API starts none.
- 4.5 MB request body limit — badge WAVs are ~320 KB and the API caps uploads at
  `MAX_AUDIO_BYTES` (default 4 MB), comfortably under the limit.
- ~300s max function duration on Hobby — the API responds in well under a second.

### CORS & dashboard auth on a Vercel domain

The dashboard (`public/`) is served from the **same** Vercel domain and calls the
API with **relative** `/api/v1/...` paths, so it is same-origin — no CORS
preflight is involved. `CORSMiddleware` (`CORS_ALLOW_ORIGINS`, default `*`,
credentials off) only affects other origins; the badge is not a browser so CORS
does not apply to it. Admin auth uses an `Authorization: Bearer` header kept only
in `sessionStorage` — this works identically on `localhost` and on Vercel.

## 4. Configure Vercel Environment Variables

Set these under **Project Settings -> Environment Variables**, or via the CLI:

**Windows PowerShell / WSL / bash**
```bash
vercel env add SUPABASE_URL
vercel env add SUPABASE_SERVICE_ROLE_KEY
vercel env add SUPABASE_AUDIO_BUCKET
vercel env add BADGE_API_KEY
vercel env add ADMIN_API_KEY
vercel env add TRANSCRIPTION_MODE
vercel env add WORKER_OFFLINE_SECONDS
vercel env add CORS_ALLOW_ORIGINS
vercel env add MAX_AUDIO_BYTES
```

Web/API variables:

| Variable | Notes |
| --- | --- |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-side service_role key |
| `SUPABASE_AUDIO_BUCKET` | Default `whisp-audio` |
| `BADGE_API_KEY` | Auth for badge requests |
| `ADMIN_API_KEY` | Auth for admin/dashboard actions |
| `TRANSCRIPTION_MODE` | Display only on the API; default `faster_whisper_only` |
| `WORKER_OFFLINE_SECONDS` | Default `20` |
| `CORS_ALLOW_ORIGINS` | Allowed browser origins |
| `MAX_AUDIO_BYTES` | Default `4194304` |

Notes:

- These are read via `os.environ` at runtime and **only apply to NEW deployments** — redeploy after changing them.
- **NEVER** put `SUPABASE_SERVICE_ROLE_KEY` or Agora secrets in browser or firmware code.

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

1. In the dashboard, **create an event** and **open a round** (requires the admin key).
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

The default `faster_whisper_only` protects Agora credits. For Agora configuration, see [`docs/AGORA_SETUP.md`](AGORA_SETUP.md).

## 9. Retention

Audio auto-deletes after `AUDIO_RETENTION_HOURS` (default **24**) via the worker's periodic sweep. **Deleting audio does not delete transcripts** — transcripts remain in Postgres.
