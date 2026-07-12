"""Agentic clustering layer: tool client, the plan->tool->act loop, and the
Worker wiring — all offline.

No network, no LLM, no credit: the OpenAI-compatible tool client uses an injected
synchronous transport, and the agent loop is driven by a scripted ``MockToolClient``.
The embedding fallback path reuses the deterministic ``FakeEmbedder`` from conftest.
No existing test is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from persephone_worker.agent_llm import (
    AGENT_LLM_BAD_RESPONSE,
    AGENT_LLM_HTTP_ERROR,
    AGENT_LLM_NOT_CONFIGURED,
    AgentLLMError,
    AgentLLMTimeout,
    AgentLLMUnavailable,
    LLMResponse,
    MockToolClient,
    OpenAICompatibleToolClient,
    ToolCall,
    _TransportError,
    _TransportTimeout,
)
from persephone_worker.cluster_agent import (
    AGENT_BAD_TOOL_CALL,
    AGENT_CAP_REACHED,
    AGENT_HALLUCINATED_ID,
    AGENT_NO_TOOL_CALL,
    AGENT_TIMEOUT,
    TOOL_ASSIGN,
    TOOL_CREATE,
    TOOL_FLAG,
    TOOL_SEARCH,
    AgentError,
    ClusterAgent,
    build_cluster_agent,
    build_tools,
)
from persephone_worker.clustering import ClusterCandidate, Clusterer
from persephone_worker.config import WorkerSettings
from persephone_worker.providers.router import FASTER_WHISPER
from persephone_worker.queue import JobQueue
from persephone_worker.worker import Worker
from tests.conftest import FakeEmbedder, FakeProvider, FakeSupabaseClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resp(name: str, args: dict[str, Any], cid: str = "call_1") -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id=cid, name=name, arguments=args)], finish_reason="tool_calls"
    )


def _search(query: str = "q") -> LLMResponse:
    return _resp(TOOL_SEARCH, {"query_text": query}, cid="call_s")


def _wire_tool_calls(name: str, arguments: str) -> dict[str, Any]:
    """An OpenAI-style response envelope carrying one tool call (arguments as the
    JSON *string* the wire format uses)."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


class RecordingTransport:
    """Synchronous transport spy: records calls, returns a canned payload or raises."""

    def __init__(self, response: dict | None = None, raises: Exception | None = None) -> None:
        self.response = response or {}
        self.raises = raises
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url: str, headers: dict, body: dict) -> dict:
        self.calls.append((url, headers, body))
        if self.raises is not None:
            raise self.raises
        return self.response


class FakeQueue:
    """Records the cluster writes the worker performs (no DB)."""

    def __init__(
        self,
        candidates: list[ClusterCandidate] | None = None,
        candidates_text: list[dict[str, Any]] | None = None,
    ) -> None:
        self.candidates = candidates or []
        self.candidates_text = candidates_text or []
        self.created_clusters: list[tuple[str, str, list]] = []
        self.added: list[tuple[str, str, float, Any]] = []

    def cluster_candidates(self, round_id: str) -> list[ClusterCandidate]:
        return self.candidates

    def cluster_candidates_with_text(self, round_id: str) -> list[dict[str, Any]]:
        return self.candidates_text

    def create_cluster(self, round_id: str, canonical: str, embedding: list) -> dict[str, Any]:
        cid = f"cluster-{len(self.created_clusters)}"
        self.created_clusters.append((cid, canonical, embedding))
        return {"id": cid}

    def add_question_to_cluster(self, cluster_id, qid, similarity, embedding=None) -> int:
        self.added.append((cluster_id, qid, similarity, embedding))
        return len(self.added)


def _settings(mode: str, **kw: Any) -> WorkerSettings:
    base: dict[str, Any] = {
        "transcription_mode": "faster_whisper_only",
        "enable_clustering": True,
        "cluster_mode": mode,
        "agent_provider": "mock",
    }
    base.update(kw)
    return WorkerSettings(**base)


def make_worker(
    mode: str,
    *,
    agent_client: Any = None,
    agent_max_tool_calls: int = 6,
    candidates: list[ClusterCandidate] | None = None,
    candidates_text: list[dict[str, Any]] | None = None,
    embed_table: dict[str, list[float]] | None = None,
    threshold: float = 0.78,
) -> tuple[Worker, FakeQueue, Clusterer]:
    q = FakeQueue(candidates=candidates, candidates_text=candidates_text)
    clusterer = Clusterer(FakeEmbedder(embed_table or {}), threshold=threshold)
    agent = None
    if agent_client is not None:
        agent = ClusterAgent(clusterer, agent_client, max_tool_calls=agent_max_tool_calls)
    w = Worker(
        _settings(mode),
        q,
        clusterer=clusterer,
        cluster_agent=agent,
        fw_provider=FakeProvider(FASTER_WHISPER),
    )
    return w, q, clusterer


# ===========================================================================
# Config
# ===========================================================================
def test_default_cluster_mode_is_embedding_only():
    s = WorkerSettings(cluster_mode="embedding_only")
    assert s.cluster_mode == "embedding_only"
    assert s.resolved_cluster_mode == "embedding_only"
    assert s.cluster_uses_agent is False


def test_cluster_mode_validated_and_normalised():
    assert WorkerSettings(cluster_mode="AGENT_FIRST").cluster_mode == "agent_first"
    with pytest.raises(ValueError):
        WorkerSettings(cluster_mode="do_magic")
    with pytest.raises(ValueError):
        WorkerSettings(agent_provider="anthropic_secret_sauce")


def test_auto_resolves_to_embedding_only_without_credentials():
    # No AGENT_* / CHATBOT_* creds => auto MUST behave exactly like today.
    s = WorkerSettings(
        cluster_mode="auto",
        agent_provider="openai_compatible",
        agent_base_url="",
        agent_model="",
        chatbot_base_url="",
        chatbot_model="",
    )
    assert s.agent_configured is False
    assert s.resolved_cluster_mode == "embedding_only"


def test_auto_resolves_to_agent_first_when_configured_via_chatbot_fallback():
    s = WorkerSettings(
        cluster_mode="auto",
        agent_provider="openai_compatible",
        chatbot_base_url="https://api.example.com/v1",
        chatbot_model="gpt-x",
    )
    assert s.resolved_agent_base_url == "https://api.example.com/v1"
    assert s.resolved_agent_model == "gpt-x"
    assert s.agent_configured is True
    assert s.resolved_cluster_mode == "agent_first"


def test_mock_provider_is_always_configured():
    assert WorkerSettings(cluster_mode="auto", agent_provider="mock").resolved_cluster_mode == (
        "agent_first"
    )


# ===========================================================================
# Tool schema
# ===========================================================================
def test_build_tools_includes_flag_only_when_allowed():
    with_flag = build_tools(allow_flag=True)
    names = {t["function"]["name"] for t in with_flag}
    assert names == {TOOL_SEARCH, TOOL_ASSIGN, TOOL_CREATE, TOOL_FLAG}
    flag = next(t for t in with_flag if t["function"]["name"] == TOOL_FLAG)
    assert "enum" in flag["function"]["parameters"]["properties"]["reason"]

    without = build_tools(allow_flag=False)
    assert TOOL_FLAG not in {t["function"]["name"] for t in without}


# ===========================================================================
# OpenAI-compatible tool client (real request/response shaping, offline)
# ===========================================================================
def test_tool_client_request_shape_auth_and_parse():
    tx = RecordingTransport(_wire_tool_calls(TOOL_ASSIGN, '{"cluster_id": "c1"}'))
    client = OpenAICompatibleToolClient(
        base_url="https://api.example.com/v1",
        model="gpt-x",
        api_key="secret-key",
        temperature=0.0,
        transport=tx,
    )
    resp = client.chat([{"role": "user", "content": "hi"}], build_tools(allow_flag=True))
    url, headers, body = tx.calls[0]
    assert url == "https://api.example.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer secret-key"
    assert body["model"] == "gpt-x"
    assert body["temperature"] == 0.0
    assert body["stream"] is False
    assert body["tool_choice"] == "auto"
    assert isinstance(body["tools"], list) and body["tools"]
    # The tool call is parsed and its JSON-string arguments are decoded to a dict.
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == TOOL_ASSIGN
    assert resp.tool_calls[0].arguments == {"cluster_id": "c1"}


def test_tool_client_no_auth_header_without_key():
    tx = RecordingTransport(_wire_tool_calls(TOOL_CREATE, '{"canonical_question": "Q?"}'))
    client = OpenAICompatibleToolClient(base_url="http://x/v1", model="m", transport=tx)
    client.chat([{"role": "user", "content": "q"}], build_tools(allow_flag=False))
    assert "Authorization" not in tx.calls[0][1]


def test_tool_client_not_configured_raises():
    client = OpenAICompatibleToolClient(base_url="", model="", transport=RecordingTransport())
    with pytest.raises(AgentLLMUnavailable) as ei:
        client.chat([], build_tools(allow_flag=True))
    assert ei.value.code == AGENT_LLM_NOT_CONFIGURED


def test_tool_client_timeout_maps():
    tx = RecordingTransport(raises=_TransportTimeout("slow"))
    client = OpenAICompatibleToolClient(base_url="http://x/v1", model="m", transport=tx)
    with pytest.raises(AgentLLMTimeout):
        client.chat([], build_tools(allow_flag=True))


def test_tool_client_http_error_is_sanitised():
    tx = RecordingTransport(raises=_TransportError("status 500: secret body"))
    client = OpenAICompatibleToolClient(base_url="http://x/v1", model="m", transport=tx)
    with pytest.raises(AgentLLMError) as ei:
        client.chat([], build_tools(allow_flag=True))
    assert ei.value.code == AGENT_LLM_HTTP_ERROR
    assert "secret body" not in ei.value.safe_message


def test_tool_client_malformed_arguments_raise_bad_response():
    tx = RecordingTransport(_wire_tool_calls(TOOL_ASSIGN, "{not valid json"))
    client = OpenAICompatibleToolClient(base_url="http://x/v1", model="m", transport=tx)
    with pytest.raises(AgentLLMError) as ei:
        client.chat([], build_tools(allow_flag=True))
    assert ei.value.code == AGENT_LLM_BAD_RESPONSE


# ===========================================================================
# ClusterAgent loop (scripted, deterministic)
# ===========================================================================
def _agent(
    script=None,
    *,
    raises=None,
    fallback=None,
    embed_table=None,
    threshold=0.78,
    max_tool_calls=6,
    allow_flag=True,
) -> tuple[ClusterAgent, MockToolClient]:
    client = MockToolClient(script=script, raises=raises, fallback=fallback)
    clusterer = Clusterer(FakeEmbedder(embed_table or {}), threshold=threshold)
    agent = ClusterAgent(clusterer, client, max_tool_calls=max_tool_calls, allow_flag=allow_flag)
    return agent, client


def test_agent_assigns_to_existing_cluster():
    table = {"How can AI help?": [0.99, 0.14, 0.0]}
    candidates = [
        {"id": "c1", "canonical_question": "How does AI help?", "embedding": [1.0, 0.0, 0.0]}
    ]
    agent, client = _agent(
        script=[_search("How can AI help?"), _resp(TOOL_ASSIGN, {"cluster_id": "c1"})],
        embed_table=table,
    )
    decision = agent.decide("How can AI help?", candidates)
    assert decision.action == "assign"
    assert decision.cluster_id == "c1"
    assert decision.similarity >= 0.78
    assert decision.embedding == [0.99, 0.14, 0.0]
    assert decision.tool_calls == 2
    assert client.calls == 2


def test_agent_creates_new_cluster_with_its_own_label():
    agent, _ = _agent(
        script=[
            _search("brand new"),
            _resp(TOOL_CREATE, {"canonical_question": "How can AI help people at work?"}),
        ],
        embed_table={"brand new topic": [1.0, 0.0]},
    )
    decision = agent.decide("brand new topic", [])
    assert decision.action == "create"
    assert decision.canonical_question == "How can AI help people at work?"
    assert decision.similarity == 1.0
    assert decision.embedding == [1.0, 0.0]


def test_agent_create_caps_canonical_length():
    long_label = "x" * 500
    agent, _ = _agent(script=[_resp(TOOL_CREATE, {"canonical_question": long_label})])
    decision = agent.decide("some question", [])
    assert decision.action == "create"
    assert len(decision.canonical_question) <= 200


def test_agent_flag_path():
    agent, _ = _agent(script=[_resp(TOOL_FLAG, {"reason": "off_topic"})])
    decision = agent.decide("buy my product now", [])
    assert decision.action == "flag"
    assert decision.flag_reason == "off_topic"


def test_agent_flag_with_bad_reason_is_error():
    agent, _ = _agent(script=[_resp(TOOL_FLAG, {"reason": "because_i_said_so"})])
    with pytest.raises(AgentError) as ei:
        agent.decide("q", [])
    assert ei.value.code == AGENT_BAD_TOOL_CALL


def test_agent_flag_disabled_is_error():
    agent, _ = _agent(script=[_resp(TOOL_FLAG, {"reason": "off_topic"})], allow_flag=False)
    with pytest.raises(AgentError) as ei:
        agent.decide("q", [])
    assert ei.value.code == AGENT_BAD_TOOL_CALL


def test_agent_hallucinated_cluster_id_is_error():
    candidates = [{"id": "c1", "canonical_question": "Q", "embedding": [0.0, 1.0, 0.0]}]
    agent, _ = _agent(
        script=[_search("x"), _resp(TOOL_ASSIGN, {"cluster_id": "ghost-999"})],
        embed_table={"x": [1.0, 0.0, 0.0]},
    )
    with pytest.raises(AgentError) as ei:
        agent.decide("x", candidates)
    assert ei.value.code == AGENT_HALLUCINATED_ID


def test_agent_unknown_tool_is_error():
    agent, _ = _agent(script=[_resp("drop_all_tables", {})])
    with pytest.raises(AgentError) as ei:
        agent.decide("q", [])
    assert ei.value.code == AGENT_BAD_TOOL_CALL


def test_agent_no_tool_call_is_error():
    # Empty script => the mock returns a plain text turn with no tool call.
    agent, _ = _agent(script=[])
    with pytest.raises(AgentError) as ei:
        agent.decide("q", [])
    assert ei.value.code == AGENT_NO_TOOL_CALL


def test_agent_timeout_from_client_becomes_agent_timeout():
    agent, _ = _agent(raises=AgentLLMTimeout())
    with pytest.raises(AgentError) as ei:
        agent.decide("q", [])
    assert ei.value.code == AGENT_TIMEOUT


def test_agent_wraps_arbitrary_exception():
    agent, _ = _agent(raises=RuntimeError("boom"))
    with pytest.raises(AgentError):
        agent.decide("q", [])


def test_agent_loop_terminates_at_cap_and_does_not_hang():
    # A model that only ever searches must terminate at the cap, not loop forever.
    agent, client = _agent(fallback=_search("again"), max_tool_calls=3, embed_table={"q": [1.0]})
    with pytest.raises(AgentError) as ei:
        agent.decide("q", [{"id": "c1", "canonical_question": "c", "embedding": [1.0]}])
    assert ei.value.code == AGENT_CAP_REACHED
    assert client.calls == 3  # exactly the cap; the loop is bounded


def test_agent_search_ranks_and_caps_candidates():
    agent, _ = _agent(
        script=[_search("q"), _resp(TOOL_CREATE, {"canonical_question": "new"})],
        embed_table={"q": [1.0, 0.0, 0.0]},
        max_tool_calls=4,
    )
    candidates = [
        {"id": "far", "canonical_question": "f", "embedding": [0.0, 1.0, 0.0]},
        {"id": "near", "canonical_question": "n", "embedding": [1.0, 0.0, 0.0]},
    ]
    # We can't read the tool result directly, but the run must complete using search.
    decision = agent.decide("q", candidates)
    assert decision.action == "create"


# ===========================================================================
# Offline reactive mock (AGENT_PROVIDER=mock) — a real agent with no network
# ===========================================================================
def test_build_cluster_agent_returns_none_for_embedding_only():
    s = _settings("embedding_only")
    assert build_cluster_agent(s, Clusterer(FakeEmbedder())) is None


def test_offline_mock_agent_creates_then_assigns():
    s = _settings("agent_first", agent_provider="mock")
    clusterer = Clusterer(FakeEmbedder({"How can AI help?": [1.0, 0.0, 0.0]}), threshold=0.78)
    agent = build_cluster_agent(s, clusterer)
    assert agent is not None

    # No candidates -> the mock creates a labelled cluster from the question.
    created = agent.decide("How can AI help?", [])
    assert created.action == "create"
    assert "AI" in (created.canonical_question or "")

    # An identical existing cluster -> the mock assigns to it.
    candidates = [
        {"id": "c1", "canonical_question": "How can AI help?", "embedding": [1.0, 0.0, 0.0]}
    ]
    assigned = agent.decide("How can AI help?", candidates)
    assert assigned.action == "assign"
    assert assigned.cluster_id == "c1"


# ===========================================================================
# Worker wiring: mode gating + fallback ladder + applying decisions
# ===========================================================================
def test_embedding_only_makes_zero_llm_calls_and_matches_today():
    client = MockToolClient(script=[_resp(TOOL_CREATE, {"canonical_question": "AGENT LABEL"})])
    w, q, _ = make_worker("embedding_only", agent_client=client, embed_table={"hello": [1.0, 0.0]})
    w._cluster("r1", "q1", "hello")
    # The agent was never consulted...
    assert client.calls == 0
    # ...and the result is byte-identical to today: a new cluster labelled with the
    # raw transcript, joined at similarity 1.0.
    assert q.created_clusters == [("cluster-0", "hello", [1.0, 0.0])]
    assert q.added == [("cluster-0", "q1", 1.0, [1.0, 0.0])]


def test_agent_first_applies_assign_via_existing_queue_methods():
    candidates_text = [
        {"id": "c1", "canonical_question": "How does AI help?", "embedding": [1.0, 0.0, 0.0]}
    ]
    client = MockToolClient(
        script=[_search("How can AI help?"), _resp(TOOL_ASSIGN, {"cluster_id": "c1"})]
    )
    w, q, _ = make_worker(
        "agent_first",
        agent_client=client,
        candidates_text=candidates_text,
        embed_table={"How can AI help?": [0.99, 0.14, 0.0]},
    )
    w._cluster("r1", "q9", "How can AI help?")
    assert q.created_clusters == []  # joined, not created
    assert len(q.added) == 1
    cid, qid, sim, _emb = q.added[0]
    assert cid == "c1" and qid == "q9" and sim >= 0.78


def test_agent_first_applies_create_with_agent_label():
    client = MockToolClient(
        script=[_search("q"), _resp(TOOL_CREATE, {"canonical_question": "Agent authored label?"})]
    )
    w, q, _ = make_worker("agent_first", agent_client=client, embed_table={"raw transcript": [1.0]})
    w._cluster("r1", "q1", "raw transcript")
    assert len(q.created_clusters) == 1
    cid, canonical, emb = q.created_clusters[0]
    # The cluster is labelled by the AGENT, not with the raw transcript.
    assert canonical == "Agent authored label?"
    assert emb == [1.0]
    assert q.added == [(cid, "q1", 1.0, [1.0])]


def test_agent_first_flag_does_not_corrupt_counts():
    client = MockToolClient(script=[_resp(TOOL_FLAG, {"reason": "abusive"})])
    w, q, _ = make_worker("agent_first", agent_client=client, embed_table={"nonsense": [1.0]})
    w._cluster("r1", "q1", "nonsense")
    # A flagged question joins nothing: no cluster row, no membership, counts intact.
    assert q.created_clusters == []
    assert q.added == []


@pytest.mark.parametrize(
    "client",
    [
        MockToolClient(raises=RuntimeError("boom")),
        MockToolClient(raises=AgentLLMTimeout()),
        MockToolClient(script=[_resp("unknown_tool", {})]),
        MockToolClient(script=[_search("x"), _resp(TOOL_ASSIGN, {"cluster_id": "ghost"})]),
    ],
    ids=["exception", "timeout", "malformed", "hallucinated_id"],
)
def test_agent_first_falls_back_to_embedding_path(client):
    candidates_text = [{"id": "c1", "canonical_question": "Q", "embedding": [0.0, 1.0, 0.0]}]
    w, q, _ = make_worker(
        "agent_first",
        agent_client=client,
        candidates_text=candidates_text,
        candidates=[ClusterCandidate(id="c1", embedding=[0.0, 1.0, 0.0])],
        embed_table={"hello world": [1.0, 0.0, 0.0]},
    )
    w._cluster("r1", "q1", "hello world")
    # Fallback = today's embedding path: transcript is orthogonal to c1 -> new cluster
    # labelled with the raw transcript (NOT an agent label, NOT a corrupt write).
    assert q.created_clusters == [("cluster-0", "hello world", [1.0, 0.0, 0.0])]
    assert q.added == [("cluster-0", "q1", 1.0, [1.0, 0.0, 0.0])]


def test_agent_first_falls_back_on_tool_cap():
    client = MockToolClient(fallback=_search("again"))  # never terminal
    w, q, _ = make_worker(
        "agent_first",
        agent_client=client,
        agent_max_tool_calls=3,
        embed_table={"hello": [1.0, 0.0]},
    )
    w._cluster("r1", "q1", "hello")
    assert client.calls == 3  # loop bounded by the cap
    assert q.created_clusters == [("cluster-0", "hello", [1.0, 0.0])]  # embedding fallback ran


def test_agent_only_does_not_fall_back():
    client = MockToolClient(raises=RuntimeError("boom"))
    w, q, _ = make_worker("agent_only", agent_client=client, embed_table={"hello": [1.0]})
    with pytest.raises(AgentError):
        w._cluster("r1", "q1", "hello")
    # No fallback: nothing was written to the queue.
    assert q.created_clusters == []
    assert q.added == []


# ===========================================================================
# Queue: the one additive method
# ===========================================================================
def test_cluster_candidates_with_text_selects_label():
    client = FakeSupabaseClient()
    client.select_returns["clusters"] = [
        {"id": "c1", "canonical_question": "How does AI help?", "embedding": [1.0, 0.0]},
        {"id": "c2", "canonical_question": "", "embedding": []},
    ]
    q = JobQueue(client, "bucket", "worker-1")
    rows = q.cluster_candidates_with_text("r1")
    assert rows[0] == {
        "id": "c1",
        "canonical_question": "How does AI help?",
        "embedding": [1.0, 0.0],
    }
    assert rows[1]["canonical_question"] == ""
    assert rows[1]["embedding"] == []
