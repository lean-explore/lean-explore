"""Ranking stages that turn retrieval candidates into ordered results.

The engine ranks in three stages:

1. Reciprocal rank fusion of the BM25 name ranking and the semantic ranking.
2. A dependency boost: candidates that many other top candidates depend on
   (foundational definitions) move up.
3. Optional reranking, combining cross-encoder scores with BM25 over the
   informalizations, the dependency signal, and fuzzy name matching.

Every function here is pure; the engine supplies the candidates and scores.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass

import bm25s

from lean_explore.models import Declaration
from lean_explore.search.scoring import (
    fuzzy_name_score,
    normalize_dependency_counts,
    normalize_scores,
)
from lean_explore.search.tokenization import tokenize_words

DEPENDENCY_BOOST_POOL_SIZE = 500
"""Number of top fused candidates analyzed for the dependency boost."""


@dataclass(frozen=True)
class RerankWeights:
    """Weights of the signals combined after cross-encoder reranking."""

    reranker: float = 1.0
    informal_bm25: float = 0.4
    dependency: float = 0.2
    fuzzy_name: float = 1.0
    fuzzy_name_threshold: float = 0.7
    """Fuzzy name similarity below this threshold contributes nothing."""


DEFAULT_RERANK_WEIGHTS = RerankWeights()


def _ranks(scores_by_id: dict[int, float]) -> dict[int, int]:
    """Map each id to its 1-based rank by descending score."""
    ordered = sorted(scores_by_id.items(), key=lambda item: item[1], reverse=True)
    return {declaration_id: rank for rank, (declaration_id, _) in enumerate(ordered, 1)}


def fuse_rankings(
    name_scores: dict[int, float], semantic_scores: dict[int, float]
) -> list[tuple[int, float]]:
    """Combine two candidate rankings with reciprocal rank fusion.

    Each candidate scores 1/name_rank + 1/semantic_rank. A candidate missing
    from one ranking takes the rank just past that ranking's end.

    Args:
        name_scores: Map of declaration id to BM25 name score.
        semantic_scores: Map of declaration id to semantic similarity.

    Returns:
        (declaration_id, fused_score) pairs sorted by descending score.
    """
    candidate_ids = set(name_scores.keys()) | set(semantic_scores.keys())
    name_ranks = _ranks(name_scores)
    semantic_ranks = _ranks(semantic_scores)
    missing_name_rank = len(name_ranks) + 1
    missing_semantic_rank = len(semantic_ranks) + 1

    fused = [
        (
            declaration_id,
            1.0 / name_ranks.get(declaration_id, missing_name_rank)
            + 1.0 / semantic_ranks.get(declaration_id, missing_semantic_rank),
        )
        for declaration_id in candidate_ids
    ]
    fused.sort(key=lambda item: item[1], reverse=True)
    return fused


def parse_dependencies(declaration: Declaration) -> list[str]:
    """Return the dependency names stored on a declaration.

    Args:
        declaration: The declaration whose JSON dependency list to parse.

    Returns:
        The dependency names, or an empty list if absent or not a JSON list.
    """
    if not declaration.dependencies:
        return []
    try:
        dependencies = json.loads(declaration.dependencies)
    except json.JSONDecodeError:
        return []
    return dependencies if isinstance(dependencies, list) else []


def count_dependents(declarations: Iterable[Declaration]) -> dict[str, int]:
    """Count, for each declaration, how many of the others depend on it.

    Args:
        declarations: The candidate pool.

    Returns:
        Map from declaration name to the number of references to it from
        dependency lists within the pool.
    """
    pool = list(declarations)
    counts = {declaration.name: 0 for declaration in pool}
    for declaration in pool:
        for dependency_name in parse_dependencies(declaration):
            if dependency_name in counts:
                counts[dependency_name] += 1
    return counts


def apply_dependency_boost(
    fused_scores: list[tuple[int, float]],
    declarations: dict[int, Declaration],
    pool_size: int = DEPENDENCY_BOOST_POOL_SIZE,
) -> list[tuple[int, float]]:
    """Re-score the top fused candidates, boosting widely depended-on ones.

    Each candidate in the pool scores 1/fused_rank + 1/dependency_rank, where
    candidates with more dependents get a better dependency rank.

    Args:
        fused_scores: Output of fuse_rankings, best first.
        declarations: Declarations for the pool, keyed by id.
        pool_size: Number of top fused candidates to re-score.

    Returns:
        (declaration_id, boosted_score) pairs for the pool, best first.
    """
    pool = fused_scores[:pool_size]
    dependents_by_name = count_dependents(
        declarations[declaration_id]
        for declaration_id, _ in pool
        if declaration_id in declarations
    )

    def dependent_count(declaration_id: int) -> int:
        declaration = declarations.get(declaration_id)
        return dependents_by_name[declaration.name] if declaration else 0

    max_count = max((dependent_count(cid) for cid, _ in pool), default=0)
    no_dependents_rank = max_count + 1 if max_count > 0 else pool_size + 1

    boosted: list[tuple[int, float]] = []
    for fused_rank, (declaration_id, _) in enumerate(pool, 1):
        count = dependent_count(declaration_id)
        dependency_rank = max_count - count + 1 if count > 0 else no_dependents_rank
        boosted.append((declaration_id, 1.0 / fused_rank + 1.0 / dependency_rank))

    boosted.sort(key=lambda item: item[1], reverse=True)
    return boosted


def rerank_document(declaration: Declaration) -> str:
    """Text shown to the cross-encoder for a declaration."""
    if declaration.informalization:
        return f"{declaration.name}: {declaration.informalization}"
    return declaration.name


def informal_bm25_scores(query: str, declarations: list[Declaration]) -> list[float]:
    """Score each declaration by BM25+ of the query against its informalization.

    Declarations without an informalization are scored on their name.

    Args:
        query: Search query string.
        declarations: The candidates to score.

    Returns:
        One score per declaration, in input order.
    """
    corpus = [
        tokenize_words(declaration.informalization or declaration.name)
        for declaration in declarations
    ]
    index = bm25s.BM25(method="bm25+")
    index.index(corpus, show_progress=False)
    positions, scores = index.retrieve(
        [tokenize_words(query)], k=len(corpus), show_progress=False
    )

    scores_by_position = [0.0] * len(declarations)
    for position, score in zip(positions[0], scores[0]):
        scores_by_position[int(position)] = float(score)
    return scores_by_position


def combine_rerank_scores(
    query: str,
    declarations: list[Declaration],
    reranker_scores: list[float],
    weights: RerankWeights = DEFAULT_RERANK_WEIGHTS,
) -> list[float]:
    """Combine the cross-encoder score with lexical and structural signals.

    Args:
        query: Search query string.
        declarations: The reranked candidates.
        reranker_scores: Cross-encoder score for each candidate.
        weights: Signal weights.

    Returns:
        Final score for each candidate, in input order.
    """
    fuzzy_scores = [fuzzy_name_score(query, d.name) for d in declarations]
    dependents_by_name = count_dependents(declarations)

    normalized_reranker = normalize_scores(reranker_scores)
    normalized_bm25 = normalize_scores(informal_bm25_scores(query, declarations))
    normalized_fuzzy = normalize_scores(fuzzy_scores)
    normalized_dependents = normalize_dependency_counts(
        [dependents_by_name[d.name] for d in declarations]
    )

    final_scores = []
    for i in range(len(declarations)):
        score = (
            weights.reranker * normalized_reranker[i]
            + weights.informal_bm25 * normalized_bm25[i]
            + weights.dependency * normalized_dependents[i]
        )
        if fuzzy_scores[i] >= weights.fuzzy_name_threshold:
            score += weights.fuzzy_name * normalized_fuzzy[i]
        final_scores.append(score)
    return final_scores
