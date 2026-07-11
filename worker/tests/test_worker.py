"""Worker.process_one end-to-end against a fake queue + fake providers.

No Supabase, no models, no Agora.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.conftest import FakeEmbedder, FakeProvider
from whisp_worker.clustering import Clusterer
from whisp_worker.config import WorkerSettings
from whisp_worker.providers.router import AGORA, FASTER_WHISPER
from whisp_worker.worker import Worker


class FakeQueue:
    def __init__(self, job: dict[str, Any] | None) -> None:
        self._job = job
        self.transcribing: list[str] = []
        self.completed: list[tuple[str, Any, bool]] = []
        self.empties: list[str] = []
        self.errors: list[tuple[str, Any, Any]] = []
        self.attempts: list[Any] = []
        self.created_clusters: list[tuple[str, str]] = []
        self.added: list[tuple[str, str, float]] = []
        self.candidates_ret: list = []

    def claim(self, lease_seconds: int):
        job, self._job = self._job, None
        return job

    def set_transcribing(self, qid: str) -> None:
        self.transcribing.append(qid)

    def download_audio(self, object_path: str, dest: Path):
        dest.write_bytes(b"\x00")  # fake providers ignore the content
        return dest

    def record_attempt(self, qid: str, attempt: Any) -> None:
        self.attempts.append(attempt)

    def complete(self, qid: str, result: Any, fallback_used: bool) -> None:
        self.completed.append((qid, result, fallback_used))

    def set_empty(self, qid: str, msg: str = "No speech detected") -> None:
        self.empties.append(qid)

    def set_error(self, qid: str, code, msg) -> None:
        self.errors.append((qid, code, msg))

    def cluster_candidates(self, round_id: str):
        return self.candidates_ret

    def create_cluster(self, round_id: str, canonical: str, embedding):
        cid = f"cluster-{len(self.created_clusters)}"
        self.created_clusters.append((cid, canonical))
        return {"id": cid}

    def add_question_to_cluster(self, cluster_id, qid, similarity, embedding=None):
        self.added.append((cluster_id, qid, similarity))
        return len(self.added)


def fw_only_settings(**kw) -> WorkerSettings:
    base = {"transcription_mode": "faster_whisper_only", "enable_clustering": True}
    base.update(kw)
    return WorkerSettings(**base)


def make_worker(job, provider, *, clusterer=None) -> tuple[Worker, FakeQueue]:
    q = FakeQueue(job)
    w = Worker(
        fw_only_settings(),
        q,
        clusterer=clusterer or Clusterer(FakeEmbedder(), threshold=0.78),
        fw_provider=provider,
    )
    return w, q


async def test_process_one_no_job():
    w, q = make_worker(None, FakeProvider(FASTER_WHISPER))
    assert await w.process_one() is False


async def test_process_one_done_and_clusters():
    job = {"id": "q1", "round_id": "r1", "audio_storage_path": "b/q1.wav", "badge_id": "b1"}
    w, q = make_worker(job, FakeProvider(FASTER_WHISPER, transcript="How can AI help?"))
    assert await w.process_one() is True
    assert q.transcribing == ["q1"]
    assert len(q.completed) == 1
    qid, result, fallback = q.completed[0]
    assert qid == "q1" and result.transcript == "How can AI help?" and fallback is False
    # First question in a round -> a new cluster is created and joined.
    assert len(q.created_clusters) == 1
    assert q.added[0][1] == "q1"


async def test_process_one_empty():
    job = {"id": "q2", "round_id": "r1", "audio_storage_path": "b/q2.wav", "badge_id": "b1"}
    w, q = make_worker(job, FakeProvider(FASTER_WHISPER, empty=True))
    await w.process_one()
    assert q.empties == ["q2"]
    assert q.completed == []


async def test_process_one_error_records_attempt():
    from whisp_worker.providers.base import ProviderError

    job = {"id": "q3", "round_id": None, "audio_storage_path": "b/q3.wav", "badge_id": "b1"}
    w, q = make_worker(job, FakeProvider(FASTER_WHISPER, raises=ProviderError("boom")))
    await w.process_one()
    assert len(q.errors) == 1
    assert len(q.attempts) == 1


async def test_process_one_missing_audio_path():
    job = {"id": "q4", "round_id": None, "audio_storage_path": None, "badge_id": "b1"}
    w, q = make_worker(job, FakeProvider(FASTER_WHISPER))
    await w.process_one()
    assert q.errors and q.errors[0][1] == "no_audio"


def test_worker_fw_only_does_not_build_agora():
    w = Worker(
        fw_only_settings(),
        FakeQueue(None),
        clusterer=Clusterer(FakeEmbedder()),
        fw_provider=FakeProvider(FASTER_WHISPER),
    )
    assert w._agora is None
    assert AGORA not in w.router.factories
    assert FASTER_WHISPER in w.router.factories


def test_worker_agora_first_builds_both():
    settings = WorkerSettings(
        transcription_mode="agora_first",
        enable_clustering=False,
        agora_app_id="A",
        agora_customer_id="c",
        agora_customer_secret="s",
    )
    w = Worker(settings, FakeQueue(None), fw_provider=FakeProvider(FASTER_WHISPER))
    assert w._agora is not None
    assert AGORA in w.router.factories and FASTER_WHISPER in w.router.factories
