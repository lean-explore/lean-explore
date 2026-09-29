"""Command-Line Interface for Lean Explore.

Provides commands to search for Lean declarations via the remote API,
run the MCP server, and manage local data.
"""

import asyncio
import logging

import httpx
import typer
from rich.console import Console

from lean_explore.api import ApiClient
from lean_explore.cli import data_commands
from lean_explore.cli.display import display_search_results

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="lean-explore",
    help="A CLI tool to explore and search Lean mathematical libraries.",
    add_completion=False,
    rich_markup_mode="markdown",
)

mcp_app = typer.Typer(
    name="mcp", help="Manage and run the Model Context Protocol (MCP) server."
)
app.add_typer(mcp_app)

app.add_typer(
    data_commands.app,
    name="data",
    help="Manage local data toolchains.",
)


def _get_console(use_stderr: bool = False) -> Console:
    """Create a Rich console instance for output.

    Args:
        use_stderr: If True, output to stderr instead of stdout.

    Returns:
        A configured Console instance.
    """
    return Console(stderr=use_stderr)


@app.command("search")
def search_command(
    query_string: str = typer.Argument(..., help="The search query string."),
    limit: int = typer.Option(
        5, "--limit", "-n", help="Number of search results to display."
    ),
    packages: list[str] | None = typer.Option(
        None, "--package", "-p", help="Filter by package (e.g., -p Mathlib -p Std)."
    ),
):
    """Search for Lean declarations using the Lean Explore API."""
    asyncio.run(_search_async(query_string, limit, packages))


async def _search_async(
    query_string: str, limit: int, packages: list[str] | None
) -> None:
    """Async implementation of search command."""
    console = _get_console()

    client = ApiClient()

    console.print(f"Searching for: '{query_string}'...")
    try:
        response = await client.search(
            query=query_string,
            limit=limit,
            packages=packages,
        )
    except httpx.HTTPError as error:
        logger.error("Remote search request failed: %s", error)
        error_console = _get_console(use_stderr=True)
        error_console.print(f"[red]Search failed:[/red] {error}")
        raise typer.Exit(code=1) from error
    display_search_results(response, display_limit=limit, console=console)


@mcp_app.command("serve")
def mcp_serve_command(
    backend: str = typer.Option(
        "api",
        "--backend",
        "-b",
        help="Backend to use for the MCP server: 'api' or 'local'. Default is 'api'.",
        case_sensitive=False,
        show_choices=True,
    ),
    api_key_override: str | None = typer.Option(
        None,
        "--api-key",
        help="Deprecated compatibility option. Its value is ignored.",
    ),
    log_level: str = typer.Option(
        "ERROR",
        "--log-level",
        help="Logging level for stderr output (DEBUG, INFO, WARNING, ERROR).",
        case_sensitive=False,
    ),
):
    """Launch the Lean Explore MCP (Model Context Protocol) server."""
    del api_key_override
    from lean_explore.mcp.server import run_server

    run_server(backend=backend.lower(), log_level=log_level.upper())


if __name__ == "__main__":
    app()
