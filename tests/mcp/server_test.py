"""Tests for the MCP server entry point."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lean_explore.api import ApiClient
from lean_explore.mcp import server
from lean_explore.mcp.app import _BACKEND_ATTRIBUTE, mcp_app
from lean_explore.mcp.server import (
    BackendInitializationError,
    _parse_arguments,
    build_backend,
    main,
    run_server,
)


@pytest.fixture(autouse=True)
def restore_attached_backend():
    """Undo any backend a test attaches to the shared mcp_app."""
    previous = getattr(mcp_app, _BACKEND_ATTRIBUTE, None)
    yield
    setattr(mcp_app, _BACKEND_ATTRIBUTE, previous)


class TestParseArguments:
    """Tests for command-line parsing."""

    def test_defaults(self):
        """Only the backend is required; log level defaults to ERROR."""
        with patch.object(sys, "argv", ["server", "--backend", "local"]):
            args = _parse_arguments()

        assert args.backend == "local"
        assert args.log_level == "ERROR"
        assert args.api_key is None

    def test_accepts_legacy_api_key(self):
        """The deprecated --api-key option is still accepted."""
        argv = ["server", "--backend", "api", "--api-key", "k", "--log-level", "DEBUG"]
        with patch.object(sys, "argv", argv):
            args = _parse_arguments()

        assert (args.backend, args.api_key, args.log_level) == ("api", "k", "DEBUG")

    @pytest.mark.parametrize(
        "argv",
        [
            ["server"],
            ["server", "--backend", "invalid"],
            ["server", "--backend", "local", "--log-level", "INVALID"],
        ],
    )
    def test_rejects_invalid_arguments(self, argv: list[str]):
        """Missing or unknown values exit with a usage error."""
        with patch.object(sys, "argv", argv), pytest.raises(SystemExit):
            _parse_arguments()


class TestBuildBackend:
    """Tests for backend construction."""

    def test_api_backend(self):
        """The api backend is the remote API client."""
        assert isinstance(build_backend("api"), ApiClient)

    def test_unknown_backend(self):
        """Unknown backend names are rejected."""
        with pytest.raises(BackendInitializationError, match="Unknown backend"):
            build_backend("remote")

    def test_local_backend_without_data(self, tmp_path: Path):
        """The local backend explains how to download missing data."""
        with patch.object(server.Config, "DATABASE_PATH", tmp_path / "missing.db"):
            with pytest.raises(BackendInitializationError, match="data fetch"):
                build_backend("local")

    def test_local_backend_with_data(self, tmp_path: Path):
        """The local backend wraps a SearchEngine reading downloaded data."""
        database = tmp_path / "lean_explore.db"
        database.touch()
        with (
            patch.object(server.Config, "DATABASE_PATH", database),
            patch("lean_explore.search.engine.SearchEngine") as engine_class,
        ):
            backend = build_backend("local")

        engine_class.assert_called_once_with(use_local_data=False)
        assert backend.engine is engine_class.return_value

    def test_local_backend_initialization_failure(self, tmp_path: Path):
        """Engine errors such as missing index files are reported, not raised raw."""
        database = tmp_path / "lean_explore.db"
        database.touch()
        with (
            patch.object(server.Config, "DATABASE_PATH", database),
            patch(
                "lean_explore.search.engine.SearchEngine",
                side_effect=FileNotFoundError("no index"),
            ),
        ):
            with pytest.raises(BackendInitializationError, match="no index"):
                build_backend("local")


class TestRunServer:
    """Tests for starting the server."""

    def test_attaches_backend_and_runs_stdio(self):
        """A healthy backend is attached and the stdio transport is started."""
        backend = MagicMock()
        with (
            patch.object(server, "build_backend", return_value=backend),
            patch.object(mcp_app, "run") as run,
        ):
            run_server("api")

        assert getattr(mcp_app, _BACKEND_ATTRIBUTE) is backend
        run.assert_called_once_with(transport="stdio")

    def test_exits_when_backend_fails(self):
        """Backend errors print a message and exit with status 1."""
        error_console = MagicMock()
        with (
            patch.object(
                server,
                "build_backend",
                side_effect=BackendInitializationError("data missing"),
            ),
            patch.object(server, "_get_error_console", return_value=error_console),
            patch.object(mcp_app, "run") as run,
            pytest.raises(SystemExit) as exit_info,
        ):
            run_server("local")

        assert exit_info.value.code == 1
        error_console.print.assert_called_once_with("Error: data missing", markup=False)
        run.assert_not_called()

    def test_exits_when_server_crashes(self):
        """Unexpected server errors exit with status 1."""
        with (
            patch.object(server, "build_backend", return_value=MagicMock()),
            patch.object(mcp_app, "run", side_effect=ValueError("boom")),
            pytest.raises(SystemExit) as exit_info,
        ):
            run_server("api")

        assert exit_info.value.code == 1


def test_main_passes_arguments_to_run_server():
    """The script entry point forwards parsed arguments."""
    argv = ["server", "--backend", "local", "--log-level", "INFO"]
    with (
        patch.object(sys, "argv", argv),
        patch.object(server, "run_server") as run,
    ):
        main()

    run.assert_called_once_with(backend="local", log_level="INFO")
