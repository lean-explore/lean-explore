"""Tests for the MCP application, backend attachment, and lifespan."""

from unittest.mock import MagicMock

import pytest

from lean_explore.api import ApiClient
from lean_explore.mcp.app import (
    _BACKEND_ATTRIBUTE,
    SERVER_INSTRUCTIONS,
    AppContext,
    app_lifespan,
    attach_backend,
    mcp_app,
)
from lean_explore.mcp.backend import SearchBackend
from tests.mcp.conftest import FakeBackend


class TestAttachBackend:
    """Tests for attaching a backend to a FastMCP server."""

    def test_uses_attribute_read_by_hosted_app(self):
        """The storage attribute is shared with lean-explore-app and must not move."""
        server = MagicMock(spec=[])
        backend = FakeBackend([])

        attach_backend(server, backend)

        assert _BACKEND_ATTRIBUTE == "_lean_explore_backend_service"
        assert getattr(server, _BACKEND_ATTRIBUTE) is backend


class TestAppLifespan:
    """Tests for the app_lifespan context manager."""

    async def test_yields_attached_backend(self):
        """The lifespan exposes the attached backend to tools."""
        server = MagicMock(spec=[])
        backend = FakeBackend([])
        attach_backend(server, backend)

        async with app_lifespan(server) as context:
            assert isinstance(context, AppContext)
            assert context.backend_service is backend

    @pytest.mark.parametrize("attached", [False, True])
    async def test_raises_without_backend(self, attached: bool):
        """Starting without a backend fails fast with a clear error."""
        server = MagicMock(spec=[])
        if attached:
            setattr(server, _BACKEND_ATTRIBUTE, None)

        with pytest.raises(RuntimeError, match="Backend service not initialized"):
            async with app_lifespan(server):
                pass


class TestSearchBackendProtocol:
    """Tests that the real backends satisfy the SearchBackend protocol."""

    def test_api_client_is_a_search_backend(self):
        """The remote API client can back the MCP server."""
        assert isinstance(ApiClient(), SearchBackend)

    def test_search_service_is_a_search_backend(self):
        """The local search service can back the MCP server."""
        from lean_explore.search import Service

        assert isinstance(Service(engine=MagicMock()), SearchBackend)


class TestMcpApp:
    """Tests for the module-level FastMCP instance."""

    def test_name_and_instructions(self):
        """The server identifies itself and explains the recommended workflow."""
        assert mcp_app.name == "LeanExploreMCPServer"
        assert mcp_app.instructions == SERVER_INSTRUCTIONS
        assert "search_summary" in SERVER_INSTRUCTIONS
