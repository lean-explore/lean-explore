"""Tests for the reranker client module.

The Hugging Face tokenizer and model are replaced by small fakes so the real
scoring arithmetic (last-token logits, softmax over true/false) runs on tiny
tensors without downloading or loading any model weights.
"""

import math
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import torch

from lean_explore.util.reranker_client import (
    DEFAULT_CPU_BATCH_SIZE,
    DEFAULT_CUDA_BATCH_SIZE,
    DEFAULT_INSTRUCTION,
    RerankerClient,
    RerankerResponse,
)

FALSE_ID = 0
TRUE_ID = 1


class FakeEncoding(dict):
    """Tokenizer output that supports ``.to(device)`` like a BatchEncoding."""

    def to(self, device):
        """Record the target device and return self."""
        self.device = device
        return self


class FakeTokenizer:
    """Maps each formatted pair to a scalar "relevance" taken from its document."""

    def __init__(self, relevance: dict[str, float]):
        """Store the per-document relevance table."""
        self.relevance = relevance
        self.calls: list[tuple[list[str], dict]] = []

    def convert_tokens_to_ids(self, token: str) -> int:
        """Return the fixed ids used for the "true"/"false" tokens."""
        return {"true": TRUE_ID, "false": FALSE_ID}[token]

    def __call__(self, pairs, **kwargs):
        """Encode pairs as their documents' relevance values."""
        self.calls.append((list(pairs), kwargs))
        values = [self.relevance[p.split("<Document>: ", 1)[1]] for p in pairs]
        return FakeEncoding(input_ids=torch.tensor(values))


class FakeModel:
    """Causal LM whose last-token "true" logit equals the input relevance."""

    def __init__(self):
        """Initialize call tracking."""
        self.batch_sizes: list[int] = []
        self.device = None
        self.eval_called = False
        self.error: Exception | None = None

    def to(self, device):
        """Record the device and return self."""
        self.device = device
        return self

    def eval(self):
        """Record that eval mode was requested."""
        self.eval_called = True

    def __call__(self, input_ids):
        """Return logits of shape [batch, seq=2, vocab=2]."""
        if self.error is not None:
            raise self.error
        self.batch_sizes.append(len(input_ids))
        logits = torch.zeros(len(input_ids), 2, 2)
        logits[:, -1, TRUE_ID] = input_ids
        logits[:, 0, TRUE_ID] = -100.0  # Non-final tokens must be ignored.
        return SimpleNamespace(logits=logits)


RELEVANCE = {"a": 2.0, "b": -1.0, "c": 0.0, "d": 3.0, "e": -2.0}


def expected_score(relevance: float) -> float:
    """Softmax over (false=0, true=relevance) equals sigmoid(relevance)."""
    return 1.0 / (1.0 + math.exp(-relevance))


@pytest.fixture
def fakes():
    """Patch model loading and return (tokenizer, model, from_pretrained mocks)."""
    tokenizer = FakeTokenizer(RELEVANCE)
    model = FakeModel()
    with (
        patch("lean_explore.util.reranker_client.AutoTokenizer") as tok_cls,
        patch("lean_explore.util.reranker_client.AutoModelForCausalLM") as model_cls,
    ):
        tok_cls.from_pretrained.return_value = tokenizer
        model_cls.from_pretrained.return_value = model
        yield SimpleNamespace(
            tokenizer=tokenizer, model=model, tok_cls=tok_cls, model_cls=model_cls
        )


@pytest.fixture
def client(fakes, monkeypatch):
    """Build a CPU reranker on top of the fakes."""
    monkeypatch.delenv("LEAN_EXPLORE_RERANKER_BATCH_SIZE", raising=False)
    return RerankerClient(model_name="test-model", device="cpu", max_length=64)


class TestRerankerClientInit:
    """Tests for model loading and configuration."""

    def test_loads_tokenizer_and_model_for_cpu(self, fakes, client):
        """CPU uses float32, left padding, eval mode, and true/false token ids."""
        fakes.tok_cls.from_pretrained.assert_called_once_with(
            "test-model", padding_side="left", trust_remote_code=True
        )
        fakes.model_cls.from_pretrained.assert_called_once_with(
            "test-model", torch_dtype=torch.float32, trust_remote_code=True
        )
        assert fakes.model.device == "cpu"
        assert fakes.model.eval_called
        assert (client._token_true_id, client._token_false_id) == (TRUE_ID, FALSE_ID)
        assert client.instruction == DEFAULT_INSTRUCTION

    def test_cuda_uses_float16(self, fakes):
        """GPU loading uses half precision and moves the model to CUDA."""
        RerankerClient(model_name="test-model", device="cuda")
        kwargs = fakes.model_cls.from_pretrained.call_args.kwargs
        assert kwargs["torch_dtype"] == torch.float16
        assert fakes.model.device == "cuda"

    def test_auto_device_never_selects_mps(self, fakes):
        """The reranker falls back to CPU even when MPS is available."""
        with patch("lean_explore.util.device.torch") as mock_torch:
            mock_torch.cuda.is_available.return_value = False
            mock_torch.backends.mps.is_available.return_value = True
            assert RerankerClient(model_name="m").device == "cpu"

    def test_auto_device_prefers_cuda(self, fakes):
        """CUDA is chosen when available."""
        with patch("lean_explore.util.device.torch") as mock_torch:
            mock_torch.cuda.is_available.return_value = True
            assert RerankerClient(model_name="m").device == "cuda"

    @pytest.mark.parametrize(
        ("explicit", "env", "device", "expected"),
        [
            (7, "99", "cuda", 7),
            (None, "99", "cuda", 99),
            (None, "", "cuda", DEFAULT_CUDA_BATCH_SIZE),
            (None, None, "cuda", DEFAULT_CUDA_BATCH_SIZE),
            (None, None, "cpu", DEFAULT_CPU_BATCH_SIZE),
        ],
    )
    def test_batch_size_resolution(
        self, fakes, monkeypatch, explicit, env, device, expected
    ):
        """Explicit value beats the env var, which beats the device default."""
        if env is None:
            monkeypatch.delenv("LEAN_EXPLORE_RERANKER_BATCH_SIZE", raising=False)
        else:
            monkeypatch.setenv("LEAN_EXPLORE_RERANKER_BATCH_SIZE", env)
        client = RerankerClient(model_name="m", device=device, batch_size=explicit)
        assert client.batch_size == expected


class TestRerankerScoring:
    """Tests for score computation and batching."""

    def test_format_pair(self, client):
        """Pairs contain instruction, query, and document in order."""
        assert client._format_pair("q", "doc") == (
            f"<Instruct>: {DEFAULT_INSTRUCTION}\n<Query>: q\n<Document>: doc"
        )

    def test_rerank_sync_scores_in_input_order(self, fakes, client):
        """Scores are P(true) per document, aligned with the input order."""
        docs = ["a", "b", "c"]
        response = client.rerank_sync("query", docs)

        assert isinstance(response, RerankerResponse)
        assert response.query == "query"
        assert response.model == "test-model"
        assert response.scores == pytest.approx(
            [expected_score(RELEVANCE[d]) for d in docs]
        )
        assert response.scores[0] > response.scores[2] > response.scores[1]

    def test_tokenizer_receives_truncation_settings(self, fakes, client):
        """Tokenization pads, truncates to max_length, and targets the device."""
        client.rerank_sync("query", ["a"])
        pairs, kwargs = fakes.tokenizer.calls[0]
        assert pairs == [client._format_pair("query", "a")]
        assert kwargs == {
            "padding": True,
            "truncation": True,
            "max_length": 64,
            "return_tensors": "pt",
        }

    async def test_rerank_batches_and_preserves_order(self, fakes, client):
        """Large inputs are split into batches whose scores are concatenated."""
        docs = ["a", "b", "c", "d", "e"]
        response = await client.rerank("query", docs, batch_size=2)

        assert fakes.model.batch_sizes == [2, 2, 1]
        assert response.scores == pytest.approx(
            [expected_score(RELEVANCE[d]) for d in docs]
        )

    async def test_rerank_small_input_uses_single_call(self, fakes, client):
        """Inputs no larger than the batch size are scored in one pass."""
        await client.rerank("query", ["a", "b"], batch_size=2)
        assert fakes.model.batch_sizes == [2]

    async def test_rerank_uses_client_default_batch_size(self, fakes, client):
        """When batch_size is omitted, the client's default is used."""
        client.batch_size = 2
        await client.rerank("query", ["a", "b", "c"])
        assert fakes.model.batch_sizes == [2, 1]

    async def test_rerank_empty_documents_skips_model(self, fakes, client):
        """Empty input returns empty scores without touching the model."""
        response = await client.rerank("query", [])
        assert response.scores == []
        assert client.rerank_sync("query", []).scores == []
        assert fakes.model.batch_sizes == []
        assert fakes.tokenizer.calls == []

    async def test_rerank_propagates_model_errors(self, fakes, client):
        """Errors raised during batched inference reach the caller."""
        fakes.model.error = RuntimeError("out of memory")
        with pytest.raises(RuntimeError, match="out of memory"):
            await client.rerank("query", ["a", "b", "c"], batch_size=1)


class TestRerankerClientIntegration:
    """Integration tests that load actual models."""

    @pytest.mark.external
    @pytest.mark.slow
    async def test_rerank_real_model(self):
        """Test reranking with a real model."""
        client = RerankerClient(
            model_name="Qwen/Qwen3-Reranker-0.6B",
            max_length=256,
        )

        response = await client.rerank(
            query="natural number addition",
            documents=[
                "Nat.add: Adds two natural numbers",
                "String.length: Returns length of string",
                "List.map: Maps a function over a list",
            ],
        )

        assert len(response.scores) == 3
        assert response.scores[0] > response.scores[1]
        assert response.scores[0] > response.scores[2]
