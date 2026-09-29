"""Shared fixtures for MCP tests: an in-memory backend and a connected client."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from lean_explore.mcp import tools  # noqa: F401
from lean_explore.mcp.app import _BACKEND_ATTRIBUTE, attach_backend, mcp_app
from lean_explore.models import SearchResponse, SearchResult


def make_result(declaration_id: int = 1, **overrides: object) -> SearchResult:
    """Build a SearchResult with sensible defaults for tests.

    Args:
        declaration_id: The declaration id.
        **overrides: Field values that replace the defaults.

    Returns:
        A populated SearchResult.
    """
    fields: dict[str, object] = {
        "id": declaration_id,
        "name": f"Test.decl{declaration_id}",
        "module": "Test.Module",
        "docstring": "A test docstring.",
        "source_text": f"def decl{declaration_id} := {declaration_id}",
        "source_link": f"https://example.com/decl{declaration_id}",
        "dependencies": '["Nat.add"]',
        "informalization": "**Test Title.** A test informalization.",
    }
    fields.update(overrides)
    return SearchResult(**fields)


class FakeBackend:
    """In-memory SearchBackend that records the searches it receives."""

    def __init__(self, results: list[SearchResult]):
        """Store the results that every search will return.

        Args:
            results: Declarations available to search and get_by_id.
        """
        self.results = results
        self.search_calls: list[dict[str, object]] = []

    async def search(
        self,
        query: str,
        limit: int = 20,
        rerank_top: int | None = 50,
        packages: list[str] | None = None,
    ) -> SearchResponse:
        """Record the call and return the stored results, truncated to limit."""
        self.search_calls.append(
            {
                "query": query,
                "limit": limit,
                "rerank_top": rerank_top,
                "packages": packages,
            }
        )
        results = self.results[:limit]
        return SearchResponse(
            query=query, results=results, count=len(results), processing_time_ms=7
        )

    async def get_by_id(self, declaration_id: int) -> SearchResult | None:
        """Return the stored result with the given id, or None."""
        return next((r for r in self.results if r.id == declaration_id), None)


@pytest.fixture
def backend() -> FakeBackend:
    """A fake backend holding two declarations."""
    return FakeBackend(
        [make_result(1), make_result(2, docstring=None, informalization="No title.")]
    )


@asynccontextmanager
async def connect(backend: FakeBackend) -> AsyncIterator[ClientSession]:
    """Open an in-memory MCP client session to mcp_app backed by ``backend``.

    This is a context manager rather than a fixture because the session must be
    entered and exited in the same task, which pytest-asyncio fixtures do not
    guarantee.

    Args:
        backend: The backend the server's tools will use.

    Yields:
        A client session connected to mcp_app.
    """
    previous = getattr(mcp_app, _BACKEND_ATTRIBUTE, None)
    attach_backend(mcp_app, backend)
    try:
        async with create_connected_server_and_client_session(mcp_app) as session:
            yield session
    finally:
        setattr(mcp_app, _BACKEND_ATTRIBUTE, previous)
