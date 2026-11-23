"""Service layer for search operations."""

import time

from lean_explore.models import SearchResponse, SearchResult
from lean_explore.search.engine import SearchEngine


class Service:
    """Service wrapper for search operations.

    Provides a clean interface for searching and retrieving declarations.
    """

    def __init__(self, engine: SearchEngine | None = None):
        """Initialize the search service.

        Args:
            engine: SearchEngine instance. Defaults to new engine.
        """
        self.engine = engine or SearchEngine()

    async def search(
        self,
        query: str,
        limit: int = 20,
        rerank_top: int | None = 50,
        packages: list[str] | None = None,
    ) -> SearchResponse:
        """Search for Lean declarations.

        Args:
            query: Search query string.
            limit: Maximum number of results to return.
            rerank_top: Number of candidates to rerank with cross-encoder.
            packages: Filter results to specific packages (e.g., ["Mathlib"]).

        Returns:
            SearchResponse containing results and metadata.
        """
        start_time = time.time()

        results = await self.engine.search(
            query=query,
            limit=limit,
            rerank_top=rerank_top,
            packages=packages,
        )

        processing_time_ms = int((time.time() - start_time) * 1000)

        return SearchResponse(
            query=query,
            results=results,
            count=len(results),
            processing_time_ms=processing_time_ms,
        )

    async def get_by_id(self, declaration_id: int) -> SearchResult | None:
        """Retrieve a declaration by ID.

        Args:
            declaration_id: The declaration ID.

        Returns:
            SearchResult if found, None otherwise.
        """
        return await self.engine.get_by_id(declaration_id)
