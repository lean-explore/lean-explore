"""Tests of the judge, calibration, round trip and search metrics."""

import json

import numpy as np

from benchmarks.informalizer.calibrate import agreement
from benchmarks.informalizer.comparisons import bootstrap_interval, default_pairs
from benchmarks.informalizer.judge import (
    aggregate,
    majority,
    parts_of,
    ratings_of,
    summarize,
)
from benchmarks.informalizer.queries import BenchmarkQuery
from benchmarks.informalizer.round_trip import candidate_theorem, reference_theorem
from benchmarks.informalizer.score_search import ranking_scores

PARTS = [{"id": "P1", "text": "G is a group"}, {"id": "P2", "text": "ab = ba"}]


def rating(p1: str, p2: str, unsupported: str = "none") -> str:
    """A judge answer rating the two parts of ``PARTS``."""
    return json.dumps(
        {
            "ratings": [{"id": "P1", "rating": p1}, {"id": "P2", "rating": p2}],
            "unsupported": unsupported,
        }
    )


def test_parts_and_ratings_are_read_strictly():
    """Parts and ratings are read strictly."""
    assert parts_of('Sure: {"parts": [{"id": "P1", "text": "x"}]}') == [
        {"id": "P1", "text": "x"}
    ]
    assert parts_of('{"parts": []}') is None
    assert ratings_of(rating("match", "minor"), PARTS) == {
        "P1": "match",
        "P2": "minor",
        "unsupported": "none",
    }
    assert ratings_of('{"ratings": [{"id": "P1", "rating": "match"}]}', PARTS) is None
    assert ratings_of(rating("match", "match", "odd"), PARTS)["unsupported"] == "major"


def test_majority_breaks_ties_towards_the_worse_rating():
    """Majority breaks ties towards the worse rating."""
    assert majority(["match", "match", "major"]) == "match"
    assert majority(["match", "minor"]) == "minor"


def test_scores_follow_leanscorer_levels():
    """Scores follow leanscorer levels."""
    votes = [ratings_of(rating("match", "minor"), PARTS)] * 2
    votes.append(ratings_of(rating("match", "major"), PARTS))
    verdict = aggregate(votes)
    assert verdict["score"] == 0.5 and not verdict["faithful"]
    assert aggregate([ratings_of(rating("match", "match"), PARTS)])["faithful"]
    assert aggregate([ratings_of(rating("minor", "minor"), PARTS)])["score"] == 0.25
    assert aggregate([ratings_of(rating("match", "major"), PARTS)])["score"] == 0.0


def test_summaries_compare_paired_declarations():
    """Summaries compare paired declarations."""
    results = [
        {"arm": "m/per-file", "name": "a", "score": 1.0, "faithful": True, "majors": 0},
        {"arm": "m/current", "name": "a", "score": 0.5, "faithful": False, "majors": 0},
        {
            "arm": "m/per-file",
            "name": "b",
            "score": 0.0,
            "faithful": False,
            "majors": 1,
        },
    ]
    pairs = default_pairs(["published", "m/current", "m/per-file"])
    assert pairs[-1] == "m/per-file:m/current"
    efforts = default_pairs(["m-default/current", "m-low/current"])
    assert efforts[-1] == "m-low/current:m-default/current"
    summary = summarize(results, pairs)
    assert summary["arms"]["m/per-file"]["major"] == 0.5
    assert summary["comparisons"]["m/per-file:m/current"]["declarations"] == 1


def test_agreement_matches_hand_computed_values():
    """Agreement matches hand computed values."""
    pairs = [(True, True), (True, False), (False, False), (False, True)]
    result = agreement(pairs)
    assert result["precision"] == 0.5 and result["recall"] == 0.5
    assert result["kappa"] == 0.0
    perfect = agreement([(True, True), (False, False)])
    assert perfect["f1"] == 1.0 and perfect["kappa"] == 1.0


def test_candidates_are_cleaned_for_lean():
    """Candidates are cleaned for lean."""
    answer = (
        "Here it is.\n```lean\nimport Mathlib\nopen Real\n"
        "lemma candidate (x : ℝ) : x = x := by sorry\n```"
    )
    assert candidate_theorem(answer) == "theorem candidate (x : ℝ) : x = x := by sorry"
    assert candidate_theorem("no code here") is None
    assert reference_theorem("Nat.cast_pos") == (
        "theorem reference_statement : type_of% @Nat.cast_pos := by sorry"
    )


def test_ranking_scores_use_linear_gains():
    """Ranking scores use linear gains."""
    query = BenchmarkQuery("q", "text", "Formal", "test", {"a": 3, "b": 1})
    scores = ranking_scores(["x", "a", "b"], query)
    assert scores["success@1"] == 0.0 and scores["success@10"] == 1.0
    assert scores["mrr@10"] == 0.5
    ideal = 3 + 1 / np.log2(3)
    assert np.isclose(scores["ndcg@10"], (3 / np.log2(3) + 1 / 2) / ideal)


def test_bootstrap_interval_brackets_the_mean():
    """Bootstrap interval brackets the mean."""
    differences = np.array([0.0, 1.0] * 50)
    low, high = bootstrap_interval(differences)
    assert low < 0.5 < high
