"""Rank fusion.

Fusion works on ranks rather than raw scores because pgvector cosine distances
and Postgres ts_rank_cd values are not comparable, and normalising them is
fragile. These tests pin the properties that follow from that choice.
"""

from __future__ import annotations

import pytest

from app.db.enums import ChunkType, DocumentStatus
from app.rag.fusion import reciprocal_rank_fusion
from app.rag.results import RetrievedChunk


def chunk(chunk_id: str, **overrides: object) -> RetrievedChunk:
    defaults: dict[str, object] = {
        "chunk_id": chunk_id,
        "body": f"body {chunk_id}",
        "chunk_type": ChunkType.PROSE,
        "document_id": "doc",
        "document_title": "Sign Bylaw No. 4451",
        "municipality_slug": "coquitlam",
        "municipality_name": "Coquitlam",
        "bylaw_number": "4451",
        "section_number": "5.3",
        "section_path": "Part 5 > 5.3",
        "section_heading": "Fascia Signs",
        "page_number": 22,
        "document_status": DocumentStatus.IN_FORCE,
    }
    defaults.update(overrides)
    return RetrievedChunk(**defaults)  # type: ignore[arg-type]


class TestBasicFusion:
    def test_single_list_preserves_order(self) -> None:
        dense = [chunk("a"), chunk("b"), chunk("c")]
        fused = reciprocal_rank_fusion(dense, [], k=60)
        assert [c.chunk_id for c in fused] == ["a", "b", "c"]

    def test_empty_inputs(self) -> None:
        assert reciprocal_rank_fusion([], []) == []

    def test_only_sparse(self) -> None:
        fused = reciprocal_rank_fusion([], [chunk("a"), chunk("b")])
        assert [c.chunk_id for c in fused] == ["a", "b"]

    def test_disjoint_lists_are_interleaved(self) -> None:
        fused = reciprocal_rank_fusion([chunk("a")], [chunk("b")])
        assert {c.chunk_id for c in fused} == {"a", "b"}


class TestAgreementWins:
    def test_agreed_result_beats_a_single_top_hit(self) -> None:
        # The core reason to fuse on ranks: a chunk both retrievers like is a
        # better citation than one only the embedding model liked.
        dense = [chunk("only-dense"), chunk("agreed"), chunk("x"), chunk("y")]
        sparse = [chunk("z"), chunk("agreed"), chunk("w"), chunk("v")]

        fused = reciprocal_rank_fusion(dense, sparse, k=10)
        assert fused[0].chunk_id == "agreed"

    def test_scores_from_both_retrievers_are_kept(self) -> None:
        dense = [chunk("a", dense_score=0.9)]
        sparse = [chunk("a", sparse_score=0.4)]
        fused = reciprocal_rank_fusion(dense, sparse)

        assert len(fused) == 1
        assert fused[0].dense_score == 0.9
        assert fused[0].sparse_score == 0.4

    def test_ranks_are_recorded_for_the_trace(self) -> None:
        dense = [chunk("x"), chunk("a")]
        sparse = [chunk("a")]
        fused = reciprocal_rank_fusion(dense, sparse)

        merged = next(c for c in fused if c.chunk_id == "a")
        assert merged.dense_rank == 2
        assert merged.sparse_rank == 1

    def test_unique_results_have_one_rank_only(self) -> None:
        fused = reciprocal_rank_fusion([chunk("a")], [chunk("b")])
        by_id = {c.chunk_id: c for c in fused}
        assert by_id["a"].sparse_rank is None
        assert by_id["b"].dense_rank is None


class TestWeighting:
    def test_dense_weighting_favours_dense_results(self) -> None:
        fused = reciprocal_rank_fusion(
            [chunk("dense-top")],
            [chunk("sparse-top")],
            dense_weight=0.9,
            sparse_weight=0.1,
        )
        assert fused[0].chunk_id == "dense-top"

    def test_sparse_weighting_favours_sparse_results(self) -> None:
        fused = reciprocal_rank_fusion(
            [chunk("dense-top")],
            [chunk("sparse-top")],
            dense_weight=0.1,
            sparse_weight=0.9,
        )
        assert fused[0].chunk_id == "sparse-top"

    def test_zero_dense_weight_still_includes_dense_results(self) -> None:
        # Weighted out of the ranking, but not silently dropped.
        fused = reciprocal_rank_fusion(
            [chunk("d")], [chunk("s")], dense_weight=0.0, sparse_weight=1.0
        )
        assert {c.chunk_id for c in fused} == {"d", "s"}

    def test_both_weights_zero_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one weight"):
            reciprocal_rank_fusion([chunk("a")], [], dense_weight=0, sparse_weight=0)


class TestSmoothingConstant:
    def test_larger_k_flattens_the_ranking(self) -> None:
        dense = [chunk(str(index)) for index in range(10)]
        tight = reciprocal_rank_fusion(dense, [], k=1)
        flat = reciprocal_rank_fusion(dense, [], k=1000)

        tight_gap = tight[0].fused_score - tight[1].fused_score
        flat_gap = flat[0].fused_score - flat[1].fused_score
        assert flat_gap < tight_gap

    def test_k_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="k must be"):
            reciprocal_rank_fusion([chunk("a")], [], k=0)


class TestDeterminismAndLimits:
    def test_ties_break_deterministically(self) -> None:
        # Two runs of the same query must return the same citations.
        dense = [chunk("b"), chunk("a")]
        first = reciprocal_rank_fusion(dense, [])
        second = reciprocal_rank_fusion(dense, [])
        assert [c.chunk_id for c in first] == [c.chunk_id for c in second]

    def test_limit_truncates(self) -> None:
        dense = [chunk(str(index)) for index in range(20)]
        assert len(reciprocal_rank_fusion(dense, [], limit=5)) == 5

    def test_limit_larger_than_input(self) -> None:
        assert len(reciprocal_rank_fusion([chunk("a")], [], limit=50)) == 1

    def test_scores_descend(self) -> None:
        dense = [chunk(str(index)) for index in range(10)]
        sparse = [chunk(str(index)) for index in range(5, 15)]
        fused = reciprocal_rank_fusion(dense, sparse)
        scores = [c.fused_score for c in fused]
        assert scores == sorted(scores, reverse=True)
