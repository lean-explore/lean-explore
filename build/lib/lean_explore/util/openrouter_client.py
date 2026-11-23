"""OpenRouter API wrapper using OpenAI SDK types.

Provides a client class that wraps OpenRouter's API and returns OpenAI SDK-compatible
types for easy integration.
"""

import os

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from tenacity import retry, stop_after_attempt, wait_exponential


class OpenRouterClient:
    """Client for interacting with OpenRouter API using OpenAI SDK types."""

    def __init__(self):
        """Initialize OpenRouter client.

        Reads API key from OPENROUTER_API_KEY environment variable.
        """
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY environment variable not set")

        self.client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def generate(
        self,
        model: str,
        messages: list[ChatCompletionMessageParam],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs,
    ) -> ChatCompletion:
        """Generate a chat completion using OpenRouter.

        Args:
            model: Model name (e.g., "anthropic/claude-3.5-sonnet")
            messages: List of message dicts with "role" and "content"
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            **kwargs: Additional parameters to pass to the API

        Returns:
            ChatCompletion object from OpenAI SDK
        """
        return await self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
