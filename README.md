# Persephone 🎙️

**Anonymous voice Q&A for conferences.** Lean into a lanyard badge, speak quietly,
and your question appears anonymously on the speaker's screen — no raised hand, no
mic runner, no interruption. Similar questions are clustered by meaning so the host
sees what the room actually thinks, and your badge tells you when others asked the
same thing.

> Everyone has an opinion. Nobody asks. Persephone gives the room a voice.

---

## How it works

```
ESP32-S3 badge ──(hold button, record WAV)──▶ Vercel FastAPI API ──▶ Supabase
      ▲                                              │  (queue job + private audio)
      │  poll transcript                             ▼
   TFT shows text ◀───────── WSL2 worker (Faster-Whisper / Agora) claims job,
   "+N asked similar"        transcribes, clusters, heartbeats ─────┘
                                          ▲
                     Host dashboard (public/) ── open/close rounds, live feed, clusters
```

- **Async upload-and-poll:** upload returns `202 {question_id, poll_url}` instantly;
  the worker transcribes off the request path (Vercel-safe). The badge polls.
- **Provider switch + fallback:** `TRANSCRIPTION_MODE` selects Faster-Whisper and/or
  Agora with automatic fallback. Default `faster_whisper_only` needs no Agora and
  spends no credit.
- **Semantic clustering:** local `all-MiniLM-L6-v2` embeddings + cosine similarity
  group similar questions; the badge sees `similar_count`.

Read `docs/ARCHITECTURE.md` for the design and `docs/API.md` for the contract.

## Repository layout

```
main.py                 Vercel/FastAPI entrypoint (from persephone_api.app import app)
persephone_api/              FastAPI app: routes (/api/v1), auth, DB + storage gateways
public/                 Host dashboard (index.html, app.js, styles.css)
worker/                 WSL2 transcription worker (faster-whisper / Agora / clustering)
supabase/migrations/    Postgres schema + atomic job-claim RPC + private bucket
firmware/persephone_badge/   ESP32-S3 Arduino sketch (proven pins + display fix)
tests/                  API + contract tests (44)      worker/tests/  worker tests (49)
docs/                   ARCHITECTURE, API, AGORA_SETUP, DEPLOYMENT, HARDWARE, PLAN
```

## Runtime boundaries

| Plane | Runs on | Heavy deps? |
|-------|---------|-------------|
| Web/API + dashboard | **Vercel** (serverless FastAPI serves both) | No — `requirements.txt` is lightweight |
| State + audio | **Supabase** (Postgres + private Storage) | n/a |
| Transcription worker | **WSL2 Ubuntu** (outbound-only) | Yes — `worker/requirements.txt` |

Badges ↔ API use **HTTP polling** — no MQTT, no WebSockets.

---

## Required software

- **Windows 11 + WSL2 Ubuntu** (already installed). Python 3.10+ inside WSL.
- A **Supabase** project (free tier).
- **Arduino IDE** (or arduino-cli) with the ESP32 core 3.x — for the badge only.
- Optional: **Agora** account (only if you want the Agora provider; not required).

Everything below runs inside **WSL2** unless it says PowerShell.

## 1) Set up Supabase

1. Create a project at <https://supabase.com>.
2. Open **SQL Editor** and run **every file in `supabase/migrations/` in order**:
   `001_initial_schema.sql` (tables, the atomic `claim_next_question()` job-claim
   function, RLS on every table) then `002_rebrand_persephone.sql` (creates the
   private **`persephone-audio`** bucket; non-destructive). `001` also creates the
   historical `whisp-audio` bucket — that is expected and left in place. Upgrading
   an existing project? See [`docs/REBRAND_MIGRATION.md`](docs/REBRAND_MIGRATION.md).
3. Confirm **Storage → persephone-audio** exists and is **not public**. (If your project
   blocks `storage.buckets` inserts, create it manually: Storage → New bucket →
   name `persephone-audio`, Public = **OFF**.)
4. Copy **Project Settings → API**: the **Project URL** (`SUPABASE_URL`), the
   **service_role** key (`SUPABASE_SERVICE_ROLE_KEY`), and the **anon / public**
   key (`SUPABASE_ANON_KEY`). The service_role key bypasses RLS — keep it
   server-side only, never in the browser or firmware. The anon key is public by
   design and is used only server-side for host login (see step 5).
5. **Create your host login.** Open **Authentication → Users → Add user** and add
   an email + password for yourself (confirm the email). Then, under
   **Authentication → Providers → Email**, turn **Enable sign-ups OFF** — Persephone
   has no public registration; hosts are provisioned here by hand. You'll list
   this email in `ADMIN_EMAIL_ALLOWLIST` below so only it can reach host routes.

## 2) Configure the web/API `.env`

```bash
cp .env.example .env
# edit .env: set SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_ANON_KEY, and
# ADMIN_EMAIL_ALLOWLIST (the host email you created in step 5). Then a badge key:
python3 -c "import secrets; print('BADGE_API_KEY=badge_'+secrets.token_urlsafe(24))"
```
Put the badge key in `.env`. Set `ADMIN_EMAIL_ALLOWLIST=you@yourevent.com`. For
local `http://localhost` dev set `SESSION_COOKIE_SECURE=false` (cookies over plain
HTTP); keep it **true** in production. Leave `TRANSCRIPTION_MODE=faster_whisper_only`.

> Host sign-in is email/password (Supabase Auth), handled entirely server-side so
> tokens live in HttpOnly cookies — never in the browser's JS or storage. The old
> shared `ADMIN_API_KEY` is retained only as an optional test/CLI shim behind
> `ALLOW_LEGACY_ADMIN_KEY` (default **off**); the browser never uses it.

## 3) Install and run the API locally

The web/API holds only lightweight deps. In the repo root (WSL2):

```bash
make install          # creates ./.venv and installs web/API + dev deps
make dev              # uvicorn on http://0.0.0.0:8000  (also serves the dashboard)
```
Open <http://localhost:8000/> for the dashboard and
<http://localhost:8000/api/docs> for Swagger. Sign in to the dashboard with the
**email and password** you created in Supabase Auth (step 5).

> PowerShell alternative (if you prefer running the API on Windows): use a working
> Python 3.10+ and `python -m venv .venv; .\.venv\Scripts\Activate.ps1;
> pip install -r requirements.txt -r requirements-dev.txt;
> uvicorn main:app --reload --port 8000`.

## 4) Install and run the worker (WSL2)

The worker holds the heavy ML deps and runs separately from the API.

```bash
cd worker
python3 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip

# CPU-only machines: install the CPU PyTorch wheel FIRST to avoid multi-GB CUDA
# downloads pulled in by faster-whisper / sentence-transformers.
python -m pip install --index-url https://download.pytorch.org/whl/cpu torch

pip install -r requirements.txt        # faster-whisper + sentence-transformers
cp .env.example .env                    # set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY
python run_worker.py
```
The **first run downloads** the Faster-Whisper model and the `all-MiniLM-L6-v2`
embedding model into the Hugging Face cache (`~/.cache/huggingface`) — these are
**not** committed to git. Startup logs the model-loading status and the active
transcription mode.

> **Model choice:** the example `.env` uses `FASTER_WHISPER_MODEL=small`. Badge
> audio is often whispered, and `base` was insufficient for whispered speech in
> hardware testing; `small` is more accurate but ~2–3× slower on CPU. See
> `worker/README.md`.

> Quick Faster-Whisper check (optional):
> ```bash
> python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8'); print('faster-whisper OK')"
> ```

## 5) Test the full flow

With the API and worker running and an active event + open round (create them in the
dashboard), post a sample WAV:

```bash
# make a 1-second 16 kHz mono WAV of silence (or use a real recording)
python3 - <<'PY'
import wave, struct
with wave.open('sample.wav','wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(struct.pack('<16000h', *([0]*16000)))
PY

BASE=http://localhost:8000
curl -s -X POST "$BASE/api/v1/questions" \
  -H "Content-Type: audio/wav" -H "X-Persephone-Key: $BADGE_API_KEY" \
  -H "X-Badge-Id: badge-001" --data-binary @sample.wav
# -> {"ok":true,"question_id":"...","status":"queued","poll_url":"/api/v1/questions/..."}

curl -s -H "X-Persephone-Key: $BADGE_API_KEY" "$BASE/api/v1/questions/<question_id>"
```
Watch the worker log the claim → transcription → cluster steps, then see the
question on the dashboard. (Silence yields `status: "empty"` — record real speech
to get a transcript.)

## 6) Flash the ESP32 badge

See `firmware/persephone_badge/README.md` and `docs/HARDWARE.md`. In short:

1. `cp firmware/persephone_badge/config.example.h firmware/persephone_badge/config.h`
   (gitignored — never commit Wi-Fi/API keys).
2. Set `WIFI_SSID`, `WIFI_PASSWORD` (2.4 GHz), `API_BASE_URL`, `BADGE_API_KEY`,
   `BADGE_ID`.
   - Local (Windows Mobile Hotspot): `http://192.168.137.1:8000`
   - Vercel: `https://your-project.vercel.app`
3. Open `persephone_badge.ino` in Arduino IDE with the board settings in
   `docs/HARDWARE.md`, install Adafruit GFX + ILI9341, and upload.

## 7) Deploy the web/API to Vercel

**Only the FastAPI app + dashboard deploy to Vercel — the worker stays running on
your local/WSL2 machine.** Vercel auto-detects FastAPI from `requirements.txt`
(root `main.py` exposes `app`), serves `public/` as static assets
(`/index.html`, `/app.js`, `/styles.css`), and routes everything else — including
bare `/` and `/api/*` — to the function. So the FastAPI app has a small `GET /`
route that **redirects to `/index.html`** (a `vercel.json` rewrite does *not* work
— Vercel's FastAPI preset sends `/` to the function regardless). `.vercelignore`
keeps the worker, tests, and ML out of the deploy. No `vercel.json` is needed.

- **Git integration:** connect the repo in the Vercel dashboard — pushes deploy
  automatically. No build command needed.
- **CLI:** `npm i -g vercel && vercel login`, then `make deploy-preview` (preview)
  or `make deploy` (production).

Set the web/API env vars in **Project Settings → Environment Variables** (exact
list in `docs/DEPLOYMENT.md`): `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
`SUPABASE_ANON_KEY`, `SUPABASE_AUDIO_BUCKET`, `BADGE_API_KEY`,
`ADMIN_EMAIL_ALLOWLIST`, `SESSION_COOKIE_SECURE=true`,
`CORS_ALLOW_ORIGINS=https://your-project.vercel.app`, `TRANSCRIPTION_MODE`
(+ optional `SESSION_COOKIE_SAMESITE`, `SESSION_MAX_AGE_SECONDS`,
`WORKER_OFFLINE_SECONDS`, `MAX_AUDIO_BYTES`). In production, set
`CORS_ALLOW_ORIGINS` to your exact dashboard origin — browsers reject wildcard CORS
with credentialed cookies. **Never** put the service_role key or Agora secrets in
the browser or firmware.

Point the badge's `API_BASE_URL` at your Vercel URL. The **same WSL2 worker**
processes jobs created by Vercel because both talk to the same Supabase project —
no redeploy of the worker is needed.

> **Local dev vs Vercel:** locally, `make dev` runs one process that serves both
> the API and the dashboard (FastAPI StaticFiles mounted at `/`, API routers ahead
> of it), so `/` → the mount serves `index.html`. On Vercel, `public/` is served
> by Vercel's static layer and `/api/*` + bare `/` reach the function; the `GET /`
> route redirects to the statically-served `/index.html`. Same code, both work.

---

## Transcription modes

Set `TRANSCRIPTION_MODE` in `worker/.env` (and, for the dashboard's display, the
Vercel/`.env` var), then restart the worker:

| Mode | Behaviour |
|------|-----------|
| `faster_whisper_only` *(default)* | Local only. **Never touches Agora** — no credit. |
| `faster_whisper_first` | Local, fall back to Agora if it fails/returns empty. |
| `agora_first` | Agora, fall back to Faster-Whisper. Shows a credit warning. |
| `agora_only` | Agora only, no fallback. |

Switching modes needs no code changes. A developer with **no Agora credentials**
can run the entire flow on Faster-Whisper. Agora is a real, credit-safe provider
that runs only in the WSL2 worker: it publishes badge PCM into an RTC channel via
`agora-python-server-sdk` and reads back captions. It never runs unless you install
the optional SDK, set `AGORA_LIVE_ENABLED=true`, and it stays off by default. Full
setup, the manual **canary** live-test, and safety details are in
`docs/AGORA_SETUP.md`.

## Developer commands (WSL2)

```bash
make test          # API/contract tests (44)
make test-worker   # worker tests (49) — no heavy deps needed
make check         # ruff + format-check + mypy + both suites
make lint          # ruff check
make fmt           # ruff format
make typecheck     # mypy (persephone_api + main)
```
Tests never contact Agora/Supabase, download a model, or need the ESP32 (fakes +
dependency injection throughout).

## Security & privacy

- **Host auth:** email/password via Supabase Auth, handled server-side. Access and
  refresh tokens live in **HttpOnly, SameSite** cookies (Secure in production) and
  are never exposed to JavaScript. Only emails in `ADMIN_EMAIL_ALLOWLIST` may reach
  host routes; there is no public sign-up. State-changing requests are CSRF-checked
  by Origin/Referer. The legacy shared `ADMIN_API_KEY` is off by default and used
  only for tests/CLI when `ALLOW_LEGACY_ADMIN_KEY=true`.
- **Badge auth:** shared `X-Persephone-Key` (`BADGE_API_KEY`), constant-time compared —
  unchanged. **Production roadmap:** per-badge credentials.
- Audio is stored in a **private** bucket; RLS denies anonymous DB access; the
  browser only calls our API with `credentials: "include"`. The service_role key
  and Agora secrets are server/worker-only — never in firmware or the browser.
- Attendee text is treated as untrusted (dashboard renders via `textContent` /
  `createElement` only — no `innerHTML` with dynamic data).
- Passwords, cookies, tokens, and the service-role key are never logged.
- Audio auto-deletes after `AUDIO_RETENTION_HOURS` (default 24); transcripts remain.
- Secrets live only in `.env` / `config.h` (both gitignored). No secrets are
  committed.

## What works today

- ✅ Vercel-deployable FastAPI API + static dashboard; state persists in Postgres;
  audio in private Storage; upload returns a job id fast; badge polls to completion.
- ✅ Faster-Whisper worker: atomic job claim (`FOR UPDATE SKIP LOCKED` + lease
  reclaim), every provider attempt recorded, heartbeats, retention sweep.
- ✅ All four `TRANSCRIPTION_MODE` values with fallback; unit tests prove ordering,
  fallback (exception/timeout/empty), no-fallback in `*_only`, and **zero Agora
  ops in `faster_whisper_only`**.
- ✅ Semantic clustering + `similar_count`; dashboard shows provider/fallback/
  latency, cluster cards, worker online/offline, and the active mode.
- ✅ ESP32 firmware implementing the async upload-and-poll contract.

## What needs your setup / credentials

- 🔧 A Supabase project + the SQL migration (state/audio).
- 🔧 `BADGE_API_KEY` / `ADMIN_API_KEY` you generate.
- 🔧 Wi-Fi credentials in `firmware/persephone_badge/config.h`.
- 🔌 **Agora (optional):** requires real credentials + credit and the Linux-only
  `agora-python-server-sdk` (install `worker/requirements-agora.txt`, run via
  `worker/scripts/run_worker_agora.sh`, set `AGORA_LIVE_ENABLED=true`). Off by
  default; guarded by a daily credit ceiling and a manual **canary** live-test. See
  `docs/AGORA_SETUP.md`. Faster-Whisper covers the full flow without it.
