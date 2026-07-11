# Agora Real-Time Speech-to-Text — setup, live-test, and safety

**TL;DR:** Persephone works end-to-end **without Agora** using Faster-Whisper (the
default). Agora is an optional, **credit-safe** provider that runs only inside the
**WSL2/Linux worker**. It never runs during development or tests, and never spends
credit unless you explicitly enable it and run the canary. The Vercel API and the
badge never touch Agora.

---

## 1. What Agora RT-STT actually is

Agora Real-Time STT is **not** a "POST a WAV, get text" API. It is an **RTC-channel**
product:

1. You start a transcription task via REST; Agora spins up bot(s) that **join your
   RTC channel** and **subscribe to the audio** published there.
2. Something must **publish audio into that channel** for the bot to hear.
3. The transcript comes back on the channel **data stream** (live captions).

So to transcribe our pre-recorded badge WAV, the worker uses Agora's **server SDK**
(`agora-python-server-sdk`) as a **media bridge**: it joins the channel as a
publisher and pushes 16 kHz/mono/PCM16 audio in real time (10 ms / 320-byte frames),
then reads captions off the data stream.

## 2. Architecture (what's implemented)

```
worker (WSL2)                                   Agora cloud
─────────────                                   ───────────
AgoraProvider (orchestration, credit-safe)
  ├─ agora_token.py   build short-lived RTC tokens (vendored AccessToken2)
  ├─ AgoraRestClient  POST /join  → agent_id   ───────────►  STT bots join channel
  │                    POST /agents/{id}/leave ───────────►  STT task stops
  └─ AgoraPcmMediaBridge (agora_bridge.py)
       AgoraServiceManager  one AgoraService per process
       RTC connection       join channel as AGORA_WORKER_UID
       publish PCM 10ms ────────────────────────────────►  bots transcribe
       on_stream_message ◄───────────────────────────────  captions (data stream)
       CaptionCollector     merge/dedup/order → transcript
```

Files:

- `worker/persephone_worker/providers/agora.py` — `AgoraConfig`, hardened
  `AgoraRestClient`, `AgoraProvider` (orchestration + credit guard), `MediaBridge`
  interface, `UnavailableMediaBridge` (default), `MockAgoraProvider`.
- `worker/persephone_worker/providers/agora_bridge.py` — real SDK adapter
  (`AgoraServiceManager`, `AgoraPcmMediaBridge`). All SDK imports are lazy.
- `worker/persephone_worker/providers/agora_token.py` — vendored official Agora
  **AccessToken2 / RtcTokenBuilder2** (MIT, stdlib-only).
- `worker/persephone_worker/providers/agora_captions.py` — caption decoding + merge.
- `worker/persephone_worker/agora_canary.py` — the guarded manual live-test tool.
- `worker/scripts/run_worker_agora.sh` — sets `LD_LIBRARY_PATH` and runs the worker/canary.

## 3. Prerequisites

- An **Agora project** with **App Certificate enabled**, and **Real-Time STT**
  enabled for the project (Agora console → your project → Features).
- Credentials (kept ONLY in `worker/.env`, never committed, never logged):
  `AGORA_APP_ID`, `AGORA_APP_CERTIFICATE`, `AGORA_CUSTOMER_ID`,
  `AGORA_CUSTOMER_SECRET`.
- **WSL2 Ubuntu** (or any glibc Linux). The native SDK does **not** run on Windows
  or on Vercel. Python **3.10+**.

## 4. Install the optional SDK

The base worker does not install Agora. In the worker's Linux venv:

```bash
cd worker
source .venv/bin/activate
pip install -r requirements-agora.txt        # agora-python-server-sdk>=2.4.9,<3.0
```

The native SDK ships `.so` libraries that must be on `LD_LIBRARY_PATH`. The helper
script derives that path from your venv (nothing hardcoded) — always start the
worker/canary through it when using Agora:

```bash
bash scripts/run_worker_agora.sh                 # runs run_worker.py with LD_LIBRARY_PATH set
```

If you prefer to set it yourself:

```bash
export LD_LIBRARY_PATH="$(python -c 'import sysconfig,os;print(os.path.join(sysconfig.get_paths()["purelib"],"agora","agora_sdk"))')${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
```

## 5. Configure (worker/.env)

```ini
# Secrets (worker-only; never commit/log)
AGORA_APP_ID=...
AGORA_APP_CERTIFICATE=...
AGORA_CUSTOMER_ID=...
AGORA_CUSTOMER_SECRET=...

# HARD master switch — live RTC/STT refuses to run unless this is true.
AGORA_LIVE_ENABLED=false

# RTC identities and token lifetime
AGORA_WORKER_UID=20001          # worker publishes badge audio as this UID
AGORA_SUB_BOT_UID=10001         # Agora STT subscriber bot
AGORA_PUB_BOT_UID=10002         # Agora STT publisher (caption) bot
AGORA_TOKEN_TTL_SECONDS=300

# Credit safety
AGORA_MAX_DURATION_SECONDS=12   # cap seconds of audio published per job
AGORA_IDLE_SECONDS=10           # REST maxIdleTime
AGORA_TIMEOUT=30                # overall provider timeout
AGORA_DAILY_MAX_JOBS=20         # max live Agora jobs per UTC day
AGORA_CANARY_MAX_SECONDS=3      # hard cap for the manual canary
```

Pick a mode (`TRANSCRIPTION_MODE`):

| Mode | Behaviour |
|------|-----------|
| `faster_whisper_only` *(default)* | Local only. **Zero** Agora imports/calls. |
| `faster_whisper_first` | Local; Agora only if local fails/empty (needs live enabled). |
| `agora_first` *(live demo)* | Agora first; **falls back to Faster-Whisper** on any Agora problem. |
| `agora_only` | Agora only; safe error, no fallback. |

## 6. Credit safety (how spend is prevented)

Live Agora **cannot** start unless ALL of these hold, checked in order **before**
any paid REST call:

1. `AGORA_LIVE_ENABLED=true` (else `agora_live_disabled`, recorded as *skipped*).
2. Credentials complete (else `agora_not_configured`).
3. Under the **daily job ceiling** `AGORA_DAILY_MAX_JOBS` (derived from
   `transcription_attempts` for the current UTC day; else `agora_credit_limit_reached`).
4. Audio is valid, non-empty, 16 kHz mono (else `agora_unsupported_audio` /
   `agora_empty_transcript`).
5. The media bridge **joins the channel and the publisher connects** — only then is
   the paid STT task started. The default `UnavailableMediaBridge` raises here, so if
   the SDK is missing the REST task never starts.

Other guards: audio is capped at `AGORA_MAX_DURATION_SECONDS`; the task has an
overall timeout; exactly one `leave` per successful `join`; no retries beyond a small
transient REST retry; no Agora calls from health checks, imports, or startup; and
**zero** Agora activity in `faster_whisper_only`.

## 7. Manual live test (the canary)

This is the **only** path that intentionally spends credit. Run it yourself — the
assistant will not. It refuses unless `--live` is passed, `AGORA_LIVE_ENABLED=true`,
credentials are complete, you type the exact phrase `SPEND AGORA CREDIT`, and the WAV
is within the canary cap.

```bash
# Full STT (spends a little credit):
bash scripts/run_worker_agora.sh -m persephone_worker.agora_canary --wav sample.wav --live --max-seconds 3

# RTC-only smoke (publishes PCM, does NOT start an STT task):
bash scripts/run_worker_agora.sh -m persephone_worker.agora_canary --wav sample.wav --live --rtc-only
```

The canary prints safe status (no secret values), joins one channel, optionally
starts one STT task, publishes ≤ 3 s, prints the transcript, cleans up, and reports
the approximate duration. It never prints tokens or secrets.

Make a 3-second 16 kHz mono WAV of real speech for `sample.wav` (silence yields an
empty transcript).

## 8. Go live for a demo, then turn it off

```ini
TRANSCRIPTION_MODE=agora_first
AGORA_LIVE_ENABLED=true
```
Start the worker via `bash scripts/run_worker_agora.sh`. **After the demo**, set
`AGORA_LIVE_ENABLED=false` (or `TRANSCRIPTION_MODE=faster_whisper_only`) so no further
credit can be spent. The dashboard shows a **⚠ Agora credit** warning whenever an
Agora mode is active.

## 9. Fallback behaviour

- `agora_first`: Agora is tried first; **any** failure — SDK missing, RTC join
  failure/timeout, REST error, publish failure, caption timeout, empty transcript, or
  the credit guard — falls back to Faster-Whisper. The dashboard records
  `provider_used=faster_whisper`, `fallback_used=true`.
- `faster_whisper_first`: local first; Agora only if local fails/empty.
- `agora_only`: no fallback; a safe error is recorded.
- `faster_whisper_only`: Agora is never constructed, imported, or called.

## 10. Troubleshooting (safe error codes)

Only these codes are stored/logged (never secrets/tokens):

| Code | Meaning / fix |
|------|---------------|
| `agora_not_configured` | Missing credentials in `worker/.env`. |
| `agora_live_disabled` | `AGORA_LIVE_ENABLED` is not true. |
| `agora_sdk_unavailable` | SDK not installed or `LD_LIBRARY_PATH` unset — use `scripts/run_worker_agora.sh`. |
| `agora_token_error` | Token build failed (check app id/certificate). |
| `agora_unsupported_audio` | Audio not 16 kHz mono PCM16. |
| `agora_rtc_join_failed` / `agora_rtc_join_timeout` | Could not join the channel — check token/UID/network/firewall. |
| `agora_stt_start_failed` | REST `/join` rejected — check Customer ID/Secret, STT enabled, and the join-body fields. |
| `agora_publish_failed` | PCM push rejected by the SDK. |
| `agora_caption_timeout` | No captions arrived — usually the caption data format or STT config (see §11). |
| `agora_empty_transcript` | Task ran but no words (silence / very short audio). |
| `agora_credit_limit_reached` | Daily ceiling hit; Faster-Whisper handles it where the mode allows. |
| `agora_cleanup_failed` | A teardown step failed (logged; never masks the real error). |

## 11. Live verification status

**Verified live on 2026-07-12** against a real Agora project with server SDK
**v4.4.32** (RTC). A 4.7 s speech clip transcribed end-to-end:

```
spoken:  "Hello. How does Persephone keep questions anonymous?"
result:  "Hello. How does Wisp keep questions anonymous?"   (provider=agora)
```

Confirmed by that run: AccessToken2 tokens are accepted, the worker joins the
channel and the publisher connects, real-time PCM publishing works, the REST
`/join` body is accepted (STT agent started), captions arrive and decode to a
transcript, `leave` stops the task, and the process shuts down cleanly.

Two integration fixes were needed and are in place:

- PCM frames are pushed as **`bytearray`** (the SDK's `send_audio_pcm_data` calls
  `ctypes.from_buffer`, which rejects immutable `bytes`).
- The process-wide `AgoraService` is **released before exit** (the native lib
  aborts at interpreter shutdown otherwise) — done by `Worker.shutdown()` and the
  canary's `finally`.

These two things can still vary by **account / SDK version**, and are isolated so
they're easy to adjust if you hit a different setup:

1. **REST join-body field spellings** — `AgoraRestClient.build_join_body`.
2. **Caption wire format / field numbers** — constants atop `agora_captions.py`
   (`ProtobufTextDecoder`), with a JSON fallback.

If either ever mismatches, the worker still **fails safe**: in `agora_first` /
`faster_whisper_first` it falls back to Faster-Whisper.

## 12. Security

- App Certificate and Customer Secret are **worker-only** — never sent to the badge
  or browser, never logged, never stored in Supabase, never committed.
- RTC tokens are short-lived and **never logged** in full; only safe metadata
  (channel, agent id, language, duration, SDK version) is logged/stored.
- The badge never talks to Agora; only the worker does.
- Faster-Whisper remains the offline fallback and the default.

Official references: <https://docs.agora.io/en/real-time-stt/overview/product-overview> ·
token builder: <https://github.com/AgoraIO/Tools/tree/master/DynamicKey/AgoraDynamicKey/python3> ·
server SDK + PCM example: <https://github.com/AgoraIO-Extensions/Agora-Python-Server-SDK>
