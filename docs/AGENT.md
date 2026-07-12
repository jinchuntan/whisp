# Persephone — Agentic clustering

Persephone groups near-duplicate questions so the host sees what the room is really
asking. The grouping can run two ways:

- **`embedding_only`** (default) — a local `all-MiniLM-L6-v2` embedding + cosine
  similarity picks the nearest cluster over a threshold. Deterministic, offline, no
  LLM. This is Persephone's original clustering and is unchanged.
- **the clustering agent** — an LLM that **plans, calls tools, and decides**. The
  embedding/cosine code above is exposed to the agent *as a tool it can call*; the
  agent then commits to a clustering action. This is the agentic layer.

Both paths write clusters through the same queue methods and produce the same kind
of result (a question joins an existing cluster or opens a new one). The agent adds
judgement the pure-vector path can't: it can author a clean canonical label, refuse
to merge questions that only share keywords, and flag a question that isn't a
clusterable question at all.

The agent is **selected only by `CLUSTER_MODE`** and is **off by default** — with no
configuration Persephone behaves exactly as it did before this layer existed.

---

## The plan → tool-call → act loop

The agent runs **once per transcribed question**, synchronously, inside the worker's
existing clustering seam (`Worker._cluster`). One run is a bounded loop:

```
                       ┌─────────────────────────────────────────────┐
   transcript ───▶     │  system prompt + fenced (untrusted) question │
   (untrusted)         └───────────────────────┬─────────────────────┘
                                               │  POST /chat/completions
                                               ▼    (tools + tool_choice=auto)
                                   ┌───────────────────────┐
                            ┌────▶ │   LLM plans & decides  │ ─────┐
                            │      └───────────────────────┘      │  emits a tool call
        tool result (JSON)  │                                      ▼
                            │                       ┌──────────────────────────┐
                            └────────────────────── │  worker executes the tool │
                                                    └──────────────┬───────────┘
                                                                   │
                     search_similar_clusters ──► loop again        │ terminal tool
                     assign / create / flag  ──► done ◀────────────┘
```

1. The worker seeds the conversation with a system prompt (the task, the policy, the
   prompt-injection guard) and the transcribed question, **fenced as untrusted
   content**.
2. The model responds with a **tool call**. The worker executes it and appends the
   result to the conversation.
3. `search_similar_clusters` is a *read* tool → the loop continues so the model can
   act on what it found. `assign_to_cluster`, `create_cluster`, and `flag_question`
   are **terminal** → the loop ends and the worker applies the decision.

The transcript embedding is computed once (reusing the existing embedder) and is the
vector persisted for the question, so the agent and the embedding path store the same
data.

## The tools the agent can call

| Tool | Kind | What it does |
|------|------|--------------|
| `search_similar_clusters(query_text)` | read | Embeds `query_text` with the existing model and returns open clusters for the round ranked by cosine similarity: `[{cluster_id, canonical_question, similarity}]`. **This is the original embedding/cosine clustering, exposed as a tool.** |
| `assign_to_cluster(cluster_id, reason)` | terminal | Join an existing cluster. `cluster_id` **must** be one the search returned. |
| `create_cluster(canonical_question, reason)` | terminal | Open a new cluster and author its canonical label. |
| `flag_question(reason)` | terminal | Drop the question out of clustering. `reason ∈ {off_topic, abusive, not_a_question, unintelligible}`. Omitted entirely when `AGENT_ALLOW_FLAG=false`. |

Terminal decisions are applied with the **existing** queue methods
(`create_cluster`, `add_question_to_cluster`) — the agent changes *how* the decision
is made, not *how* it is written. A flagged question simply joins no cluster; there
is no schema or status change.

## Decision policy

- Prefer **assigning** to an existing cluster when the new question means essentially
  the same thing; **create** a new cluster otherwise. Do not merge questions that
  only share keywords.
- When creating, author a short, neutral canonical phrasing as the label (this is the
  text the host sees on the cluster card). It is length-capped.
- **Flag** only genuinely non-clusterable questions.
- `temperature = 0.0` — the same question and the same clusters give the same
  decision.

## Safety rails

The loop can never hang the worker and never writes something the model imagined:

- **Bounded loop** — at most `AGENT_MAX_TOOL_CALLS` tool calls (default 6) and a
  wall-clock deadline (`AGENT_TIMEOUT_SECONDS`). Reaching either without a terminal
  decision is an error → fallback.
- **No hallucinated writes** — a `cluster_id` the model returns is validated against
  the real candidate set. An unknown id is an error → fallback, never a bad write.
- **Capped label** — `canonical_question` is truncated to a safe length.
- **Untrusted transcript** — the question is fenced and the system prompt forbids
  treating it as instructions (the same guard wording the voice assistant uses).
- **No secret leakage** — the tool client never logs API keys, headers, or raw
  provider bodies; failures carry only a short safe message + a stable error code.

## Modes and the fallback ladder

`CLUSTER_MODE` mirrors `TRANSCRIPTION_MODE` / `CHATBOT_MODE`:

| `CLUSTER_MODE` | Behaviour |
|----------------|-----------|
| `embedding_only` *(default)* | Original cosine clustering. **Zero LLM calls.** Byte-identical to pre-agent Persephone. |
| `auto` | `agent_first` **if the agent is configured**, else `embedding_only`. With no credentials it is identical to `embedding_only`. |
| `agent_first` | The agent decides; on **any** failure, silently fall back to the embedding path. |
| `agent_only` | The agent decides with **no** fallback (for tests/demos). A failure logs and the question is left unclustered. |

In `agent_first`, the agent falls back to the deterministic embedding path — silently
to the caller, clearly in the logs — on any of:

- an exception or crash in the agent,
- a timeout / deadline,
- a malformed or unparseable tool call, or an unknown tool,
- a hallucinated `cluster_id`,
- the tool-call cap reached with no terminal decision,
- missing/invalid credentials.

This is the same "try, then fall back, and log why" pattern the transcription
provider router uses. Every agent run is logged like a provider attempt: mode, tool
calls made, the decision, whether it fell back and why, and latency.

## Providers

`AGENT_PROVIDER` selects the tool client:

- **`mock`** — a deterministic, **offline** agent loop (no network, no credit): it
  really runs the search → decide loop and picks assign/create, so you can demo the
  agent end-to-end without any API key.
- **`openai_compatible`** — any OpenAI-compatible `{AGENT_BASE_URL}/chat/completions`
  endpoint that supports tool calling (`AGENT_API_KEY` is sent as a Bearer token).
  Local models work too (e.g. an Ollama server's OpenAI-compatible endpoint), so the
  agent can run without paid API credit.

Any blank `AGENT_*` field inherits the matching `CHATBOT_*` value, so a worker that
already has a voice-assistant model configured can power the agent with just
`CLUSTER_MODE=agent_first`.

## Honest scope — what the agent does and doesn't decide

**It decides:** for each transcribed question, which cluster it belongs to — join an
existing one, open a new one (and its label), or flag it out of clustering — by
planning and calling tools.

**It does not decide, and does not touch:**

- **Transcription.** Speech-to-text is Faster-Whisper / Agora via the provider
  router. The agent only ever sees the resulting text.
- **Answer generation.** The optional voice assistant (`CHATBOT_MODE`) is separate.
- **The embedding math.** The agent *calls* the existing embedding/cosine code as a
  tool; it does not replace or modify it.
- **Anything when disabled.** In `embedding_only` (the default) the agent is never
  constructed and makes zero LLM calls.

The agent is an **additive** layer: it makes clustering smarter when enabled and
configured, and is a transparent no-op when it isn't.

## Where it lives

```
worker/persephone_worker/
  agent_llm.py      # sync OpenAI-compatible tool-calling client + deterministic mock
  cluster_agent.py  # the plan->tool->act loop, tools, safety rails, decision types
  worker.py         # Worker._cluster: mode gating, apply decision, embedding fallback
  clustering.py     # (unchanged) Clusterer + cosine — exposed to the agent as a tool
  queue.py          # (+ cluster_candidates_with_text) gives the agent cluster labels
worker/tests/
  test_cluster_agent.py   # offline: client shaping, the loop, every fallback trigger
```

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for how the agent sits inside the worker and
the [README](../README.md) for the mode tables.
