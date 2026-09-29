"""Run the Lean Explore MCP (Model Context Protocol) server over stdio.

The server exposes Lean search and retrieval as MCP tools, backed either by the
remote API or by locally downloaded data.

Command-line arguments:
  --backend {'api', 'local'} : Specifies the backend to use. (required)
  --api-key TEXT             : Deprecated compatibility option; ignored.
  --log-level TEXT           : Sets logging output level (e.g., INFO, WARNING, DEBUG).
"""

import argparse
import logging
import sys

from rich.console import Console as RichConsole

from lean_explore.config import Config

# Importing the tools module registers the tools on mcp_app.
from lean_explore.mcp import tools  # noqa: F401
from lean_explore.mcp.app import attach_backend, mcp_app
from lean_explore.mcp.backend import SearchBackend

logger = logging.getLogger(__name__)

BACKEND_CHOICES = ("api", "local")
LOG_LEVEL_CHOICES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class BackendInitializationError(RuntimeError):
    """Raised when the requested backend cannot be created."""


def _get_error_console() -> RichConsole:
    """Create a Rich console for error output to stderr."""
    return RichConsole(stderr=True)


def _parse_arguments() -> argparse.Namespace:
    """Parses command-line arguments for the MCP server.

    Returns:
        argparse.Namespace: An object containing the parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description="Lean Explore MCP Server. Provides Lean search tools via MCP."
    )
    parser.add_argument(
        "--backend",
        type=str,
        choices=BACKEND_CHOICES,
        required=True,
        help=(
            "Specifies the backend to use: 'api' for remote API, 'local' for local"
            " data."
        ),
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="Deprecated compatibility option. Its value is ignored.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=LOG_LEVEL_CHOICES,
        default="ERROR",
        help="Set the logging output level (default: ERROR).",
    )
    return parser.parse_args()


def _configure_logging(log_level: str) -> None:
    """Send log output to stderr so stdout stays reserved for MCP messages.

    Args:
        log_level: Name of the logging level, e.g. "INFO".
    """
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.ERROR),
        format="%(asctime)s - %(levelname)s - [%(name)s:%(lineno)d] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
        force=True,
    )


def _missing_local_data_message() -> str | None:
    """Describe missing local data files, or return None if all are present."""
    if Config.DATABASE_PATH.exists():
        return None
    return (
        "Essential data files for the local backend are missing.\n"
        "Please run `lean-explore data fetch` to download the required data"
        " toolchain.\n"
        f"Expected data directory for active version ('{Config.ACTIVE_VERSION}'):"
        f" {Config.ACTIVE_CACHE_PATH.resolve()}\n"
        f"Missing: database file at {Config.DATABASE_PATH.resolve()}"
    )


def build_backend(backend: str) -> SearchBackend:
    """Create the backend service used by the MCP tools.

    Args:
        backend: Either "api" for the remote API or "local" for downloaded data.

    Returns:
        The initialized backend service.

    Raises:
        BackendInitializationError: If the backend name is unknown, local data is
            missing, or the backend fails to initialize.
    """
    if backend == "api":
        from lean_explore.api import ApiClient

        return ApiClient()

    if backend != "local":
        raise BackendInitializationError(f"Unknown backend '{backend}'.")

    missing_data_message = _missing_local_data_message()
    if missing_data_message:
        raise BackendInitializationError(missing_data_message)

    try:
        # Imported lazily: loading the local search stack pulls in FAISS.
        from lean_explore.search import SearchEngine, Service

        # use_local_data=False reads the data downloaded by `data fetch`.
        return Service(engine=SearchEngine(use_local_data=False))
    except (FileNotFoundError, RuntimeError) as error:
        raise BackendInitializationError(
            f"Local backend initialization failed: {error}"
        ) from error


def run_server(backend: str, log_level: str = "ERROR") -> None:
    """Start the MCP server over stdio and block until it exits.

    Exits the process with status 1 if the backend cannot be initialized or the
    server stops with an unexpected error.

    Args:
        backend: Either "api" or "local".
        log_level: Name of the logging level for stderr output.
    """
    _configure_logging(log_level)
    logger.info("Starting Lean Explore MCP Server with backend: %s", backend)

    try:
        attach_backend(mcp_app, build_backend(backend))
    except BackendInitializationError as error:
        _get_error_console().print(f"Error: {error}", markup=False)
        sys.exit(1)

    try:
        mcp_app.run(transport="stdio")
    except Exception:
        logger.critical("MCP server exited with an unexpected error.", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("MCP server has shut down.")


def main() -> None:
    """Parse command-line arguments and run the MCP server."""
    args = _parse_arguments()
    run_server(backend=args.backend, log_level=args.log_level)


if __name__ == "__main__":
    main()
