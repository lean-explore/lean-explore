"""Opt-in, context-local measurements for one search; no query text is retained."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class SearchTrace:
    """Measurements from one completed search.

    candidate_count is the union of retrieved IDs before loading, filtering,
    and truncation. Scores are the final ranking signals, not probabilities.
    cache_hit describes a whole-result cache; this engine has no such cache.
    Retrieval includes embedding, both indices, and rank fusion. Reranking
    includes cross-encoder inference and score combination. Neither includes
    the caller's execution lock. A failed search leaves completed false.
    """

    candidate_count: int = 0
    cache_hit: bool = False
    retrieval_ms: float = 0.0
    reranking_ms: float = 0.0
    scores: dict[int, float] = field(default_factory=dict)
    score_kind: str | None = None
    completed: bool = False


_current_trace: ContextVar[SearchTrace | None] = ContextVar(
    "search_trace", default=None
)


def current_search_trace() -> SearchTrace | None:
    """Return the active caller-owned trace, if capture is enabled."""
    return _current_trace.get()


@contextmanager
def capture_search_trace() -> Iterator[SearchTrace]:
    """Capture one search in this execution context without changing its results.

    Use a separate scope for each search, including concurrent tasks. Scopes
    nest safely and reset their context on exceptions. No trace is logged or
    persisted by the engine.

    Yields:
        The trace populated by SearchEngine.search in this scope.
    """
    trace = SearchTrace()
    token = _current_trace.set(trace)
    try:
        yield trace
    finally:
        _current_trace.reset(token)
