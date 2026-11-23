"""Initializes the FastMCP application and its lifespan context."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server.fastmcp import FastMCP

from lean_explore.mcp.backend import SearchBackend

logger = logging.getLogger(__name__)

# Attribute on the FastMCP instance that stores the backend. The hosted app in
# lean-explore-app sets this attribute directly, so the name must stay stable.
_BACKEND_ATTRIBUTE = "_lean_explore_backend_service"

SERVER_INSTRUCTIONS = (
    "MCP Server for searching Lean 4 mathematical declarations (theorems, "
    "definitions, lemmas, instances, etc.) from Mathlib and other Lean "
    "packages.\n\n"
    "The search engine is hybrid: it matches by declaration name (e.g., "
    "'List.map', 'Nat.add') AND by informal natural language meaning (e.g., "
    "'a continuous function on a compact set', 'prime number divisibility'). "
    "You can use either style of query.\n\n"
    "Recommended workflow:\n"
    "1. Always use search_summary to browse results (low token cost).\n"
    "2. Use per-field tools to fetch only what you need:\n"
    "   - get_source_code: Lean source code\n"
    "   - get_source_link: GitHub link to source\n"
    "   - get_docstring: documentation string\n"
    "   - get_description: natural language description\n"
    "   - get_module: module path in the package\n"
    "   - get_dependencies: declarations this depends on\n"
    "The legacy search tool is deprecated and retained only for backwards "
    "compatibility. Do not use it in new workflows; use search_summary "
    "followed by the per-field tools instead."
)


@dataclass
class AppContext:
    """Application-level context available to MCP tools.

    Attributes:
        backend_service: The backend that tools use to search and retrieve.
    """

    backend_service: SearchBackend


def attach_backend(server: FastMCP, backend: SearchBackend) -> None:
    """Attach the backend that the server's tools will use.

    Must be called before the server starts running.

    Args:
        server: The FastMCP application instance.
        backend: The backend service to expose through the MCP tools.
    """
    setattr(server, _BACKEND_ATTRIBUTE, backend)


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Provide the attached backend to tools for the server's lifetime.

    Args:
        server: The FastMCP application instance.

    Yields:
        AppContext: The application context containing the backend service.

    Raises:
        RuntimeError: If no backend has been attached to the server.
    """
    backend: SearchBackend | None = getattr(server, _BACKEND_ATTRIBUTE, None)
    if backend is None:
        raise RuntimeError(
            "Backend service not initialized for MCP app. "
            "Call attach_backend() before running the server."
        )

    logger.info("MCP application lifespan starting...")
    try:
        yield AppContext(backend_service=backend)
    finally:
        logger.info("MCP application lifespan shutting down...")


mcp_app = FastMCP(
    name="LeanExploreMCPServer",
    instructions=SERVER_INSTRUCTIONS,
    lifespan=app_lifespan,
)
