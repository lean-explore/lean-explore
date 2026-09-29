"""Tests for the pure ranking stages."""

import json

import pytest

from lean_explore.models import Declaration
from lean_explore.search.ranking import (
    RerankWeights,
    apply_dependency_boost,
    combine_rerank_scores,
    count_dependents,
    fuse_rankings,
    informal_bm25_scores,
    parse_dependencies,
    rerank_document,
)


def declaration(
    declaration_id: int,
    name: str,
    dependencies: list[str] | str | None = None,
    informalization: str | None = None,
) -> Declaration:
    """Build an unsaved Declaration for ranking tests."""
    if isinstance(dependencies, list):
        dependencies = json.dumps(dependencies)
    return Declaration(
        id=declaration_id,
        name=name,
        module="Test",
        source_text="",
        source_link="",
        dependencies=dependencies,
        informalization=informalization,
    )


class TestFuseRankings:
    """Tests for reciprocal rank fusion."""

    def test_scores_are_sums_of_reciprocal_ranks(self):
        """A candidate first in both rankings scores 1/1 + 1/1."""
        fused = dict(fuse_rankings({1: 9.0, 2: 5.0}, {1: 0.9, 2: 0.1}))

        assert fused == {1: 2.0, 2: 1.0}

    def test_missing_candidate_takes_rank_past_the_end(self):
        """A candidate absent from one ranking gets that ranking's length + 1."""
        fused = dict(fuse_rankings({1: 9.0, 2: 5.0}, {3: 0.8}))

        assert fused[1] == pytest.approx(1 / 1 + 1 / 2)
        assert fused[2] == pytest.approx(1 / 2 + 1 / 2)
        assert fused[3] == pytest.approx(1 / 3 + 1 / 1)

    def test_sorted_best_first(self):
        """Results are ordered by descending fused score."""
        fused = fuse_rankings({1: 1.0, 2: 2.0, 3: 3.0}, {1: 1.0, 2: 2.0, 3: 3.0})

        assert [cid for cid, _ in fused] == [3, 2, 1]

    def test_empty(self):
        """No candidates yields no results."""
        assert fuse_rankings({}, {}) == []


class TestDependencies:
    """Tests for dependency parsing and counting."""

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            (["A", "B"], ["A", "B"]),
            (None, []),
            ("not json [", []),
            ('{"A": 1}', []),
            ("null", []),
        ],
    )
    def test_parse_dependencies(self, stored, expected: list[str]):
        """Only JSON lists yield dependencies; anything else is empty."""
        assert parse_dependencies(declaration(1, "X", stored)) == expected

    def test_count_dependents_only_counts_within_pool(self):
        """References to declarations outside the pool are ignored."""
        pool = [
            declaration(1, "Base"),
            declaration(2, "UsesBase", ["Base", "Outside"]),
            declaration(3, "UsesBoth", ["Base", "UsesBase"]),
        ]

        assert count_dependents(pool) == {"Base": 2, "UsesBase": 1, "UsesBoth": 0}


class TestApplyDependencyBoost:
    """Tests for the dependency boost stage."""

    def test_depended_on_candidate_moves_up(self):
        """A foundational declaration outranks a slightly better fused match."""
        declarations = {
            1: declaration(1, "Lemma", ["Base"]),
            2: declaration(2, "Other", ["Base"]),
            3: declaration(3, "Base"),
        }
        fused = [(1, 0.9), (3, 0.8), (2, 0.7)]

        boosted = apply_dependency_boost(fused, declarations)

        assert [cid for cid, _ in boosted] == [3, 1, 2]
        assert dict(boosted)[3] == pytest.approx(1 / 2 + 1 / 1)
        assert dict(boosted)[1] == pytest.approx(1 / 1 + 1 / 3)

    def test_without_dependencies_order_is_kept(self):
        """With no dependents, every candidate shares the worst dependency rank."""
        declarations = {i: declaration(i, f"D{i}") for i in (1, 2, 3)}
        fused = [(1, 0.9), (2, 0.8), (3, 0.7)]

        boosted = apply_dependency_boost(fused, declarations, pool_size=3)

        assert [cid for cid, _ in boosted] == [1, 2, 3]
        assert dict(boosted)[1] == pytest.approx(1 / 1 + 1 / 4)

    def test_only_pool_is_returned(self):
        """Candidates beyond the pool size are dropped."""
        declarations = {i: declaration(i, f"D{i}") for i in (1, 2, 3)}

        boosted = apply_dependency_boost(
            [(1, 0.9), (2, 0.8), (3, 0.7)], declarations, pool_size=2
        )

        assert {cid for cid, _ in boosted} == {1, 2}

    def test_candidates_missing_from_database_are_kept(self):
        """An id without a loaded declaration still receives a score."""
        boosted = apply_dependency_boost(
            [(1, 0.9), (99, 0.8)], {1: declaration(1, "A")}
        )

        assert {cid for cid, _ in boosted} == {1, 99}


class TestRerankSignals:
    """Tests for the signals combined after cross-encoder reranking."""

    def test_rerank_document_prefers_informalization(self):
        """The cross-encoder sees the name and informalization when available."""
        assert rerank_document(declaration(1, "A", informalization="x")) == "A: x"
        assert rerank_document(declaration(1, "A")) == "A"

    def test_informal_bm25_scores_follow_input_order(self):
        """Each score belongs to the declaration at the same position."""
        declarations = [
            declaration(1, "A", informalization="lists and maps"),
            declaration(2, "B", informalization="prime numbers divide products"),
        ]

        scores = informal_bm25_scores("prime numbers", declarations)

        assert scores[1] > scores[0]

    def test_informal_bm25_falls_back_to_name(self):
        """Declarations without informalization are matched on their name."""
        declarations = [declaration(1, "prime"), declaration(2, "other")]

        scores = informal_bm25_scores("prime", declarations)

        assert scores[0] > scores[1]

    def test_combine_rerank_scores_weights_signals(self):
        """Scores add the weighted normalized reranker, BM25, and dependency signals."""
        declarations = [
            declaration(1, "Alpha", informalization="unrelated words"),
            declaration(2, "Beta", ["Alpha"], informalization="unrelated text"),
        ]
        weights = RerankWeights(fuzzy_name_threshold=2.0)

        scores = combine_rerank_scores("zzz", declarations, [0.2, 0.8], weights)

        assert scores == pytest.approx([0.2, 1.0])

    def test_fuzzy_name_bonus_needs_threshold(self):
        """A near-exact name match earns the fuzzy bonus above the threshold."""
        declarations = [declaration(1, "Nat.add"), declaration(2, "List.map")]
        weights = RerankWeights(reranker=0.0, informal_bm25=0.0, dependency=0.0)

        scores = combine_rerank_scores("nat add", declarations, [0.5, 0.5], weights)

        assert scores == pytest.approx([1.0, 0.0])
