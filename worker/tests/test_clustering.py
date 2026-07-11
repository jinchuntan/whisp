"""Clustering: cosine similarity and threshold-based assignment."""

from __future__ import annotations

from tests.conftest import FakeEmbedder
from whisp_worker.clustering import ClusterCandidate, Clusterer, cosine_similarity


def test_cosine_identical():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0


def test_cosine_orthogonal():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_handles_zero_vector():
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_mismatched_length():
    assert cosine_similarity([1.0], [1.0, 0.0]) == 0.0


def test_decide_creates_new_cluster_when_no_candidates():
    clusterer = Clusterer(FakeEmbedder({"hello": [1.0, 0.0, 0.0]}), threshold=0.78)
    decision = clusterer.decide("hello", [])
    assert decision.matched_cluster_id is None
    assert decision.embedding == [1.0, 0.0, 0.0]


def test_decide_joins_cluster_above_threshold():
    table = {
        "How does AI help?": [1.0, 0.0, 0.0],
        "How can AI help?": [0.99, 0.14, 0.0],  # ~0.99 cosine -> above 0.78
    }
    clusterer = Clusterer(FakeEmbedder(table), threshold=0.78)
    candidates = [ClusterCandidate(id="c1", embedding=table["How does AI help?"])]
    decision = clusterer.decide("How can AI help?", candidates)
    assert decision.matched_cluster_id == "c1"
    assert decision.similarity >= 0.78


def test_decide_new_cluster_below_threshold():
    table = {
        "cats": [1.0, 0.0, 0.0],
        "quantum physics": [0.0, 1.0, 0.0],  # orthogonal -> below threshold
    }
    clusterer = Clusterer(FakeEmbedder(table), threshold=0.78)
    candidates = [ClusterCandidate(id="c1", embedding=table["cats"])]
    decision = clusterer.decide("quantum physics", candidates)
    assert decision.matched_cluster_id is None


def test_decide_picks_nearest_of_several():
    table = {
        "q": [1.0, 0.0, 0.0],
        "near": [0.95, 0.31, 0.0],
        "far": [0.2, 0.98, 0.0],
    }
    clusterer = Clusterer(FakeEmbedder(table), threshold=0.5)
    candidates = [
        ClusterCandidate(id="far", embedding=table["far"]),
        ClusterCandidate(id="near", embedding=table["near"]),
    ]
    decision = clusterer.decide("q", candidates)
    assert decision.matched_cluster_id == "near"
