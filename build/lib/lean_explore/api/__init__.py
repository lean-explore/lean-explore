"""Remote API client package for Lean Explore.

This package provides an async HTTP client for connecting to the remote
Lean Explore API service as an alternative to local search.

Modules:
    client: ApiClient class for search and declaration retrieval via HTTP.
"""

from lean_explore.api.client import ApiClient

__all__ = ["ApiClient"]
