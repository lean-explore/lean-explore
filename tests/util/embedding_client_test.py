"""Tests for the embedding client module.

``SentenceTransformer`` is replaced by a stub so no model is ever loaded.
"""

from unittest.mock import patch

import numpy as np
import pytest

from lean_explore.util.embedding_client import (
    DEFAULT_BATCH_SIZE,
    EmbeddingClient,
    EmbeddingResponse,
)


class StubSentenceTransformer:
    """Records encode calls and returns one deterministic vector per text."""

    def __init__(self, model_name, device):
        """Record construction arguments."""
        self.model_name = model_name
        self.device = device
        self.encode_calls: list[tuple[list[str], dict]] = []
        self.error: Exception | None = None

    def encode(self, texts, **kwargs):
        """Return a [len(texts), 3] array whose rows identify each text."""
        if self.error is not None:
            raise self.error
        self.encode_calls.append((list(texts), kwargs))
        return np.array([[float(len(t)), float(i), 0.5] for i, t in enumerate(texts)])


@pytest.fixture
def stub_st():
    """Patch SentenceTransformer with the stub class."""
    with patch(
        "lean_explore.util.embedding_client.SentenceTransformer",
        StubSentenceTransformer,
    ):
        yield


@pytest.fixture
def client(stub_st, monkeypatch):
    """Create a CPU embedding client backed by the stub."""
    monkeypatch.delenv("LEAN_EXPLORE_EMBEDDING_BATCH_SIZE", raising=False)
    return EmbeddingClient(model_name="test-model", device="cpu")


class TestEmbeddingClientInit:
    """Tests for EmbeddingClient initialization."""

    @pytest.mark.parametrize(
        ("cuda", "mps", "expected"),
        [(True, True, "cuda"), (False, True, "mps"), (False, False, "cpu")],
    )
    def test_auto_device_selection(self, stub_st, cuda, mps, expected):
        """Device auto-detection prefers CUDA, then MPS, then CPU."""
        with patch("lean_explore.util.device.torch") as mock_torch:
            mock_torch.cuda.is_available.return_value = cuda
            mock_torch.backends.mps.is_available.return_value = mps
            client = EmbeddingClient(model_name="test-model")
        assert client.device == expected
        assert client.model.device == expected

    def test_explicit_device_is_passed_to_model(self, stub_st):
        """An explicit device skips detection and is forwarded to the model."""
        with patch("lean_explore.util.device.torch") as mock_torch:
            client = EmbeddingClient(model_name="test-model", device="cpu")
            mock_torch.cuda.is_available.assert_not_called()
        assert client.model.model_name == "test-model"
        assert client.model.device == "cpu"

    def test_max_length_sets_model_limit(self, stub_st):
        """max_length is applied to the model's max_seq_length."""
        client = EmbeddingClient(model_name="m", device="cpu", max_length=256)
        assert client.model.max_seq_length == 256

    def test_max_length_none_leaves_model_default(self, stub_st):
        """Without max_length the model's own limit is untouched."""
        client = EmbeddingClient(model_name="m", device="cpu")
        assert not hasattr(client.model, "max_seq_length")

    @pytest.mark.parametrize(
        ("explicit", "env", "expected"),
        [(4, "64", 4), (None, "64", 64), (None, None, DEFAULT_BATCH_SIZE)],
    )
    def test_batch_size_resolution(self, stub_st, monkeypatch, explicit, env, expected):
        """Explicit batch size beats the env var, which beats the default."""
        if env is None:
            monkeypatch.delenv("LEAN_EXPLORE_EMBEDDING_BATCH_SIZE", raising=False)
        else:
            monkeypatch.setenv("LEAN_EXPLORE_EMBEDDING_BATCH_SIZE", env)
        client = EmbeddingClient(model_name="m", device="cpu", batch_size=explicit)
        assert client.batch_size == expected


class TestEmbeddingClientEmbed:
    """Tests for EmbeddingClient.embed."""

    async def test_embed_returns_one_list_per_text_in_order(self, client):
        """Embeddings are plain float lists aligned with the input texts."""
        response = await client.embed(["hi", "world"])

        assert isinstance(response, EmbeddingResponse)
        assert response.texts == ["hi", "world"]
        assert response.model == "test-model"
        assert response.embeddings == [[2.0, 0.0, 0.5], [5.0, 1.0, 0.5]]
        assert all(isinstance(v, float) for v in response.embeddings[0])

    async def test_document_encoding_has_no_prompt(self, client):
        """Documents are encoded without a prompt, using the batch size."""
        await client.embed(["document"])
        texts, kwargs = client.model.encode_calls[0]
        assert texts == ["document"]
        assert kwargs == {
            "show_progress_bar": False,
            "convert_to_numpy": True,
            "batch_size": DEFAULT_BATCH_SIZE,
        }

    async def test_query_encoding_uses_query_prompt(self, client):
        """is_query=True selects the model's "query" prompt."""
        await client.embed(["query"], is_query=True)
        _, kwargs = client.model.encode_calls[0]
        assert kwargs["prompt_name"] == "query"

    async def test_empty_input(self, client):
        """An empty list yields an empty response."""
        response = await client.embed([])
        assert response.texts == []
        assert response.embeddings == []

    async def test_encode_errors_propagate(self, client):
        """Errors raised by the model reach the caller."""
        client.model.error = RuntimeError("CUDA out of memory")
        with pytest.raises(RuntimeError, match="CUDA out of memory"):
            await client.embed(["x"])


class TestEmbeddingResponse:
    """Tests for EmbeddingResponse validation."""

    def test_rejects_non_numeric_embeddings(self):
        """Pydantic validation rejects malformed embeddings."""
        with pytest.raises(ValueError):
            EmbeddingResponse(texts=["a"], embeddings=[["x"]], model="m")


class TestEmbeddingClientIntegration:
    """Integration tests that load actual models."""

    @pytest.mark.external
    @pytest.mark.slow
    async def test_embed_real_model(self):
        """Test embedding generation with a small real model."""
        client = EmbeddingClient(
            model_name="sentence-transformers/all-MiniLM-L6-v2",
            max_length=128,
        )

        response = await client.embed(["Hello, world!", "Test sentence"])

        assert len(response.embeddings) == 2
        assert len(response.embeddings[0]) == 384
        assert response.embeddings[0] != response.embeddings[1]
