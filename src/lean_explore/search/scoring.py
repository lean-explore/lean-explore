"""Score normalization and fuzzy matching for search ranking.

This module provides score normalization and fuzzy name matching used when
combining retrieval signals into a final ranking.
"""

import difflib
import math

EPSILON = 1e-9


def normalize_scores(scores: list[float]) -> list[float]:
    """Min-max normalize scores to [0, 1] range.

    Args:
        scores: List of raw scores.

    Returns:
        List of normalized scores.
    """
    if not scores:
        return []

    min_score = min(scores)
    max_score = max(scores)
    score_range = max_score - min_score

    if score_range < EPSILON:
        if max_score > EPSILON:
            return [1.0] * len(scores)
        return [0.0] * len(scores)

    return [(s - min_score) / score_range for s in scores]


def normalize_dependency_counts(counts: list[int]) -> list[float]:
    """Log-scale normalization for dependency counts.

    Uses log(1 + count) / log(1 + max_count) to compress the range
    and give more credit to items with moderate dependency counts.

    Args:
        counts: List of dependency counts.

    Returns:
        List of normalized scores in [0, 1] range.
    """
    if not counts:
        return []

    max_count = max(counts)
    if max_count == 0:
        return [0.0] * len(counts)

    log_max = math.log(1 + max_count)
    return [math.log(1 + c) / log_max for c in counts]


def fuzzy_name_score(query: str, name: str) -> float:
    """Compute fuzzy match score between query and declaration name.

    Normalizes both strings (dots/underscores -> spaces) and uses
    SequenceMatcher ratio for character-level similarity.

    Args:
        query: Search query string.
        name: Declaration name to match against.

    Returns:
        Similarity score between 0 and 1.
    """
    normalized_query = query.lower().replace(".", " ").replace("_", " ")
    normalized_name = name.lower().replace(".", " ").replace("_", " ")
    return difflib.SequenceMatcher(None, normalized_query, normalized_name).ratio()
