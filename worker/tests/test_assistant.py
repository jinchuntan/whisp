"""AssistantProcessor: reconcile → claim → generate → store, with retries,
lease recovery, retry ceiling, and isolation from transcription.

Uses a bespoke in-memory fake queue (no Supabase, no network). The chatbot
provider is a controllable fake (no LLM, no credit).
"""

from __future__ import annotations

from typing import Any

from persephone_worker.assistant import AssistantProcessor
from persephone_worker.chatbot import ChatbotContext, ChatbotResult, ChatbotUnavailable
from persephone_worker.chatbot.base import EmptyAnswer
from persephone_worker.config import WorkerSettings


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeChatProvider:
    name = "fake"

    def __init__(
        self, *, text: str = "A concise spoken answer.", raises: Exception | None = None
    ) -> None:
        self._text = text
        self._raises = raises
        self.calls = 0

    async def generate(self, ctx: ChatbotContext) -> ChatbotResult:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return ChatbotResult(text=self._text, provider=self.name, model="m", processing_ms=7)


class FakeAssistantQueue:
    """One assistant_responses row store + a linked question context."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.contexts: dict[str, dict[str, Any]] = {}
        self.reconcile_calls = 0
        self.reconcile_limit: int | None = None
        self._claim_order: list[str] = []

    # -- seed helpers --
    def seed_job(self, rid: str, qid: str, *, transcript: str = "What is AI?", **row: Any) -> None:
        base = {
            "id": rid,
            "question_id": qid,
            "status": "queued",
            "attempt_count": 0,
            "lease_expires_at": None,
        }
        base.update(row)
        self.rows[rid] = base
        self.contexts[qid] = {"transcript": transcript, "round_prompt": "AI", "event_name": "Conf"}
        if rid not in self._claim_order:
            self._claim_order.append(rid)

    # -- queue surface used by AssistantProcessor --
    def reconcile_assistant_jobs(self, limit: int) -> int:
        self.reconcile_calls += 1
        self.reconcile_limit = limit
        return 0

    def claim_assistant(self, lease_seconds: int) -> dict[str, Any] | None:
        # Claim the oldest queued row, or a generating row whose lease expired.
        for rid in self._claim_order:
            row = self.rows[rid]
            claimable = row["status"] == "queued" or (
                row["status"] == "generating" and row.get("lease_expires_at") == "EXPIRED"
            )
            if claimable:
                row["status"] = "generating"
                row["attempt_count"] += 1
                row["lease_expires_at"] = "LEASED"
                return dict(row)
        return None

    def load_assistant_context(self, question_id: str) -> dict[str, Any] | None:
        return self.contexts.get(question_id)

    def complete_assistant(self, response_id, *, text, provider, model, processing_ms) -> None:
        self.rows[response_id].update(
            status="done",
            response_text=text,
            provider=provider,
            model=model,
            processing_ms=processing_ms,
            lease_expires_at=None,
        )

    def requeue_assistant(self, response_id: str) -> None:
        self.rows[response_id].update(status="queued", lease_expires_at=None)

    def fail_assistant(self, response_id: str, safe_message: str) -> None:
        self.rows[response_id].update(
            status="error", safe_error_message=safe_message, lease_expires_at=None
        )


def settings(**kw: Any) -> WorkerSettings:
    base: dict[str, Any] = {"chatbot_mode": "mock", "chatbot_max_attempts": 3}
    base.update(kw)
    return WorkerSettings(**base)


def make(provider: Any, **kw: Any) -> tuple[AssistantProcessor, FakeAssistantQueue]:
    q = FakeAssistantQueue()
    return AssistantProcessor(settings(**kw), q, provider), q


# ---------------------------------------------------------------------------
# Disabled mode
# ---------------------------------------------------------------------------
async def test_disabled_mode_does_nothing():
    # provider None (disabled) -> no claims, no reconcile, no network.
    p, q = make(None, chatbot_mode="disabled")
    assert p.enabled is False
    assert await p.process_one() is False
    p.maybe_reconcile()
    assert q.reconcile_calls == 0


# ---------------------------------------------------------------------------
# Happy path + one response per question
# ---------------------------------------------------------------------------
async def test_generates_and_stores_answer():
    p, q = make(FakeChatProvider(text="AI can help summarise talks."))
    q.seed_job("r1", "q1")
    assert await p.process_one() is True
    row = q.rows["r1"]
    assert row["status"] == "done"
    assert row["response_text"] == "AI can help summarise talks."
    assert row["provider"] == "fake"


async def test_no_second_job_when_none_queued():
    p, q = make(FakeChatProvider())
    q.seed_job("r1", "q1")
    await p.process_one()  # done
    assert await p.process_one() is False  # nothing left to claim


# ---------------------------------------------------------------------------
# Reconcile idempotency (auto-generate gate)
# ---------------------------------------------------------------------------
async def test_reconcile_only_when_auto_generate():
    p, q = make(
        FakeChatProvider(), chatbot_auto_generate=True, chatbot_reconcile_interval_seconds=0
    )
    p.maybe_reconcile()
    assert q.reconcile_calls == 1
    assert q.reconcile_limit == p.settings.chatbot_reconcile_limit

    p2, q2 = make(FakeChatProvider(), chatbot_auto_generate=False)
    p2.maybe_reconcile()
    assert q2.reconcile_calls == 0


# ---------------------------------------------------------------------------
# Retries + retry ceiling
# ---------------------------------------------------------------------------
async def test_recoverable_failure_requeues_until_ceiling_then_errors():
    p, q = make(FakeChatProvider(raises=ChatbotUnavailable("down")), chatbot_max_attempts=3)
    q.seed_job("r1", "q1")
    # attempt 1 -> requeue, 2 -> requeue, 3 -> error (terminal)
    await p.process_one()
    assert q.rows["r1"]["status"] == "queued" and q.rows["r1"]["attempt_count"] == 1
    await p.process_one()
    assert q.rows["r1"]["status"] == "queued" and q.rows["r1"]["attempt_count"] == 2
    await p.process_one()
    assert q.rows["r1"]["status"] == "error" and q.rows["r1"]["attempt_count"] == 3
    assert q.rows["r1"]["safe_error_message"] == "Assistant unavailable"


async def test_empty_answer_is_retryable_then_terminal():
    p, q = make(FakeChatProvider(raises=EmptyAnswer()), chatbot_max_attempts=1)
    q.seed_job("r1", "q1")
    await p.process_one()  # attempt 1 == max -> error
    assert q.rows["r1"]["status"] == "error"
    assert q.rows["r1"]["safe_error_message"] == "No answer generated"


async def test_error_message_is_safe_not_internal():
    p, q = make(FakeChatProvider(raises=RuntimeError("stack trace: secret at 0xdeadbeef")))
    p.settings.__dict__["chatbot_max_attempts"] = 1
    q.seed_job("r1", "q1")
    await p.process_one()
    msg = q.rows["r1"]["safe_error_message"]
    assert "secret" not in msg and "0xdead" not in msg
    assert msg == "Assistant response failed"


# ---------------------------------------------------------------------------
# Lease recovery (crashed worker) + crash-loop ceiling
# ---------------------------------------------------------------------------
async def test_expired_lease_is_reclaimed():
    p, q = make(FakeChatProvider(text="recovered answer."))
    # A row left 'generating' by a crashed worker, lease expired.
    q.seed_job("r1", "q1", status="generating", attempt_count=1, lease_expires_at="EXPIRED")
    assert await p.process_one() is True
    assert q.rows["r1"]["status"] == "done"


async def test_crash_looped_row_past_ceiling_is_failed_without_calling_provider():
    prov = FakeChatProvider()
    p, q = make(prov, chatbot_max_attempts=3)
    # attempt_count already at ceiling; reclaim bumps to 4 (> max) -> immediate error.
    q.seed_job("r1", "q1", status="generating", attempt_count=3, lease_expires_at="EXPIRED")
    await p.process_one()
    assert q.rows["r1"]["status"] == "error"
    assert prov.calls == 0  # provider never invoked


# ---------------------------------------------------------------------------
# No transcript -> terminal, harmless
# ---------------------------------------------------------------------------
async def test_missing_transcript_fails_safely():
    p, q = make(FakeChatProvider())
    q.seed_job("r1", "q1", transcript="")
    await p.process_one()
    assert q.rows["r1"]["status"] == "error"
    assert q.rows["r1"]["safe_error_message"] == "No transcript to answer"
