"""Type definitions for search results and related data structures."""

import re

from pydantic import BaseModel, ConfigDict


def extract_bold_description(informalization: str | None) -> str | None:
    """Extract the bold header text from an informalization string.

    Informalizations follow the pattern: **Bold Title.** Rest of description...
    This function extracts just the bold title portion.

    Args:
        informalization: The full informalization text, or None.

    Returns:
        The bold header text (without ** markers), or None if no bold
        header is found or input is None.
    """
    if not informalization:
        return None
    match = re.match(r"\*\*(.+?)\*\*", informalization)
    return match.group(1) if match else None


class SearchResultSummary(BaseModel):
    """A slim search result containing only identification and description.

    Used by the MCP search tool to return concise results that minimize
    token usage. Consumers can use the id to fetch specific fields via the
    per-field tools (get_source_code, get_docstring, etc.).
    """

    id: int
    """Primary key identifier."""

    name: str
    """Fully qualified Lean name (e.g., 'Nat.add')."""

    description: str | None
    """Short description extracted from the informalization bold header."""


class SearchSummaryResponse(BaseModel):
    """Response from a slim search operation containing summary results."""

    query: str
    """The original search query string."""

    results: list[SearchResultSummary]
    """List of slim search results."""

    count: int
    """Number of results returned."""

    processing_time_ms: int | None = None
    """Processing time in milliseconds, if available."""


class SearchResult(BaseModel):
    """A search result representing a Lean declaration.

    This model represents the core information returned from a search query,
    mirroring the essential fields from the database Declaration model.
    """

    id: int
    """Primary key identifier."""

    name: str
    """Fully qualified Lean name (e.g., 'Nat.add')."""

    module: str
    """Module name (e.g., 'Mathlib.Data.List.Basic')."""

    docstring: str | None
    """Documentation string from the source code, if available."""

    source_text: str
    """The actual Lean source code for this declaration."""

    source_link: str
    """GitHub URL to the declaration source code."""

    dependencies: str | None
    """JSON array of declaration names this declaration depends on."""

    informalization: str | None
    """Natural language description of the declaration."""

    model_config = ConfigDict(from_attributes=True)


class SearchResponse(BaseModel):
    """Response from a search operation containing results and metadata."""

    query: str
    """The original search query string."""

    results: list[SearchResult]
    """List of search results."""

    count: int
    """Number of results returned."""

    processing_time_ms: int | None = None
    """Processing time in milliseconds, if available."""
