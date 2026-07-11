"""Voice-assistant answer processor: reconcile → claim → generate → store.

Runs as its OWN asyncio task alongside the transcription loop (see run_worker),
so a slow LLM never blocks transcription, never extends a transcription lease,
and a chatbot failure never changes a transcribed question's status. Chatbot
selection is driven only by ``CHATBOT_MODE`` and is fully independent of
``TRANSCRIPTION_MODE``/Agora.

When ``CHATBOT_MODE=disabled`` the provider is None and this processor does
nothing at all (no DB calls, no network).
"""

from __future__ import annotations

import asyncio
import logging
import time

from persephone_worker.chatbot import (
    ChatbotContext,
    ChatbotError,
    ChatbotProvider,
    EmptyAnswer,
)
from persephone_worker.config import WorkerSettings
from persephone_worker.queue import JobQueue

log = logging.getLogger("persephone.assistant")


class AssistantProcessor:
    def __init__(
        self,
        settings: WorkerSettings,
        queue: JobQueue,
        provider: ChatbotProvider | None,
    ) -> None:
        self.settings = settings
        self.q = queue
        self.provider = provider
        self._last_reconcile = 0.0

    @property
    def enabled(self) -> bool:
        return self.provider is not None and self.settings.chatbot_enabled

    # -- startup ------------------------------------------------------------
    def startup(self) -> None:
        """Safe startup logging. NEVER logs the API key or auth headers."""
        s = self.settings
        if not self.enabled:
            log.info("chatbot disabled (CHATBOT_MODE=%s) — no answer jobs will run", s.chatbot_mode)
            return
        log.info(
            "chatbot enabled: mode=%s model=%s auto_generate=%s timeout=%ss max_attempts=%s",
            s.chatbot_mode,
            s.chatbot_model or "(default)",
            s.chatbot_auto_generate,
            s.chatbot_timeout_seconds,
            s.chatbot_max_attempts,
        )

    # -- reconciliation -----------------------------------------------------
    def maybe_reconcile(self) -> None:
        """Periodically ensure every done+transcribed question has an answer job.

        Crash-safe: independent of the in-memory path, so an answer job is created
        even if a worker died right after transcription. Only runs when chatbot
        generation AND auto-generate are enabled.
        """
        if not (self.enabled and self.settings.chatbot_auto_generate):
            return
        now = time.monotonic()
        if now - self._last_reconcile < self.settings.chatbot_reconcile_interval_seconds:
            return
        self._last_reconcile = now
        try:
            n = self.q.reconcile_assistant_jobs(self.settings.chatbot_reconcile_limit)
            if n:
                log.info("reconcile: enqueued %d missing assistant response(s)", n)
        except Exception:
            log.warning("assistant reconcile failed (will retry)")

    # -- one job ------------------------------------------------------------
    async def process_one(self) -> bool:
        if not self.enabled:
            return False
        assert self.provider is not None

        job = self.q.claim_assistant(self.settings.chatbot_lease_seconds)
        if not job:
            return False

        rid = job["id"]
        qid = job.get("question_id")
        attempts = int(job.get("attempt_count") or 1)

        # Retry ceiling: a crash-looped row (attempt_count past the ceiling) is
        # failed rather than retried forever.
        if attempts > self.settings.chatbot_max_attempts:
            self.q.fail_assistant(rid, "Assistant response unavailable")
            log.warning("assistant job %s exhausted retries -> error", rid)
            return True

        ctx_row = self.q.load_assistant_context(qid)
        if ctx_row is None or not (ctx_row.get("transcript") or "").strip():
            # No transcript to answer — terminal, but harmless to the question.
            self.q.fail_assistant(rid, "No transcript to answer")
            return True

        ctx = ChatbotContext(
            transcript=ctx_row["transcript"],
            round_prompt=ctx_row.get("round_prompt"),
            event_name=ctx_row.get("event_name"),
        )

        try:
            result = await asyncio.wait_for(
                self.provider.generate(ctx), timeout=self.settings.chatbot_timeout_seconds
            )
        except TimeoutError:
            self._handle_failure(rid, attempts, "Assistant timed out")
            return True
        except EmptyAnswer:
            self._handle_failure(rid, attempts, "No answer generated")
            return True
        except ChatbotError as exc:
            # Only the pre-approved safe message is stored/logged — never internals.
            self._handle_failure(rid, attempts, exc.safe_message)
            return True
        except Exception:
            log.exception("assistant job %s unexpected error", rid)
            self._handle_failure(rid, attempts, "Assistant response failed")
            return True

        text = (result.text or "").strip()
        if not text:
            self._handle_failure(rid, attempts, "No answer generated")
            return True

        self.q.complete_assistant(
            rid,
            text=text,
            provider=result.provider,
            model=result.model,
            processing_ms=result.processing_ms,
        )
        log.info(
            "assistant done response=%s question=%s provider=%s %dms",
            rid,
            qid,
            result.provider,
            result.processing_ms,
        )
        return True

    def _handle_failure(self, response_id: str, attempts: int, safe_message: str) -> None:
        """Requeue recoverable failures until the retry ceiling, then fail safe."""
        if attempts < self.settings.chatbot_max_attempts:
            self.q.requeue_assistant(response_id)
            log.info(
                "assistant job %s failed (attempt %d/%d) -> requeued: %s",
                response_id,
                attempts,
                self.settings.chatbot_max_attempts,
                safe_message,
            )
        else:
            self.q.fail_assistant(response_id, safe_message)
            log.warning(
                "assistant job %s failed (attempt %d/%d) -> error: %s",
                response_id,
                attempts,
                self.settings.chatbot_max_attempts,
                safe_message,
            )

    # -- run ----------------------------------------------------------------
    async def run_forever(self) -> None:
        self.startup()
        while True:
            self.maybe_reconcile()
            processed = False
            try:
                processed = await self.process_one()
            except Exception:
                log.exception("assistant processing loop error")
            if not processed:
                await asyncio.sleep(self.settings.chatbot_poll_interval_seconds)
