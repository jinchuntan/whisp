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

Vercel **auto-detects FastAPI** from `requirements.txt`; the root `main.py` exposes `app`. The `public/` directory is served automatically at `/`. **No build command is needed.**

The `vercel.json` config keeps the function bundle lean:

- `functions."main.py".maxDuration = 60`
- `excludeFiles` keeps the worker, tests, firmware, and other ML/heavy files out of the deployed function bundle.

### Vercel runtime constraints

- Only `/tmp` is writable.
- No background threads.
- 4.5 MB request body limit.
- ~300s max function duration on Hobby.

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
pip install -r requirements.txt   # heavy: faster-whisper + sentence-transformers
cp .env.example .env
# edit .env: set SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, and TRANSCRIPTION_MODE
python run_worker.py
```

- The **first run downloads** the Whisper "base" model and the `all-MiniLM-L6-v2` embedding model to the Hugging Face cache (`~/.cache/huggingface`).
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
