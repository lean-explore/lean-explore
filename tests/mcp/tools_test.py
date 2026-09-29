"""Tests for the MCP tools, exercised through a real in-memory MCP session."""

import pytest

from tests.mcp.conftest import FakeBackend, connect

FIELD_TOOLS = {
    "get_source_code": "source_text",
    "get_source_link": "source_link",
    "get_docstring": "docstring",
    "get_description": "informalization",
    "get_module": "module",
    "get_dependencies": "dependencies",
}


class TestToolRegistration:
    """Tests for the tool list that clients discover."""

    async def test_all_tools_are_registered(self, backend: FakeBackend):
        """Clients see the two search tools and all per-field tools."""
        async with connect(backend) as client:
            tools = (await client.list_tools()).tools

            assert {tool.name for tool in tools} == {
                "search",
                "search_summary",
                *FIELD_TOOLS,
            }

    async def test_all_tools_are_annotated_as_read_only(self, backend: FakeBackend):
        """Expose accurate safety hints required by plugin directories."""
        async with connect(backend) as client:
            for tool in (await client.list_tools()).tools:
                assert tool.title
                assert tool.annotations is not None
                assert tool.annotations.readOnlyHint is True
                assert tool.annotations.destructiveHint is False
                assert tool.annotations.idempotentHint is True
                assert tool.annotations.openWorldHint is False

    async def test_search_is_advertised_as_deprecated(self, backend: FakeBackend):
        """Direct MCP clients from the deprecated search tool to search_summary."""
        async with connect(backend) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            tool = tools["search"]

            assert tool.title == "[Deprecated] Search with full results"
            assert tool.description.startswith("DEPRECATED: Use search_summary")
            assert tool.meta == {"deprecated": True, "replacement": "search_summary"}

    async def test_tools_publish_output_schemas(self, backend: FakeBackend):
        """Every tool returns structured content described by a schema."""
        async with connect(backend) as client:
            for tool in (await client.list_tools()).tools:
                assert tool.outputSchema is not None, tool.name


class TestSearchSummaryTool:
    """Tests for the recommended search_summary tool."""

    async def test_forwards_default_arguments(self, backend: FakeBackend):
        """Omitted arguments use the documented defaults."""
        async with connect(backend) as client:
            await client.call_tool("search_summary", {"query": "prime"})

            assert backend.search_calls == [
                {"query": "prime", "limit": 10, "rerank_top": 50, "packages": None}
            ]

    async def test_forwards_explicit_arguments(self, backend: FakeBackend):
        """Explicit limit, rerank_top, and packages reach the backend unchanged."""
        async with connect(backend) as client:
            await client.call_tool(
                "search_summary",
                {"query": "q", "limit": 3, "rerank_top": 0, "packages": ["Mathlib"]},
            )

            assert backend.search_calls == [
                {"query": "q", "limit": 3, "rerank_top": 0, "packages": ["Mathlib"]}
            ]

    async def test_returns_slim_results(self, backend: FakeBackend):
        """Results carry only id, name, and the bold description."""
        async with connect(backend) as client:
            result = await client.call_tool("search_summary", {"query": "q"})

            assert not result.isError
            assert result.structuredContent == {
                "query": "q",
                "count": 2,
                "processing_time_ms": 7,
                "results": [
                    {"id": 1, "name": "Test.decl1", "description": "Test Title."},
                    {"id": 2, "name": "Test.decl2"},
                ],
            }


class TestDeprecatedSearchTool:
    """Tests for the deprecated full-result search tool."""

    async def test_returns_all_fields(self, backend: FakeBackend):
        """Full results include every non-null declaration field."""
        async with connect(backend) as client:
            result = await client.call_tool("search", {"query": "q", "limit": 1})

            assert not result.isError
            assert result.structuredContent["count"] == 1
            assert result.structuredContent["results"] == [
                {
                    "id": 1,
                    "name": "Test.decl1",
                    "module": "Test.Module",
                    "docstring": "A test docstring.",
                    "source_text": "def decl1 := 1",
                    "source_link": "https://example.com/decl1",
                    "dependencies": '["Nat.add"]',
                    "informalization": "**Test Title.** A test informalization.",
                }
            ]

    async def test_omits_null_fields(self, backend: FakeBackend):
        """Null fields are dropped from full results to save context."""
        async with connect(backend) as client:
            result = await client.call_tool("search", {"query": "q"})

            assert "docstring" not in result.structuredContent["results"][1]


class TestFieldTools:
    """Tests for the per-field retrieval tools."""

    @pytest.mark.parametrize(("tool_name", "field"), FIELD_TOOLS.items())
    async def test_returns_id_name_and_field(
        self, backend: FakeBackend, tool_name: str, field: str
    ):
        """Each tool returns the declaration id, name, and its one field."""
        async with connect(backend) as client:
            result = await client.call_tool(tool_name, {"declaration_id": 1})

            assert not result.isError
            content = result.structuredContent["result"]
            assert set(content) == {"id", "name", field}
            assert content["id"] == 1
            assert content["name"] == "Test.decl1"

    async def test_null_field_is_returned_as_null(self, backend: FakeBackend):
        """A missing docstring is reported explicitly rather than omitted."""
        async with connect(backend) as client:
            result = await client.call_tool("get_docstring", {"declaration_id": 2})

            assert result.structuredContent == {
                "result": {"id": 2, "name": "Test.decl2", "docstring": None}
            }

    @pytest.mark.parametrize("tool_name", FIELD_TOOLS)
    async def test_unknown_id_returns_null(self, backend: FakeBackend, tool_name):
        """An id that does not exist yields a null result, not an error."""
        async with connect(backend) as client:
            result = await client.call_tool(tool_name, {"declaration_id": 999})

            assert not result.isError
            assert result.structuredContent == {"result": None}


class TestBackendFailures:
    """Tests for errors raised by the backend."""

    async def test_backend_error_is_reported_as_tool_error(self, backend: FakeBackend):
        """Backend exceptions become MCP tool errors instead of crashing."""
        async with connect(backend) as client:

            async def failing_search(**_: object):
                raise RuntimeError("index unavailable")

            backend.search = failing_search
            result = await client.call_tool("search_summary", {"query": "q"})

            assert result.isError
            assert "index unavailable" in result.content[0].text
