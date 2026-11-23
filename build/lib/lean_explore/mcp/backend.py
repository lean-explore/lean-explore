"""Backend interface shared by the MCP tools.

The MCP server can be backed by the remote API client or by a local search
service. Both satisfy the ``SearchBackend`` protocol, so the tools never need
to know which one they are talking to.
"""

from typing import Protocol, runtime_checkable

from lean_explore.models import SearchResponse, SearchResult


@runtime_checkable
class SearchBackend(Protocol):
    """Asynchronous search and retrieval operations used by the MCP tools."""

    async def search(
        self,
        query: str,
        limit: int = 20,
        rerank_top: int | None = 50,
        packages: list[str] | None = None,
    ) -> SearchResponse:
        """Search for Lean declarations."""
        ...

    async def get_by_id(self, declaration_id: int) -> SearchResult | None:
        """Retrieve a declaration by id, or None when it does not exist."""
        ...
