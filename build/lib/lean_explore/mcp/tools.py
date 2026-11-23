"""Defines MCP tools for interacting with the Lean Explore search engine."""

import logging
from typing import Any, cast

from mcp.server.fastmcp import Context as MCPContext
from mcp.types import ToolAnnotations

from lean_explore.mcp.app import AppContext, mcp_app
from lean_explore.mcp.backend import SearchBackend
from lean_explore.mcp.schemas import (
    DependenciesResultDict,
    DescriptionResultDict,
    DocstringResultDict,
    ModuleResultDict,
    SearchResponseDict,
    SearchSummaryResponseDict,
    SourceCodeResultDict,
    SourceLinkResultDict,
)
from lean_explore.models import SearchResponse
from lean_explore.models.search_types import (
    SearchResultSummary,
    SearchSummaryResponse,
    extract_bold_description,
)

logger = logging.getLogger(__name__)

READ_ONLY_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _get_backend(ctx: MCPContext) -> SearchBackend:
    """Return the backend service stored in the MCP lifespan context.

    Args:
        ctx: The MCP context provided to the tool.

    Returns:
        The configured backend service.
    """
    app_context: AppContext = ctx.request_context.lifespan_context
    return app_context.backend_service


async def _run_search(
    ctx: MCPContext,
    tool_name: str,
    query: str,
    limit: int,
    rerank_top: int | None,
    packages: list[str] | None,
) -> SearchResponse:
    """Log a search tool call and run the search on the backend.

    Args:
        ctx: The MCP context, providing access to the backend service.
        tool_name: Name of the calling tool, used for logging.
        query: The search query string.
        limit: Maximum number of results.
        rerank_top: Number of candidates to rerank with the cross-encoder.
        packages: Optional package filter.

    Returns:
        The search response from the backend.
    """
    logger.info(
        "MCP tool '%s' called with query: '%s', limit: %d, rerank_top: %s, "
        "packages: %s",
        tool_name,
        query,
        limit,
        rerank_top,
        packages,
    )
    return await _get_backend(ctx).search(
        query=query, limit=limit, rerank_top=rerank_top, packages=packages
    )


async def _fetch_field(
    ctx: MCPContext, tool_name: str, declaration_id: int, field: str
) -> dict[str, Any] | None:
    """Fetch one field of a declaration, together with its id and name.

    Args:
        ctx: The MCP context, providing access to the backend service.
        tool_name: Name of the calling tool, used for logging.
        declaration_id: The numeric id of the declaration.
        field: The SearchResult attribute to return.

    Returns:
        A dictionary with id, name, and the requested field, or None if the id
        does not exist.
    """
    logger.info(
        "MCP tool '%s' called for declaration_id: %d", tool_name, declaration_id
    )
    result = await _get_backend(ctx).get_by_id(declaration_id=declaration_id)
    if result is None:
        return None
    return {"id": result.id, "name": result.name, field: getattr(result, field)}


@mcp_app.tool(
    title="[Deprecated] Search with full results",
    description=(
        "DEPRECATED: Use search_summary, then fetch needed fields with the "
        "per-field tools. This compatibility tool returns every field for every "
        "match and may consume substantially more context."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
    meta={"deprecated": True, "replacement": "search_summary"},
)
async def search(
    ctx: MCPContext,
    query: str,
    limit: int = 10,
    rerank_top: int | None = 50,
    packages: list[str] | None = None,
) -> SearchResponseDict:
    """Deprecated MCP compatibility tool returning full search results.

    Use search_summary followed by the per-field tools for all new workflows.
    This function remains available so existing MCP clients do not break.

    Accepts two kinds of queries:
      - By name: a full or partial Lean declaration name, e.g., "List.map",
        "Nat.Prime", "CategoryTheory.Functor.map".
      - By meaning: an informal natural language description, e.g.,
        "continuous function on a compact set", "sum of a geometric series",
        "a group homomorphism preserving multiplication".

    The search engine handles both styles simultaneously via hybrid retrieval
    (lexical name matching + semantic similarity), so you do not need to
    specify which kind of query you are making.

    Returns full results including source code, module, dependencies, and
    informalization for every hit.

    Args:
        ctx: The MCP context, providing access to the backend service.
        query: A Lean declaration name (e.g., "List.filter") or an informal
            natural language description (e.g., "prime number divisibility").
        limit: The maximum number of search results to return. Defaults to 10.
        rerank_top: Number of candidates to rerank with cross-encoder. Set to 0 or
            None to skip reranking. Defaults to 50. Only used with local backend.
        packages: Filter results to specific packages (e.g., ["Mathlib", "Std"]).
            Defaults to None (all packages).

    Returns:
        A dictionary containing the full search response with all fields.
    """
    response = await _run_search(ctx, "search", query, limit, rerank_top, packages)
    return cast(SearchResponseDict, response.model_dump(exclude_none=True))


@mcp_app.tool(
    title="Search Lean declaration summaries",
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def search_summary(
    ctx: MCPContext,
    query: str,
    limit: int = 10,
    rerank_top: int | None = 50,
    packages: list[str] | None = None,
) -> SearchSummaryResponseDict:
    """Search Lean 4 declarations and return concise results (recommended first step).

    This is the preferred starting point for search. Returns only id, name,
    and a short natural language description for each hit, keeping token
    usage low. After reviewing these summaries, use the per-field tools
    (get_source_code, get_docstring, get_description, get_module,
    get_dependencies) for the entries you need details on.

    Accepts two kinds of queries:
      - By name: a full or partial Lean declaration name, e.g., "List.map",
        "Nat.Prime", "CategoryTheory.Functor.map".
      - By meaning: an informal natural language description, e.g.,
        "continuous function on a compact set", "sum of a geometric series",
        "a group homomorphism preserving multiplication".

    The search engine handles both styles simultaneously via hybrid retrieval
    (lexical name matching + semantic similarity), so you do not need to
    specify which kind of query you are making.

    Args:
        ctx: The MCP context, providing access to the backend service.
        query: A Lean declaration name (e.g., "List.filter") or an informal
            natural language description (e.g., "prime number divisibility").
        limit: The maximum number of search results to return. Defaults to 10.
        rerank_top: Number of candidates to rerank with cross-encoder. Set to 0 or
            None to skip reranking. Defaults to 50. Only used with local backend.
        packages: Filter results to specific packages (e.g., ["Mathlib", "Std"]).
            Defaults to None (all packages).

    Returns:
        A dictionary containing slim search results with id, name, and description.
    """
    response = await _run_search(
        ctx, "search_summary", query, limit, rerank_top, packages
    )
    summary_response = SearchSummaryResponse(
        query=response.query,
        results=[
            SearchResultSummary(
                id=result.id,
                name=result.name,
                description=extract_bold_description(result.informalization),
            )
            for result in response.results
        ],
        count=response.count,
        processing_time_ms=response.processing_time_ms,
    )
    return cast(
        SearchSummaryResponseDict, summary_response.model_dump(exclude_none=True)
    )


@mcp_app.tool(
    title="Get Lean source code",
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def get_source_code(
    ctx: MCPContext,
    declaration_id: int,
) -> SourceCodeResultDict | None:
    """Retrieve the Lean source code for a declaration by id.

    Returns the declaration name and its Lean 4 source code. Use this after
    calling search_summary to inspect the actual implementation.

    The id values come from the search_summary result list.

    Args:
        ctx: The MCP context, providing access to the backend service.
        declaration_id: The numeric id from a search_summary result.

    Returns:
        A dictionary with id, name, and source_text, or None if the id
        does not exist.
    """
    result = await _fetch_field(ctx, "get_source_code", declaration_id, "source_text")
    return cast("SourceCodeResultDict | None", result)


@mcp_app.tool(
    title="Get source link",
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def get_source_link(
    ctx: MCPContext,
    declaration_id: int,
) -> SourceLinkResultDict | None:
    """Retrieve the GitHub source link for a declaration by id.

    Returns the declaration name and a URL to the source code on GitHub.
    Use this when you need to reference or link to the original source.

    The id values come from the search_summary result list.

    Args:
        ctx: The MCP context, providing access to the backend service.
        declaration_id: The numeric id from a search_summary result.

    Returns:
        A dictionary with id, name, and source_link, or None if the id
        does not exist.
    """
    result = await _fetch_field(ctx, "get_source_link", declaration_id, "source_link")
    return cast("SourceLinkResultDict | None", result)


@mcp_app.tool(
    title="Get declaration docstring",
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def get_docstring(
    ctx: MCPContext,
    declaration_id: int,
) -> DocstringResultDict | None:
    """Retrieve the docstring for a declaration by id.

    Returns the declaration name and its documentation string from the Lean
    source code. Use this to check what documentation exists without
    fetching the full source code.

    The id values come from the search_summary result list.

    Args:
        ctx: The MCP context, providing access to the backend service.
        declaration_id: The numeric id from a search_summary result.

    Returns:
        A dictionary with id, name, and docstring, or None if the id
        does not exist.
    """
    result = await _fetch_field(ctx, "get_docstring", declaration_id, "docstring")
    return cast("DocstringResultDict | None", result)


@mcp_app.tool(
    title="Get declaration description",
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def get_description(
    ctx: MCPContext,
    declaration_id: int,
) -> DescriptionResultDict | None:
    """Retrieve the natural language description for a declaration by id.

    Returns the declaration name and its informalization, an AI-generated
    plain-English explanation of what the declaration states or does.

    The id values come from the search_summary result list.

    Args:
        ctx: The MCP context, providing access to the backend service.
        declaration_id: The numeric id from a search_summary result.

    Returns:
        A dictionary with id, name, and informalization, or None if the id
        does not exist.
    """
    result = await _fetch_field(
        ctx, "get_description", declaration_id, "informalization"
    )
    return cast("DescriptionResultDict | None", result)


@mcp_app.tool(
    title="Get declaration module",
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def get_module(
    ctx: MCPContext,
    declaration_id: int,
) -> ModuleResultDict | None:
    """Retrieve the module path for a declaration by id.

    Returns the declaration name and the Lean module it belongs to
    (e.g., 'Mathlib.Data.List.Basic'). Use this to find where a
    declaration lives in the package structure.

    The id values come from the search_summary result list.

    Args:
        ctx: The MCP context, providing access to the backend service.
        declaration_id: The numeric id from a search_summary result.

    Returns:
        A dictionary with id, name, and module, or None if the id does
        not exist.
    """
    result = await _fetch_field(ctx, "get_module", declaration_id, "module")
    return cast("ModuleResultDict | None", result)


@mcp_app.tool(
    title="Get declaration dependencies",
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def get_dependencies(
    ctx: MCPContext,
    declaration_id: int,
) -> DependenciesResultDict | None:
    """Retrieve the dependencies for a declaration by id.

    Returns the declaration name and a JSON array of other declaration
    names that this declaration depends on. Use this to understand what
    a declaration builds upon.

    The id values come from the search_summary result list.

    Args:
        ctx: The MCP context, providing access to the backend service.
        declaration_id: The numeric id from a search_summary result.

    Returns:
        A dictionary with id, name, and dependencies, or None if the id
        does not exist.
    """
    result = await _fetch_field(ctx, "get_dependencies", declaration_id, "dependencies")
    return cast("DependenciesResultDict | None", result)
