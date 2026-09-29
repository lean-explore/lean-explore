"""Tests for the remote embedding client.

``requests.post`` is replaced by a stub that returns genuine
``requests.Response`` objects, so status handling and JSON decoding run for
real without any network access.
"""

import json

import pytest
import requests

from lean_explore.util.embedding_client import EmbeddingResponse
from lean_explore.util.remote_embedding_client import RemoteEmbeddingClient


def make_response(status: int, body: object, url: str) -> requests.Response:
    """Build a real Response with the given status and JSON body."""
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    response.url = url
    response.reason = "Error" if status >= 400 else "OK"
    return response


@pytest.fixture
def server(monkeypatch):
    """Stub the embed endpoint; returns mutable state with recorded requests."""
    state = {"calls": [], "status": 200, "body": None}

    def fake_post(url, json, timeout):
        state["calls"].append({"url": url, "json": json, "timeout": timeout})
        body = state["body"]
        if body is None:
            body = {"embeddings": [[float(len(t)), 1.0] for t in json["texts"]]}
        return make_response(state["status"], body, url)

    monkeypatch.setattr(
        "lean_explore.util.remote_embedding_client.requests.post", fake_post
    )
    return state


class TestRemoteEmbeddingClient:
    """Tests for RemoteEmbeddingClient."""

    def test_endpoint_strips_trailing_slash(self):
        """The endpoint is built from the base URL without double slashes."""
        client = RemoteEmbeddingClient("http://gpu:5001/", timeout=5)
        assert client.server_url == "http://gpu:5001"
        assert client.endpoint == "http://gpu:5001/api/v2/embed"
        assert client.timeout == 5
        assert client.model_name == "remote"

    async def test_embed_posts_texts_and_returns_embeddings(self, server):
        """Texts are posted as JSON and the server's vectors are returned."""
        client = RemoteEmbeddingClient("http://gpu:5001")
        response = await client.embed(["ab", "abcd"], is_query=True)

        assert server["calls"] == [
            {
                "url": "http://gpu:5001/api/v2/embed",
                "json": {"texts": ["ab", "abcd"]},
                "timeout": 120,
            }
        ]
        assert isinstance(response, EmbeddingResponse)
        assert response.texts == ["ab", "abcd"]
        assert response.embeddings == [[2.0, 1.0], [4.0, 1.0]]
        assert response.model == "remote"

    async def test_embed_uses_server_model_name(self, server):
        """The model name reported by the server is propagated."""
        server["body"] = {"embeddings": [[0.1]], "model": "Qwen3-Embedding"}
        response = await RemoteEmbeddingClient("http://x").embed(["t"])
        assert response.model == "Qwen3-Embedding"

    async def test_embed_empty_input(self, server):
        """An empty batch round-trips to an empty response."""
        response = await RemoteEmbeddingClient("http://x").embed([])
        assert response.embeddings == []
        assert server["calls"][0]["json"] == {"texts": []}

    async def test_http_error_propagates(self, server):
        """Non-2xx responses raise requests.HTTPError."""
        server["status"] = 503
        server["body"] = {"detail": "overloaded"}
        with pytest.raises(requests.HTTPError, match="503"):
            await RemoteEmbeddingClient("http://x").embed(["t"])

    async def test_malformed_response_raises(self, server):
        """A 200 response without embeddings raises KeyError."""
        server["body"] = {"unexpected": True}
        with pytest.raises(KeyError, match="embeddings"):
            await RemoteEmbeddingClient("http://x").embed(["t"])

    async def test_connection_error_propagates(self, monkeypatch):
        """Transport failures are not swallowed."""

        def refuse(*args, **kwargs):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(
            "lean_explore.util.remote_embedding_client.requests.post", refuse
        )
        with pytest.raises(requests.ConnectionError):
            await RemoteEmbeddingClient("http://x").embed(["t"])
