"""Response types returned by the MCP tools.

These TypedDicts define the structured output schemas that MCP clients see.
Their class names appear in the published schemas, so rename them only as a
deliberate interface change.
"""

from typing_extensions import TypedDict


class SearchResultSummaryDict(TypedDict, total=False):
    """Serialized SearchResultSummary for slim MCP search responses."""

    id: int
    name: str
    description: str | None


class SearchSummaryResponseDict(TypedDict, total=False):
    """Serialized SearchSummaryResponse for slim MCP search responses."""

    query: str
    results: list[SearchResultSummaryDict]
    count: int
    processing_time_ms: int | None


class SearchResultDict(TypedDict, total=False):
    """Serialized SearchResult for verbose MCP tool responses."""

    id: int
    name: str
    module: str
    docstring: str | None
    source_text: str
    source_link: str
    dependencies: str | None
    informalization: str | None


class SearchResponseDict(TypedDict, total=False):
    """Serialized SearchResponse for verbose MCP tool responses."""

    query: str
    results: list[SearchResultDict]
    count: int
    processing_time_ms: int | None


class SourceCodeResultDict(TypedDict):
    """Result containing declaration id, name, and source code."""

    id: int
    name: str
    source_text: str


class SourceLinkResultDict(TypedDict):
    """Result containing declaration id, name, and GitHub source link."""

    id: int
    name: str
    source_link: str


class DocstringResultDict(TypedDict):
    """Result containing declaration id, name, and docstring."""

    id: int
    name: str
    docstring: str | None


class DescriptionResultDict(TypedDict):
    """Result containing declaration id, name, and informalization."""

    id: int
    name: str
    informalization: str | None


class ModuleResultDict(TypedDict):
    """Result containing declaration id, name, and module path."""

    id: int
    name: str
    module: str


class DependenciesResultDict(TypedDict):
    """Result containing declaration id, name, and dependencies."""

    id: int
    name: str
    dependencies: str | None
