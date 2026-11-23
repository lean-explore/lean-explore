"""Embedding generation client using sentence transformers."""

import asyncio
import logging
import os

from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

from lean_explore.util.device import select_device

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 8


class EmbeddingResponse(BaseModel):
    """Response from embedding generation."""

    texts: list[str]
    """Original input texts."""

    embeddings: list[list[float]]
    """List of embeddings (one per input text)."""

    model: str
    """Model name used for generation."""


class EmbeddingClient:
    """Client for generating text embeddings."""

    def __init__(
        self,
        model_name: str,
        device: str | None = None,
        max_length: int | None = None,
        batch_size: int | None = None,
    ):
        """Initialize the embedding client.

        Args:
            model_name: Name of the sentence transformer model
            device: Device to use ("cuda", "mps", "cpu"). Auto-detects if None.
            max_length: Maximum sequence length for tokenization. If None, uses
                model default. Lower values reduce memory usage.
            batch_size: Batch size for encode calls. Falls back to the
                LEAN_EXPLORE_EMBEDDING_BATCH_SIZE env var, then to
                DEFAULT_BATCH_SIZE. Raise on large-VRAM hardware for better
                throughput.
        """
        self.model_name = model_name
        self.device = device or self._select_device()
        self.max_length = max_length
        self.batch_size = batch_size or int(
            os.getenv("LEAN_EXPLORE_EMBEDDING_BATCH_SIZE", DEFAULT_BATCH_SIZE)
        )
        logger.info("Loading embedding model %s on %s", model_name, self.device)
        self.model = SentenceTransformer(model_name, device=self.device)

        # Set max sequence length if specified
        if max_length is not None:
            self.model.max_seq_length = max_length
            logger.info("Set max sequence length to %d", max_length)

    def _select_device(self) -> str:
        """Select best available device (CUDA, then MPS, then CPU)."""
        return select_device(allow_mps=True)

    async def embed(
        self, texts: list[str], is_query: bool = False
    ) -> EmbeddingResponse:
        """Generate embeddings for a list of texts.

        Args:
            texts: List of text strings to embed
            is_query: If True, encode as search queries using the model's query
                prompt. If False (default), encode as documents without prompt.
                Qwen3-Embedding models are asymmetric and perform better when
                queries use prompt_name="query".

        Returns:
            EmbeddingResponse with texts, embeddings, and model info
        """
        loop = asyncio.get_running_loop()

        def _encode():
            # Use query prompt for search queries, no prompt for documents
            encode_kwargs = {
                "show_progress_bar": False,
                "convert_to_numpy": True,
                "batch_size": self.batch_size,
            }
            if is_query:
                encode_kwargs["prompt_name"] = "query"
            return self.model.encode(texts, **encode_kwargs)

        embeddings = await loop.run_in_executor(None, _encode)
        return EmbeddingResponse(
            texts=texts,
            embeddings=[emb.tolist() for emb in embeddings],
            model=self.model_name,
        )
