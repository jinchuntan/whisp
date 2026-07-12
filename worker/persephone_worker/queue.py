"""Worker-side Postgres + Storage gateway (Supabase, service_role).

Job claiming uses the atomic ``claim_next_question`` RPC (FOR UPDATE SKIP LOCKED
with lease reclamation). Cluster writes go through ``add_question_to_cluster``.
All methods are synchronous DB calls; the worker awaits only provider I/O.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from persephone_worker.clustering import ClusterCandidate
from persephone_worker.providers.base import AttemptRecord, TranscriptionResult

log = logging.getLogger("persephone.queue")


class JobQueue:
    def __init__(self, client: Any, bucket: str, worker_id: str) -> None:
        self._c = client
        self._bucket = bucket
        self.worker_id = worker_id

    # -- claim / lease ------------------------------------------------------
    def claim(self, lease_seconds: int) -> dict[str, Any] | None:
        res = self._c.rpc(
            "claim_next_question",
            {"p_worker_id": self.worker_id, "p_lease_seconds": lease_seconds},
        ).execute()
        rows = res.data or []
        return rows[0] if rows else None

    def renew_lease(self, question_id: str, lease_seconds: int) -> None:
        cutoff = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        self._c.table("questions").update({"lease_expires_at": cutoff}).eq(
            "id", question_id
        ).execute()

    def set_transcribing(self, question_id: str) -> None:
        self._c.table("questions").update({"status": "transcribing"}).eq(
            "id", question_id
        ).execute()

    def download_audio(self, object_path: str, dest: Any) -> Any:
        from persephone_worker.audio import download_audio

        return download_audio(self._c, self._bucket, object_path, dest)

    # -- results ------------------------------------------------------------
    def complete(self, question_id: str, result: TranscriptionResult, fallback_used: bool) -> None:
        self._c.table("questions").update(
            {
                "status": "done",
                "transcript": result.transcript,
                "language": result.language,
                "provider_used": result.provider,
                "fallback_used": fallback_used,
                "processing_ms": result.processing_ms,
            }
        ).eq("id", question_id).execute()

    def set_empty(self, question_id: str, safe_message: str = "No speech detected") -> None:
        self._c.table("questions").update(
            {"status": "empty", "safe_error_message": safe_message, "error_code": "empty"}
        ).eq("id", question_id).execute()

    def set_error(self, question_id: str, code: str | None, safe_message: str | None) -> None:
        self._c.table("questions").update(
            {
                "status": "error",
                "error_code": code or "error",
                "safe_error_message": safe_message or "Transcription unavailable",
            }
        ).eq("id", question_id).execute()

    def record_attempt(self, question_id: str, attempt: AttemptRecord) -> None:
        self._c.table("transcription_attempts").insert(
            {
                "question_id": question_id,
                "provider": attempt.provider,
                "attempt_order": attempt.attempt_order,
                "status": attempt.status,
                "latency_ms": attempt.latency_ms,
                "finished_at": "now()",
                "safe_error_code": attempt.safe_error_code,
                "safe_error_message": attempt.safe_error_message,
                "provider_metadata": attempt.provider_metadata or {},
            }
        ).execute()

    # -- clustering ---------------------------------------------------------
    def cluster_candidates(self, round_id: str) -> list[ClusterCandidate]:
        res = (
            self._c.table("clusters")
            .select("id,embedding")
            .eq("round_id", round_id)
            .eq("status", "open")
            .execute()
        )
        out = []
        for row in res.data or []:
            emb = row.get("embedding") or []
            out.append(ClusterCandidate(id=row["id"], embedding=[float(x) for x in emb]))
        return out

    def cluster_candidates_with_text(self, round_id: str) -> list[dict[str, Any]]:
        """Like :meth:`cluster_candidates` but also selects ``canonical_question`` so
        the clustering agent can reason over cluster TEXT, not just embeddings.

        Additive and read-only; the embedding path keeps using the lighter
        ``cluster_candidates`` (id + embedding only).
        """
        res = (
            self._c.table("clusters")
            .select("id,canonical_question,embedding")
            .eq("round_id", round_id)
            .eq("status", "open")
            .execute()
        )
        out: list[dict[str, Any]] = []
        for row in res.data or []:
            emb = row.get("embedding") or []
            out.append(
                {
                    "id": row["id"],
                    "canonical_question": row.get("canonical_question") or "",
                    "embedding": [float(x) for x in emb],
                }
            )
        return out

    def create_cluster(
        self, round_id: str, canonical: str, embedding: list[float]
    ) -> dict[str, Any]:
        res = (
            self._c.table("clusters")
            .insert(
                {
                    "round_id": round_id,
                    "canonical_question": canonical,
                    "question_count": 0,
                    "embedding": embedding,
                    "status": "open",
                }
            )
            .execute()
        )
        return res.data[0]

    def add_question_to_cluster(
        self,
        cluster_id: str,
        question_id: str,
        similarity: float,
        embedding: list[float] | None = None,
    ) -> int:
        res = self._c.rpc(
            "add_question_to_cluster",
            {
                "p_cluster_id": cluster_id,
                "p_question_id": question_id,
                "p_similarity": similarity,
                "p_embedding": embedding,
            },
        ).execute()
        # rpc returns the new member count (scalar).
        data = res.data
        if isinstance(data, list):
            return int(data[0]) if data else 0
        return int(data or 0)

    # -- recluster ----------------------------------------------------------
    def pending_recluster_rounds(self) -> list[dict[str, Any]]:
        res = (
            self._c.table("rounds").select("*").not_.is_("recluster_requested_at", "null").execute()
        )
        pending = []
        for r in res.data or []:
            requested = r.get("recluster_requested_at")
            done = r.get("reclustered_at")
            if requested and (not done or done < requested):
                pending.append(r)
        return pending

    def done_questions_for_round(self, round_id: str) -> list[dict[str, Any]]:
        res = (
            self._c.table("questions")
            .select("id,transcript,round_id,status")
            .eq("round_id", round_id)
            .eq("status", "done")
            .order("created_at")
            .execute()
        )
        return res.data or []

    def clear_clusters_for_round(self, round_id: str) -> None:
        self._c.table("questions").update({"cluster_id": None}).eq("round_id", round_id).execute()
        self._c.table("clusters").delete().eq("round_id", round_id).execute()

    def mark_reclustered(self, round_id: str) -> None:
        self._c.table("rounds").update({"reclustered_at": "now()"}).eq("id", round_id).execute()

    # -- credit guard -------------------------------------------------------
    def count_agora_jobs_today(self) -> int:
        """Count Agora attempts that actually ran today (UTC), for the daily cap.

        Excludes ``skipped`` attempts (those consumed no Agora credit). Derived
        from ``transcription_attempts`` so no extra table is needed. Any query
        failure returns 0 (the caller degrades safely — the other credit guards
        still apply).
        """
        start = (
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .isoformat()
        )
        try:
            res = (
                self._c.table("transcription_attempts")
                .select("id", count="exact")
                .eq("provider", "agora")
                .neq("status", "skipped")
                .gte("created_at", start)
                .execute()
            )
        except Exception:
            log.warning("count_agora_jobs_today query failed; treating as 0")
            return 0
        count = getattr(res, "count", None)
        if count is not None:
            return int(count)
        return len(res.data or [])

    # -- assistant responses (voice-assistant answers) ----------------------
    # A distinct, independent job table from questions. A failure here never
    # touches a transcribed question, so transcription/clustering stay usable.
    def reconcile_assistant_jobs(self, limit: int) -> int:
        """Idempotently enqueue missing answer jobs for done+transcribed questions.

        Crash-safe: this closes the gap where a worker transcribes a question but
        dies before enqueueing its answer job. Returns the number newly enqueued.
        """
        res = self._c.rpc("enqueue_missing_assistant_responses", {"p_limit": limit}).execute()
        data = res.data
        if isinstance(data, list):
            return int(data[0]) if data else 0
        return int(data or 0)

    def claim_assistant(self, lease_seconds: int) -> dict[str, Any] | None:
        res = self._c.rpc(
            "claim_next_assistant_response",
            {"p_worker_id": self.worker_id, "p_lease_seconds": lease_seconds},
        ).execute()
        rows = res.data or []
        return rows[0] if rows else None

    def load_assistant_context(self, question_id: str) -> dict[str, Any] | None:
        """Fetch the transcript + round/event context for an answer job.

        Returns None if the question is gone. Round prompt / event name are
        best-effort (None if unavailable) so the answer can still be generated.
        """
        qres = (
            self._c.table("questions")
            .select("id,transcript,round_id,event_id,status")
            .eq("id", question_id)
            .limit(1)
            .execute()
        )
        qrows = qres.data or []
        if not qrows:
            return None
        q = qrows[0]
        round_prompt: str | None = None
        if q.get("round_id"):
            rres = (
                self._c.table("rounds").select("prompt").eq("id", q["round_id"]).limit(1).execute()
            )
            rrows = rres.data or []
            round_prompt = (rrows[0].get("prompt") if rrows else None) or None
        event_name: str | None = None
        if q.get("event_id"):
            eres = self._c.table("events").select("name").eq("id", q["event_id"]).limit(1).execute()
            erows = eres.data or []
            event_name = (erows[0].get("name") if erows else None) or None
        return {
            "transcript": q.get("transcript") or "",
            "round_prompt": round_prompt,
            "event_name": event_name,
        }

    def complete_assistant(
        self,
        response_id: str,
        *,
        text: str,
        provider: str,
        model: str | None,
        processing_ms: int,
    ) -> None:
        self._c.table("assistant_responses").update(
            {
                "status": "done",
                "response_text": text,
                "provider": provider,
                "model": model,
                "processing_ms": processing_ms,
                "safe_error_message": None,
                "lease_expires_at": None,
                "completed_at": "now()",
            }
        ).eq("id", response_id).execute()

    def requeue_assistant(self, response_id: str) -> None:
        """Return a job to the queue for another attempt (recoverable failure)."""
        self._c.table("assistant_responses").update(
            {"status": "queued", "lease_expires_at": None}
        ).eq("id", response_id).execute()

    def fail_assistant(self, response_id: str, safe_message: str) -> None:
        """Mark a job terminally failed with a public-safe message (no internals)."""
        self._c.table("assistant_responses").update(
            {
                "status": "error",
                "safe_error_message": safe_message or "Assistant response unavailable",
                "lease_expires_at": None,
                "completed_at": "now()",
            }
        ).eq("id", response_id).execute()

    # -- heartbeat ----------------------------------------------------------
    def heartbeat(self, version: str, mode: str, status: str) -> None:
        self._c.table("worker_heartbeats").upsert(
            {
                "worker_id": self.worker_id,
                "version": version,
                "transcription_mode": mode,
                "status": status,
                "last_seen_at": "now()",
            },
            on_conflict="worker_id",
        ).execute()

    # -- retention ----------------------------------------------------------
    def cleanup_expired_audio(self, retention_hours: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=retention_hours)).isoformat()
        res = (
            self._c.table("questions")
            .select("id,audio_storage_path")
            .lt("created_at", cutoff)
            .not_.is_("audio_storage_path", "null")
            .limit(100)
            .execute()
        )
        rows = res.data or []
        removed = 0
        for row in rows:
            path = row.get("audio_storage_path")
            if not path:
                continue
            try:
                self._c.storage.from_(self._bucket).remove([path])
            except Exception:
                log.warning("failed to remove audio %s", path)
            # Deleting audio must NOT delete the transcript/question row.
            self._c.table("questions").update({"audio_storage_path": None}).eq(
                "id", row["id"]
            ).execute()
            removed += 1
        if removed:
            log.info("retention: removed %d expired audio object(s)", removed)
        return removed
