"""Client for interacting with the remote Lean Explore API."""

import httpx

from lean_explore.config import Config
from lean_explore.models import SearchResponse, SearchResult


class ApiClient:
    """Async client for the remote Lean Explore API.

    This client handles making HTTP requests to the public API and parsing
    responses into SearchResult objects. The ``api_key`` argument is accepted
    and ignored so existing integrations continue to start without changes.
    """

    def __init__(self, api_key: str | None = None, timeout: float = 10.0):
        """Initialize the API client.

        Args:
            api_key: Deprecated compatibility argument. It is ignored.
            timeout: Default timeout for HTTP requests in seconds.
        """
        del api_key
        self.base_url: str = Config.API_BASE_URL
        self.api_key: str | None = None
        self.timeout: float = timeout
        self._headers: dict[str, str] = {}

    async def search(
        self,
        query: str,
        limit: int = 20,
        rerank_top: int | None = None,  # Ignored for API (server handles reranking)
        packages: list[str] | None = None,
    ) -> SearchResponse:
        """Search for Lean declarations via the API.

        Args:
            query: The search query string.
            limit: Maximum number of results to return.
            rerank_top: Ignored for API backend (included for interface consistency).
            packages: Filter results to specific packages (e.g., ["Mathlib"]).

        Returns:
            SearchResponse containing results and metadata.

        Raises:
            httpx.HTTPStatusError: If the API returns an HTTP error status.
            httpx.RequestError: For network-related issues.
        """
        del rerank_top  # Unused - server handles reranking
        endpoint = f"{self.base_url}/search"
        params: dict[str, str | int] = {"q": query, "limit": limit}
        if packages:
            params["packages"] = ",".join(packages)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(endpoint, params=params, headers=self._headers)
            response.raise_for_status()
            data = response.json()

            # Parse API response into our types
            results = [SearchResult(**item) for item in data.get("results", [])]

            return SearchResponse(
                query=query,
                results=results,
                count=len(results),
                processing_time_ms=data.get("processing_time_ms"),
            )

    async def get_by_id(self, declaration_id: int) -> SearchResult | None:
        """Retrieve a declaration by ID via the API.

        Args:
            declaration_id: The declaration ID.

        Returns:
            SearchResult if found, None otherwise.

        Raises:
            httpx.HTTPStatusError: If the API returns an error (except 404).
            httpx.RequestError: For network-related issues.
        """
        endpoint = f"{self.base_url}/declarations/{declaration_id}"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(endpoint, headers=self._headers)

            if response.status_code == 404:
                return None

            response.raise_for_status()
            return SearchResult(**response.json())
