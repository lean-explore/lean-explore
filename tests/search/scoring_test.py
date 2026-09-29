"""Tests for score normalization and fuzzy name matching."""

import math

import pytest

from lean_explore.search.scoring import (
    fuzzy_name_score,
    normalize_dependency_counts,
    normalize_scores,
)


class TestNormalizeScores:
    """Tests for min-max normalization."""

    def test_scales_to_unit_interval(self):
        """The minimum maps to 0, the maximum to 1, and order is kept."""
        assert normalize_scores([1.0, 3.0, 2.0, 5.0]) == [0.0, 0.5, 0.25, 1.0]

    def test_empty(self):
        """Empty input gives empty output."""
        assert normalize_scores([]) == []

    @pytest.mark.parametrize(
        ("scores", "expected"), [([3.0, 3.0], [1.0, 1.0]), ([0.0, 0.0], [0.0, 0.0])]
    )
    def test_constant_scores(self, scores: list[float], expected: list[float]):
        """Equal positive scores all become 1; equal zero scores stay 0."""
        assert normalize_scores(scores) == expected


class TestNormalizeDependencyCounts:
    """Tests for log-scaled dependency count normalization."""

    def test_log_scaling(self):
        """Counts scale by log(1 + count) / log(1 + max)."""
        normalized = normalize_dependency_counts([0, 1, 3])

        assert normalized[0] == 0.0
        assert normalized[1] == pytest.approx(math.log(2) / math.log(4))
        assert normalized[2] == 1.0

    @pytest.mark.parametrize(("counts", "expected"), [([], []), ([0, 0], [0.0, 0.0])])
    def test_degenerate_inputs(self, counts: list[int], expected: list[float]):
        """Empty and all-zero inputs do not divide by zero."""
        assert normalize_dependency_counts(counts) == expected


class TestFuzzyNameScore:
    """Tests for fuzzy name similarity."""

    def test_exact_match(self):
        """Identical names score 1."""
        assert fuzzy_name_score("Nat.add", "Nat.add") == 1.0

    def test_separators_are_ignored(self):
        """Dots and underscores are treated as spaces, case-insensitively."""
        assert fuzzy_name_score("nat add comm", "Nat.add_comm") == 1.0

    def test_partial_and_unrelated(self):
        """Partial matches score between unrelated names and exact matches."""
        partial = fuzzy_name_score("add", "Nat.add")
        unrelated = fuzzy_name_score("xyz", "Nat.add")

        assert 0.0 <= unrelated < partial < 1.0
