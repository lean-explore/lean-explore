"""Tests for the API client module.

HTTP is served by ``httpx.MockTransport`` so real request construction, status
handling, and JSON decoding run without network access.
"""

import httpx
import pytest

from lean_explore.api.client import ApiClient
from lean_explore.config import Config
from lean_explore.models import SearchResponse, SearchResult


def declaration(declaration_id: int, name: str) -> dict:
    """Build a declaration payload as returned by the API."""
    return {
        "id": declaration_id,
        "name": name,
        "module": "Init.Data.Nat.Basic",
        "docstring": f"Docs for {name}",
        "source_text": f"def {name} := ...",
        "source_link": f"https://github.com/example#L{declaration_id}",
        "dependencies": None,
        "informalization": f"Informal {name}",
    }


@pytest.fixture
def api(monkeypatch):
    """Route ApiClient traffic to a programmable MockTransport.

    Returns a state dict: set ``handler`` to a function taking an
    ``httpx.Request`` and returning an ``httpx.Response``; sent requests and
    client constructor kwargs are recorded in ``requests`` and ``client_kwargs``.
    """
    state: dict = {"requests": [], "client_kwargs": [], "handler": None}
    real_async_client = httpx.AsyncClient

    def dispatch(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return state["handler"](request)

    def make_client(**kwargs):
        state["client_kwargs"].append(kwargs)
        return real_async_client(transport=httpx.MockTransport(dispatch), **kwargs)

    monkeypatch.setattr("lean_explore.api.client.httpx.AsyncClient", make_client)
    return state


class TestApiClientInit:
    """Tests for ApiClient initialization."""

    def test_legacy_api_key_is_accepted_and_ignored(self, monkeypatch):
        """Neither the argument nor the env var produces credentials."""
        monkeypatch.setenv("LEANEXPLORE_API_KEY", "env-key")
        client = ApiClient(api_key="param-key")
        assert client.api_key is None
        assert client._headers == {}

    def test_defaults(self):
        """Base URL comes from Config and the timeout defaults to 10s."""
        client = ApiClient()
        assert client.base_url == Config.API_BASE_URL
        assert client.timeout == 10.0

    def test_custom_timeout(self):
        """A custom timeout is stored."""
        assert ApiClient(timeout=30.0).timeout == 30.0


class TestApiClientSearch:
    """Tests for ApiClient.search."""

    async def test_search_parses_results(self, api):
        """Results are parsed into SearchResult objects in server order."""
        api["handler"] = lambda request: httpx.Response(
            200,
            json={
                "results": [declaration(1, "Nat.add"), declaration(2, "Nat.mul")],
                "processing_time_ms": 42,
            },
        )
        response = await ApiClient().search("natural numbers", limit=10)

        assert isinstance(response, SearchResponse)
        assert response.query == "natural numbers"
        assert [r.name for r in response.results] == ["Nat.add", "Nat.mul"]
        assert all(isinstance(r, SearchResult) for r in response.results)
        assert response.count == 2
        assert response.processing_time_ms == 42

    async def test_search_request_shape(self, api):
        """The request hits /search with query, limit, and no auth header."""
        api["handler"] = lambda request: httpx.Response(200, json={"results": []})
        await ApiClient(api_key="ignored", timeout=3.0).search("q", rerank_top=5)

        (request,) = api["requests"]
        assert str(request.url).startswith(f"{Config.API_BASE_URL}/search?")
        assert dict(request.url.params) == {"q": "q", "limit": "20"}
        assert "authorization" not in request.headers
        assert api["client_kwargs"] == [{"timeout": 3.0}]

    async def test_search_joins_packages(self, api):
        """Package filters are sent as one comma-separated parameter."""
        api["handler"] = lambda request: httpx.Response(200, json={"results": []})
        await ApiClient().search("q", limit=5, packages=["Mathlib", "Batteries"])

        params = dict(api["requests"][0].url.params)
        assert params == {"q": "q", "limit": "5", "packages": "Mathlib,Batteries"}

    async def test_search_empty_packages_omitted(self, api):
        """An empty package list sends no packages parameter."""
        api["handler"] = lambda request: httpx.Response(200, json={"results": []})
        await ApiClient().search("q", packages=[])
        assert "packages" not in api["requests"][0].url.params

    async def test_search_missing_fields_default(self, api):
        """A body without results or timing yields an empty response."""
        api["handler"] = lambda request: httpx.Response(200, json={})
        response = await ApiClient().search("q")
        assert response.results == []
        assert response.count == 0
        assert response.processing_time_ms is None

    async def test_search_http_error(self, api):
        """Non-2xx statuses raise HTTPStatusError."""
        api["handler"] = lambda request: httpx.Response(500)
        with pytest.raises(httpx.HTTPStatusError):
            await ApiClient().search("q")

    async def test_search_network_error(self, api):
        """Transport failures raise RequestError."""

        def fail(request):
            raise httpx.ConnectError("refused", request=request)

        api["handler"] = fail
        with pytest.raises(httpx.RequestError):
            await ApiClient().search("q")


class TestApiClientGetById:
    """Tests for ApiClient.get_by_id."""

    async def test_get_by_id_found(self, api):
        """A 200 response is parsed into a SearchResult."""
        api["handler"] = lambda request: httpx.Response(
            200, json=declaration(42, "List.map")
        )
        result = await ApiClient().get_by_id(42)

        assert isinstance(result, SearchResult)
        assert (result.id, result.name) == (42, "List.map")
        request = api["requests"][0]
        assert str(request.url) == f"{Config.API_BASE_URL}/declarations/42"
        assert "authorization" not in request.headers

    async def test_get_by_id_not_found(self, api):
        """A 404 returns None rather than raising."""
        api["handler"] = lambda request: httpx.Response(404)
        assert await ApiClient().get_by_id(99999) is None

    async def test_get_by_id_http_error(self, api):
        """Other error statuses propagate."""
        api["handler"] = lambda request: httpx.Response(503)
        with pytest.raises(httpx.HTTPStatusError):
            await ApiClient().get_by_id(42)
