# Voice assistant — chatbot answers spoken through a Bluetooth speaker

Persephone can generate a short spoken answer to each anonymous question and read
it aloud on the host laptop. The answer is produced by a chatbot provider in the
WSL2 worker, stored in Supabase, delivered to the dashboard through the existing
HTTP polling, and spoken by the browser's Web Speech API — which plays through
whatever output device Windows is using (e.g. a paired Bluetooth speaker).

```text
ESP32-S3 badge records question
      → Persephone API stores the WAV (Vercel)
      → WSL2 worker transcribes it (Faster-Whisper or Agora)
      → chatbot provider generates a concise answer (WSL2 worker)
      → answer stored in Supabase (assistant_responses)
      → dashboard receives it via existing polling
      → browser Web Speech API speaks it
      → Windows plays it through the connected Bluetooth speaker
```

Two features are involved, and they are independent:

1. **Answer generation** — in the worker, selected by `CHATBOT_MODE`
   (`disabled | mock | ollama | openai_compatible`). Independent of
   `TRANSCRIPTION_MODE` and Agora.
2. **Speech output** — in the browser, using `window.speechSynthesis`. No cloud
   TTS, **no Agora credit**, no microphone access.

## Runtime boundaries

| Layer | Responsibility |
|---|---|
| Vercel (API + dashboard) | Serves the dashboard; exposes admin state + assistant actions. **Never** generates answers (heavy/slow work stays off the request path). |
| Supabase | Stores `assistant_responses` (one per question); atomic claim + reconciliation RPCs. |
| WSL2 worker | Transcribes **and** generates answers, as two separate async loops. A slow LLM never blocks transcription. |
| Browser (host laptop) | Speaks answers via Web Speech API; exactly-once dedup. |
| Windows | Routes the browser's audio to the selected output (Bluetooth speaker). |
| ESP32 firmware | **Unchanged.** Still records, uploads, polls, displays. No Bluetooth on the badge. |

Key independence guarantees:

- Browser TTS consumes **no Agora minutes**.
- LLM usage is independent of `TRANSCRIPTION_MODE`; Agora transcription selection
  is independent of chatbot selection.
- A chatbot failure never changes a transcribed question to `error`;
  transcription and clustering remain fully usable.

## 1. Enable answer generation (worker)

Edit `worker/.env` (copy from `worker/.env.example`). All chatbot config lives
here — **never** in frontend JS, Vercel config, firmware, Supabase rows, or git.

```dotenv
CHATBOT_MODE=ollama          # disabled | mock | ollama | openai_compatible
CHATBOT_AUTO_GENERATE=true
CHATBOT_MODEL=llama3.2:3b
CHATBOT_BASE_URL=http://localhost:11434
CHATBOT_API_KEY=             # only for hosted OpenAI-compatible endpoints
CHATBOT_TIMEOUT_SECONDS=30
CHATBOT_MAX_OUTPUT_TOKENS=180
CHATBOT_TEMPERATURE=0.3
CHATBOT_MAX_ATTEMPTS=3
```

- `disabled` (default): no chatbot work, **zero** chatbot network calls. The
  original Persephone workflow is completely unchanged.
- `mock`: a deterministic local answer. No network; good for tests and a
  credential-free end-to-end demo.
- `ollama`: a local Ollama model via its native `POST {BASE_URL}/api/chat`.
- `openai_compatible`: any OpenAI-compatible server via
  `POST {BASE_URL}/chat/completions` (`CHATBOT_API_KEY` sent as a Bearer token).

Restart the worker after changing `.env`:

```bash
cd worker && python run_worker.py
```

Safe startup logging reports `chatbot_mode`, `chatbot_model`, and
`chatbot_auto_generate`. The API key and Authorization headers are **never**
logged.

### Ollama setup

1. Install Ollama: <https://ollama.com/download> (Windows or Linux).
2. Pull a small model (3B is a good latency/quality balance for spoken answers):
   ```bash
   ollama pull llama3.2:3b
   ```
3. Confirm the endpoint works (native chat API, non-streaming):
   ```bash
   curl http://localhost:11434/api/chat -d '{
     "model": "llama3.2:3b",
     "messages": [{"role":"user","content":"Say hello in one sentence."}],
     "stream": false
   }'
   ```
   You should get a JSON body with `.message.content`.
4. Set `worker/.env` as shown above and restart the worker.

Ollama also exposes an **OpenAI-compatible** endpoint at
`{BASE_URL}/v1/chat/completions`. To use that instead, set
`CHATBOT_MODE=openai_compatible` and `CHATBOT_BASE_URL=http://localhost:11434/v1`.

#### Windows Ollama vs. WSL2 worker networking

The worker runs in WSL2; Ollama may run in WSL2 **or** on Windows.

- **Ollama inside WSL2**: `CHATBOT_BASE_URL=http://localhost:11434` works.
- **Ollama on Windows, worker in WSL2**: `localhost` inside WSL2 does **not**
  reliably reach the Windows host. Two options:
  1. **Mirrored networking** (Windows 11 22H2+): put `networkingMode=mirrored`
     under `[wsl2]` in `C:\Users\<you>\.wslconfig`, run `wsl --shutdown`, and then
     `localhost` reaches Windows services.
  2. **Explicit host IP** (works everywhere): start Ollama on Windows bound to all
     interfaces by setting the environment variable `OLLAMA_HOST=0.0.0.0` (then
     restart Ollama), and point the worker at the Windows host IP. Find that IP
     from inside WSL2:
     ```bash
     grep -m1 nameserver /etc/resolv.conf | awk '{print $2}'
     ```
     Then set, e.g. `CHATBOT_BASE_URL=http://172.20.0.1:11434` (use the IP you
     got). Verify with `curl http://<that-ip>:11434/api/tags` from WSL2.

### Verify answer generation

- **Without hardware** (mock): set `CHATBOT_MODE=mock`, ensure a `done` question
  with a transcript exists, and watch it get a `done` answer within a couple of
  seconds. Or use the dashboard **Generate answer** button on a question.
- **With a real model**: upload a WAV from the badge (or seed a done question),
  and the answer appears under the question in the dashboard.

## 2. Enable speech output (browser + Windows Bluetooth)

The website **cannot** reliably choose an output device itself — the Web Speech
API plays through the current Windows output. So you pick the speaker in Windows,
not in the page.

1. **Pair** the Bluetooth speaker in Windows (Settings → Bluetooth & devices).
2. Open **Windows Sound settings** and select the Bluetooth speaker as the
   **output** device.
3. Open the Persephone dashboard in **Chrome or Edge**.
4. **Log in** as the host.
5. In the **Voice output** panel (control rail), click **Enable voice**. Browsers
   block speech until a user interaction, so this click is required. You should
   immediately hear: *"Persephone voice output is ready."*
6. Click **Test voice** to confirm the speaker.
7. Keep the dashboard tab open during the event.

Voice-panel controls:

- **Enable / Disable voice** — master gate. Enabling establishes a *baseline*, so
  only answers completed **after** you enable are auto-spoken (the backlog is
  never read aloud). Disabling cancels the current utterance and clears the queue.
- **Auto-speak new answers** — toggle automatic playback. Re-enabling
  re-establishes the baseline.
- **Test voice** / **Stop** — sample playback / stop immediately.
- **Browser voice** + **Speaking rate** — saved as harmless preferences in
  `localStorage` (no tokens/passwords).
- Each completed answer has a **▶ Replay** button (may speak historical answers).

Exactly-once playback:

- Spoken-answer ids are tracked in `sessionStorage`, so **refreshing the tab does
  not re-read** the backlog.
- Polling and DOM re-rendering never repeat speech.
- Only newly **completed** answers are spoken — never queued/generating/failed.
- Multiple new answers are queued in completion order; a new answer does not
  cancel one already playing.

## Feedback-loop note (hackathon prototype)

The Bluetooth speaker may be audible to the badge microphone. For this phase:

- The dashboard shows a clear **"Persephone is speaking…"** state.
- The badge is **push-to-talk** — the attendee should release the badge button
  before the answer plays.
- Persephone does **not** access the laptop microphone, does not listen
  continuously, and creates at most **one** automatic answer per question.
- Acoustic echo cancellation is intentionally **not** implemented yet.

## Failure behaviour (everything degrades gracefully)

The original Q&A system keeps working if: no speaker is connected, the speaker
disconnects, speech synthesis is unavailable, voice output is disabled, Ollama
isn't running, the LLM times out, `CHATBOT_MODE=disabled`, the chatbot returns
empty output, a chatbot job fails, the worker restarts, the tab refreshes or is
hidden, Agora is disabled, or Faster-Whisper is selected. None of these break
transcription, clustering, dashboard questions, badge results, host controls, or
provider selection.

## Data model

`assistant_responses` (migration `003_voice_assistant.sql`) holds **one** answer
per question (`question_id` is unique). Statuses: `queued → generating → done`
(or `error`). Crash-safe:

- `enqueue_missing_assistant_responses()` — idempotent reconciliation ensures
  every done+transcribed question gets exactly one queued job, even if a worker
  died right after transcription.
- `claim_next_assistant_response()` — atomic `FOR UPDATE SKIP LOCKED` claim with
  lease reclamation and per-attempt counting (retry ceiling = `CHATBOT_MAX_ATTEMPTS`).
- `enqueue_assistant_response()` / `requeue_assistant_response()` — the idempotent
  primitives behind the host **Generate / Retry / Regenerate** actions.

No provider credentials are ever stored; only safe metadata (provider name,
model name, latency) and a public-safe error message.
