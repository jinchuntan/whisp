"""Semantic clustering with local sentence-transformer embeddings + cosine.

No paid LLM calls. The embedding model is injectable (``EmbeddingModel``
protocol) so tests use a deterministic fake and never download a model. The
similarity math is pure Python (no numpy dependency required).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("whisp.clustering")

DEFAULT_MODEL = "all-MiniLM-L6-v2"
DEFAULT_THRESHOLD = 0.78


@runtime_checkable
class EmbeddingModel(Protocol):
    def embed(self, text: str) -> list[float]: ...


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@dataclass
class ClusterCandidate:
    id: str
    embedding: list[float]


@dataclass
class ClusterDecision:
    embedding: list[float]
    matched_cluster_id: str | None
    similarity: float


class SentenceTransformerEmbedder:
    """Real embedder. ``sentence_transformers`` is imported lazily."""

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self.model_name = model_name
        self._model: Any = None

    def load(self) -> None:
        self._ensure()

    def _ensure(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            log.info("loading embedding model %s ...", self.model_name)
            self._model = SentenceTransformer(self.model_name)
            log.info("embedding model loaded")
        return self._model

    def embed(self, text: str) -> list[float]:
        model = self._ensure()
        vec = model.encode(text, normalize_embeddings=True)
        return [float(x) for x in vec]


class Clusterer:
    def __init__(self, embedder: EmbeddingModel, threshold: float = DEFAULT_THRESHOLD) -> None:
        self.embedder = embedder
        self.threshold = threshold

    def embed(self, text: str) -> list[float]:
        return self.embedder.embed(text)

    def decide(self, text: str, candidates: list[ClusterCandidate]) -> ClusterDecision:
        """Embed ``text`` and pick the nearest cluster at/above the threshold."""
        embedding = self.embed(text)
        best_id: str | None = None
        best_sim = 0.0
        for cand in candidates:
            sim = cosine_similarity(embedding, cand.embedding)
            if sim > best_sim:
                best_sim = sim
                best_id = cand.id
        if best_id is not None and best_sim >= self.threshold:
            return ClusterDecision(embedding, best_id, best_sim)
        return ClusterDecision(embedding, None, best_sim)
