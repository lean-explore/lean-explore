"""Tests for the main CLI module.

These tests verify the core CLI commands including search and MCP server launch.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import typer
from typer.testing import CliRunner

from lean_explore.cli.main import _get_console, _search_async, app
from lean_explore.models import SearchResponse, SearchResult

runner = CliRunner()


class TestGetConsole:
    """Tests for the _get_console helper function."""

    def test_get_console_stdout(self):
        """Test creating a stdout console."""
        console = _get_console(use_stderr=False)
        assert console is not None
        assert not console.stderr

    def test_get_console_stderr(self):
        """Test creating a stderr console."""
        console = _get_console(use_stderr=True)
        assert console is not None
        assert console.stderr


class TestSearchCommand:
    """Tests for the search command."""

    @pytest.fixture
    def mock_api_client(self):
        """Create a mock API client."""
        client = MagicMock()
        client.search = AsyncMock(
            return_value=SearchResponse(
                query="test",
                results=[
                    SearchResult(
                        id=1,
                        name="Test.result",
                        module="Test",
                        docstring="A test result",
                        source_text="def test := 1",
                        source_link="https://example.com",
                        dependencies=None,
                        informalization="A test",
                    )
                ],
                count=1,
                processing_time_ms=42,
            )
        )
        return client

    async def test_search_command_success(self, mock_api_client):
        """Test successful search command execution."""
        with (
            patch("lean_explore.cli.main.ApiClient", return_value=mock_api_client),
            patch("lean_explore.cli.main.display_search_results"),
        ):
            # Test the async implementation directly
            await _search_async(query_string="test query", limit=5, packages=None)
            mock_api_client.search.assert_called_once_with(
                query="test query", limit=5, packages=None
            )

    async def test_search_command_with_limit(self, mock_api_client):
        """Test search command with custom limit."""
        with (
            patch("lean_explore.cli.main.ApiClient", return_value=mock_api_client),
            patch("lean_explore.cli.main.display_search_results"),
        ):
            await _search_async(query_string="test query", limit=10, packages=None)
            mock_api_client.search.assert_called_once_with(
                query="test query", limit=10, packages=None
            )

    async def test_search_command_with_packages(self, mock_api_client):
        """Test search command with package filter."""
        with (
            patch("lean_explore.cli.main.ApiClient", return_value=mock_api_client),
            patch("lean_explore.cli.main.display_search_results"),
        ):
            await _search_async(
                query_string="test query", limit=5, packages=["Mathlib", "Std"]
            )
            mock_api_client.search.assert_called_once_with(
                query="test query", limit=5, packages=["Mathlib", "Std"]
            )

    async def test_search_command_reports_network_errors(self, mock_api_client):
        """Remote request failures produce a concise stderr error and exit 1."""
        mock_api_client.search.side_effect = httpx.ConnectError("connection refused")
        output_console = MagicMock()
        error_console = MagicMock()

        with (
            patch("lean_explore.cli.main.ApiClient", return_value=mock_api_client),
            patch(
                "lean_explore.cli.main._get_console",
                side_effect=[output_console, error_console],
            ),
            pytest.raises(typer.Exit) as exit_info,
        ):
            await _search_async(query_string="test query", limit=5, packages=None)

        assert exit_info.value.exit_code == 1
        error_console.print.assert_called_once_with(
            "[red]Search failed:[/red] connection refused"
        )


class TestMcpServeCommand:
    """Tests for the MCP serve command."""

    @pytest.mark.parametrize(
        ("arguments", "backend", "log_level"),
        [
            ([], "api", "ERROR"),
            (["--backend", "local"], "local", "ERROR"),
            (["--backend", "LOCAL", "--log-level", "info"], "local", "INFO"),
            (["-b", "api", "--api-key", "my-key"], "api", "ERROR"),
        ],
    )
    def test_runs_server_with_options(
        self, arguments: list[str], backend: str, log_level: str
    ):
        """Options are normalized and the legacy API key is ignored."""
        with patch("lean_explore.mcp.server.run_server") as run_server:
            result = runner.invoke(app, ["mcp", "serve", *arguments])

        assert result.exit_code == 0
        run_server.assert_called_once_with(backend=backend, log_level=log_level)

    def test_propagates_server_exit_code(self):
        """A server that exits with an error makes the command fail."""
        with patch("lean_explore.mcp.server.run_server", side_effect=SystemExit(1)):
            result = runner.invoke(app, ["mcp", "serve"])

        assert result.exit_code == 1


class TestCliApp:
    """Tests for the CLI app structure."""

    def test_app_help(self):
        """Test that app help displays correctly."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "lean-explore" in result.output.lower() or "search" in result.output

    def test_search_help(self):
        """Test that search command help displays correctly."""
        result = runner.invoke(app, ["search", "--help"])
        assert result.exit_code == 0
        assert "query" in result.output.lower()

    def test_mcp_help(self):
        """Test that mcp subcommand help displays correctly."""
        result = runner.invoke(app, ["mcp", "--help"])
        assert result.exit_code == 0
        assert "serve" in result.output.lower()

    def test_data_subcommand_exists(self):
        """Test that data subcommand is registered."""
        result = runner.invoke(app, ["data", "--help"])
        assert result.exit_code == 0
