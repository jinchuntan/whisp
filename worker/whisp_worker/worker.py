"""Worker main loop: heartbeat, claim, transcribe, cluster, retention.

Assembles the provider router from settings (Agora is only constructed when the
mode selects it), then processes one job at a time. Designed to run in WSL2.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
import time
from pathlib import Path
from typing import Any

from whisp_worker import __version__
from whisp_worker.clustering import Clusterer, SentenceTransformerEmbedder
from whisp_worker.config import WorkerSettings
from whisp_worker.providers.agora import AgoraConfig, AgoraProvider
from whisp_worker.providers.faster_whisper import FasterWhisperProvider
from whisp_worker.providers.router import AGORA, FASTER_WHISPER, MODE_ORDER, ProviderRouter
from whisp_worker.queue import JobQueue

log = logging.getLogger("whisp.worker")


class Worker:
    def __init__(
        self,
        settings: WorkerSettings,
        queue: JobQueue,
        *,
        clusterer: Clusterer | None = None,
        fw_provider: FasterWhisperProvider | None = None,
        agora_provider: AgoraProvider | None = None,
    ) -> None:
        self.settings = settings
        self.q = queue
        self._fw: FasterWhisperProvider | None = None
        self._agora: AgoraProvider | None = None

        factories: dict[str, Any] = {}
        if settings.uses_faster_whisper:
            self._fw = fw_provider or FasterWhisperProvider(
                model_name=settings.faster_whisper_model,
                device=settings.faster_whisper_device,
                compute_type=settings.faster_whisper_compute_type,
                language=settings.transcription_language,
                beam_size=settings.faster_whisper_beam_size,
            )
            factories[FASTER_WHISPER] = lambda: self._fw
        if settings.uses_agora:
            self._agora = agora_provider or AgoraProvider(
                AgoraConfig(
                    app_id=settings.agora_app_id,
                    app_certificate=settings.agora_app_certificate,
                    customer_id=settings.agora_customer_id,
                    customer_secret=settings.agora_customer_secret,
                    max_duration_seconds=settings.agora_max_duration_seconds,
                    idle_seconds=settings.agora_idle_seconds,
                ),
                language=settings.transcription_language,
            )
            factories[AGORA] = lambda: self._agora

        self.router = ProviderRouter(
            settings.transcription_mode,
            factories,
            timeouts={
                FASTER_WHISPER: settings.faster_whisper_timeout,
                AGORA: settings.agora_timeout,
            },
        )

        if clusterer is not None:
            self.clusterer: Clusterer | None = clusterer
        elif settings.enable_clustering:
            self.clusterer = Clusterer(
                SentenceTransformerEmbedder(settings.embedding_model),
                threshold=settings.cluster_similarity_threshold,
            )
        else:
            self.clusterer = None

        self._last_heartbeat = 0.0
        self._last_retention = 0.0

    # -- startup ------------------------------------------------------------
    def startup(self) -> None:
        order = MODE_ORDER[self.settings.transcription_mode]
        log.info("worker %s v%s starting", self.settings.resolved_worker_id, __version__)
        log.info("transcription_mode=%s provider_order=%s", self.settings.transcription_mode, order)
        if self.settings.uses_agora:
            log.warning("Agora is in the provider order — this mode CAN consume Agora credit.")
        if self._fw is not None:
            log.info("preloading Faster-Whisper model ...")
            try:
                self._fw.load()
            except Exception:
                log.exception("Faster-Whisper preload failed (will retry per-job)")
        if self.clusterer is not None and isinstance(
            self.clusterer.embedder, SentenceTransformerEmbedder
        ):
            log.info("preloading embedding model ...")
            try:
                self.clusterer.embedder.load()
            except Exception:
                log.exception("embedding model preload failed (will retry per-job)")

    # -- one job ------------------------------------------------------------
    async def process_one(self) -> bool:
        job = self.q.claim(self.settings.lease_seconds)
        if not job:
            return False

        qid = job["id"]
        round_id = job.get("round_id")
        object_path = job.get("audio_storage_path")
        log.info("claimed question id=%s badge=%s round=%s", qid, job.get("badge_id"), round_id)
        self.q.set_transcribing(qid)

        tmp = Path(tempfile.gettempdir()) / f"whisp-{qid}.wav"
        try:
            if not object_path:
                self.q.set_error(qid, "no_audio", "No audio for question")
                return True
            try:
                self.q.download_audio(object_path, tmp)
            except Exception:
                log.exception("download failed for question=%s", qid)
                self.q.set_error(qid, "download_failed", "Audio unavailable")
                return True

            outcome = await self.router.transcribe(tmp, qid)
            for attempt in outcome.attempts:
                try:
                    self.q.record_attempt(qid, attempt)
                except Exception:
                    log.warning("failed to record attempt for question=%s", qid)

            if outcome.status == "done" and outcome.result is not None:
                self.q.complete(qid, outcome.result, outcome.fallback_used)
                log.info(
                    "done question=%s provider=%s fallback=%s %dms",
                    qid,
                    outcome.provider_used,
                    outcome.fallback_used,
                    outcome.result.processing_ms,
                )
                if self.clusterer is not None and round_id:
                    try:
                        self._cluster(round_id, qid, outcome.result.transcript)
                    except Exception:
                        log.exception("clustering failed for question=%s", qid)
            elif outcome.status == "empty":
                self.q.set_empty(qid, outcome.safe_error_message or "No speech detected")
                log.info("empty question=%s", qid)
            else:
                self.q.set_error(qid, outcome.error_code, outcome.safe_error_message)
                log.warning("error question=%s code=%s", qid, outcome.error_code)
            return True
        finally:
            with contextlib.suppress(Exception):
                tmp.unlink(missing_ok=True)

    def _cluster(self, round_id: str, question_id: str, transcript: str) -> None:
        assert self.clusterer is not None
        candidates = self.q.cluster_candidates(round_id)
        decision = self.clusterer.decide(transcript, candidates)
        if decision.matched_cluster_id:
            count = self.q.add_question_to_cluster(
                decision.matched_cluster_id, question_id, decision.similarity
            )
            log.info(
                "cluster join question=%s cluster=%s sim=%.3f count=%d",
                question_id,
                decision.matched_cluster_id,
                decision.similarity,
                count,
            )
        else:
            cluster = self.q.create_cluster(round_id, transcript, decision.embedding)
            count = self.q.add_question_to_cluster(
                cluster["id"], question_id, 1.0, decision.embedding
            )
            log.info(
                "cluster new question=%s cluster=%s count=%d", question_id, cluster["id"], count
            )

    # -- recluster ----------------------------------------------------------
    def handle_reclusters(self) -> None:
        if self.clusterer is None:
            return
        for rnd in self.q.pending_recluster_rounds():
            round_id = rnd["id"]
            log.info("reclustering round=%s", round_id)
            self.q.clear_clusters_for_round(round_id)
            for q in self.q.done_questions_for_round(round_id):
                transcript = (q.get("transcript") or "").strip()
                if transcript:
                    try:
                        self._cluster(round_id, q["id"], transcript)
                    except Exception:
                        log.exception("recluster failed for question=%s", q["id"])
            self.q.mark_reclustered(round_id)

    # -- heartbeat / retention ---------------------------------------------
    def maybe_heartbeat(self, status: str) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat >= self.settings.heartbeat_interval_seconds:
            try:
                self.q.heartbeat(__version__, self.settings.transcription_mode, status)
            except Exception:
                log.warning("heartbeat failed")
            self._last_heartbeat = now

    def maybe_retention(self) -> None:
        now = time.monotonic()
        if now - self._last_retention >= self.settings.retention_interval_seconds:
            try:
                self.q.cleanup_expired_audio(self.settings.audio_retention_hours)
            except Exception:
                log.warning("retention sweep failed")
            self._last_retention = now

    # -- run ----------------------------------------------------------------
    async def run_forever(self) -> None:
        self.startup()
        self.maybe_heartbeat("idle")
        while True:
            self.maybe_heartbeat("idle")
            self.maybe_retention()
            try:
                self.handle_reclusters()
            except Exception:
                log.exception("recluster handling failed")

            processed = False
            try:
                self.maybe_heartbeat("busy")
                processed = await self.process_one()
            except Exception:
                log.exception("job processing failed")

            if not processed:
                await asyncio.sleep(self.settings.poll_interval_seconds)
