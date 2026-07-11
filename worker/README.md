# Persephone Transcription Worker

Runs in **WSL2 Ubuntu** (or any Linux host). It is **not** part of the Vercel
deployment — it stays running on a local/long-lived machine. The worker:

1. Claims queued questions from Supabase (atomic `FOR UPDATE SKIP LOCKED` +
   lease reclaim).
2. Downloads the private WAV from Supabase Storage.
3. Transcribes with the provider order chosen by `TRANSCRIPTION_MODE`
   (Faster-Whisper and/or Agora, with fallback).
4. Clusters the transcript by meaning (local embeddings).
5. Writes results + every provider attempt back to Postgres and sends heartbeats.

Only **outbound** connections are made — no inbound exposure is needed.

## Install (WSL2 / Linux)

```bash
cd worker
python3 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip
```

### CPU-only machines: install PyTorch's CPU wheel FIRST

`faster-whisper` and `sentence-transformers` pull in PyTorch. On a CPU-only box,
installing them directly can drag in **multi-GB CUDA** packages you don't need.
Install the CPU build of Torch first, then the rest:

```bash
python -m pip install --index-url https://download.pytorch.org/whl/cpu torch
pip install -r requirements.txt
```

(On a machine with a supported NVIDIA GPU + CUDA you can skip the first line and
set `FASTER_WHISPER_DEVICE=cuda`, `FASTER_WHISPER_COMPUTE_TYPE=float16`.)

## Configure

```bash
cp .env.example .env
# set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (service_role; server-side only)
```

Key settings (see `.env.example` for the full list):

| Var | Default | Notes |
|-----|---------|-------|
| `TRANSCRIPTION_MODE` | `faster_whisper_only` | `faster_whisper_only` / `faster_whisper_first` / `agora_first` / `agora_only` |
| `FASTER_WHISPER_MODEL` | **`small` recommended** | see below |
| `FASTER_WHISPER_DEVICE` | `cpu` | `cuda` if you have a GPU |
| `FASTER_WHISPER_COMPUTE_TYPE` | `int8` | `float16` on GPU |
| `TRANSCRIPTION_LANGUAGE` | `en` | |
| `CLUSTER_SIMILARITY_THRESHOLD` | `0.78` | raise to split more, lower to merge more |

### Model recommendation: use `small` for whispered speech

Badge audio is often **whispered/quiet**. In hardware testing the `base` model
was **insufficient** for whispered speech — it dropped or garbled words. Prefer:

```
FASTER_WHISPER_MODEL=small
```

**Tradeoff:** `small` is more accurate but noticeably slower on CPU than `base`
(roughly ~2–3× the transcription time for the same clip). On a modern laptop CPU
a short badge clip is still typically a few seconds. If you need lower latency and
your audio is spoken at normal volume, `base` is fine; for whispered speech,
`small` is worth the extra time. (`medium` is more accurate still but much slower
on CPU.)

## Run

```bash
python run_worker.py
```

The **first run downloads** the chosen Faster-Whisper model and the
`all-MiniLM-L6-v2` embedding model into the Hugging Face cache
(`~/.cache/huggingface`; override with `HF_HOME`). These are **not** committed to
git. Startup logs the model-loading status and the active transcription mode.

## Tests

The worker test suite uses fakes and dependency injection — it needs **none** of
the heavy ML packages and never downloads a model or contacts Agora/Supabase:

```bash
# from the repo root, using the API venv:
cd worker && ../.venv/bin/python -m pytest -q
```

Optional manual integration tests (real model / real Supabase) are gated behind
`PERSEPHONE_RUN_INTEGRATION=1` — see `tests/test_integration.py`.

## Agora (optional, credit-safe)

Agora is a real Real-Time STT provider that runs **only here in the worker** — it
publishes badge PCM into an RTC channel via the Linux-only `agora-python-server-sdk`
and reads captions back. It is **off by default** and never spends credit unless you
opt in.

```bash
# 1) install the optional SDK (Linux/WSL2 only)
pip install -r requirements-agora.txt
# 2) set credentials + AGORA_LIVE_ENABLED=true in worker/.env
# 3) run the worker with LD_LIBRARY_PATH set (helper derives it — nothing hardcoded)
bash scripts/run_worker_agora.sh
# 4) manual live-test before a demo (you type "SPEND AGORA CREDIT"):
bash scripts/run_worker_agora.sh -m persephone_worker.agora_canary --wav sample.wav --live --max-seconds 3
```

Credit safeguards: hard `AGORA_LIVE_ENABLED` switch, per-job duration cap, per-day
job ceiling, publisher-connected-before-STT ordering, and automatic Faster-Whisper
fallback in `agora_first`. Full details, error codes, and the remaining live-verify
notes are in [`../docs/AGORA_SETUP.md`](../docs/AGORA_SETUP.md). The full flow runs
on Faster-Whisper with no Agora setup.
