# Agora Real-Time Speech-to-Text — setup & the honest blocker

**TL;DR:** Whisp works end-to-end **without Agora** using Faster-Whisper (the
default). Agora is optional and, for our recorded-WAV use case, has a real
external blocker that we do **not** paper over. This document explains exactly
what is implemented, what is not, and why.

## What Agora RT-STT actually is

Agora Real-Time Speech-to-Text is **not** a "POST this WAV and get text" API. It
is an **RTC-channel** product:

1. You start a transcription task via REST (v7.x).
2. Agora spins up bot(s) that **join your RTC channel** and **subscribe to the
   audio** published there.
3. The transcript is delivered back through the channel data-stream (live
   captions) or written to cloud storage as WebVTT.

So to transcribe our pre-recorded badge WAV, **something must publish that audio
into an RTC channel** so Agora's bot can hear it. That "something" is a media
bridge built on Agora's **server SDK** (`agora-python-server-sdk`), which pushes
16-bit/16 kHz/mono PCM frames (10 ms/320-byte cadence) into the channel.

## What is implemented (verified, credit-safe)

`worker/whisp_worker/providers/agora.py`:

- **`AgoraRestClient`** — the v7.x control-plane, verified against Agora docs:
  - `POST https://api.agora.io/api/speech-to-text/v1/projects/{appId}/join` → `agent_id`
  - `GET  …/projects/{appId}/agents/{agent_id}` (query)
  - `POST …/projects/{appId}/agents/{agent_id}/leave` (stop)
  - HTTP **Basic auth**: `base64(AGORA_CUSTOMER_ID:AGORA_CUSTOMER_SECRET)`.
  - Makes **no** network calls at import or construction; unit-tested with a
    mocked transport.
- **`MediaBridge`** — the boundary that pushes PCM into the channel. The default
  is `UnavailableMediaBridge`, which raises with the documented reason.
- **`AgoraProvider`** — orchestrates start → publish → stop with cleanup in a
  `finally` block. Crucially, it **opens the media bridge first**; with the
  default (unavailable) bridge it raises **before** any REST task starts, so
  **no Agora credit is ever consumed** until a real bridge is supplied.
- **`MockAgoraProvider`** — deterministic, offline; used by tests and for a
  credential-free "agora" demo.

## The one external blocker (not fabricated)

To make Agora truly transcribe our WAV you must supply a real `MediaBridge`
backed by `agora-python-server-sdk`. That has hard prerequisites we cannot satisfy
in this environment without your credentials and credit:

1. **Linux/glibc only.** `agora-python-server-sdk` bundles native `.so` libs and
   does **not** support native Windows. Use **WSL2 Ubuntu**. You must
   `export LD_LIBRARY_PATH=<site-packages>/agora/agora_sdk/` before running.
2. **Real credentials + credit.** A live channel + STT task consumes your free
   allowance. We will not spend it without explicit permission.
3. **Unverified request-body field names.** Agora's v7 docs render as a
   client-side SPA; we verified the **endpoints and auth** but could **not**
   verify every JSON field spelling (`channelName` vs `channel`, exact nesting of
   `rtcConfig`/`captionConfig`/storage). The `start_task` body in
   `AgoraRestClient` uses best-effort field names annotated in code — **verify
   them against the current v7.x "join" reference before going live.**

Because of (1)–(3), Whisp ships the verified control-plane + a clean bridge
boundary + a mock, and documents this blocker — rather than pretending Agora is
working. This satisfies the requirement that the system never fakes Agora.

## Enabling Agora (when you have credentials)

1. In **WSL2 Ubuntu**, install the server SDK:
   ```bash
   cd worker
   # uncomment agora-python-server-sdk in requirements.txt, then:
   pip install -r requirements.txt
   export LD_LIBRARY_PATH="$(python -c 'import agora, os; print(os.path.dirname(agora.__file__))')/agora_sdk"
   ```
2. Set the Agora secrets in `worker/.env` (never commit them):
   ```
   AGORA_APP_ID=...
   AGORA_APP_CERTIFICATE=...
   AGORA_CUSTOMER_ID=...
   AGORA_CUSTOMER_SECRET=...
   ```
3. Implement a real `MediaBridge` (publish PCM frames at 10 ms cadence; collect
   the transcript from the channel data-stream or WebVTT) and pass a
   `bridge_factory` to `AgoraProvider`. Verify the `start_task` body fields.
4. Choose a mode that selects Agora:
   ```
   TRANSCRIPTION_MODE=agora_first   # Agora, then Faster-Whisper fallback
   # or agora_only (no fallback)
   ```
   The dashboard shows a **⚠ Agora mode — consumes credit** warning whenever an
   Agora mode is active.

## Security

- App Certificate and Customer Secret are **server/worker-only**; never sent to
  the badge or browser.
- Tokens and secrets are redacted from logs; provider responses are never logged
  verbatim (they may contain tokens). Only `agent_id`/`channel` are logged.

Official starting point: <https://docs.agora.io/en/real-time-stt/overview/product-overview>
