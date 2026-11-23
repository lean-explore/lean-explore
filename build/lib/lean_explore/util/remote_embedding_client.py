"""Remote embedding client that delegates to a running backend server."""

import logging

import requests

from lean_explore.util.embedding_client import EmbeddingResponse

logger = logging.getLogger(__name__)


class RemoteEmbeddingClient:
    """Client that generates embeddings via a remote backend endpoint.

    Provides the same interface as EmbeddingClient but avoids loading the
    model locally, instead delegating to an already-running server that has
    the model loaded on GPU.
    """

    def __init__(self, server_url: str, timeout: int = 120):
        """Initialize the remote embedding client.

        Args:
            server_url: Base URL of the backend server (e.g. http://localhost:5001).
            timeout: Request timeout in seconds.
        """
        self.server_url = server_url.rstrip("/")
        self.endpoint = f"{self.server_url}/api/v2/embed"
        self.timeout = timeout
        self.model_name = "remote"
        logger.info("Using remote embedding server at %s", self.endpoint)

    async def embed(
        self, texts: list[str], is_query: bool = False
    ) -> EmbeddingResponse:
        """Generate embeddings by calling the remote server.

        Args:
            texts: List of text strings to embed.
            is_query: Ignored for remote client (server handles encoding).

        Returns:
            EmbeddingResponse with texts, embeddings, and model info.
        """
        response = requests.post(
            self.endpoint,
            json={"texts": texts},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()

        return EmbeddingResponse(
            texts=texts,
            embeddings=data["embeddings"],
            model=data.get("model", "remote"),
        )
