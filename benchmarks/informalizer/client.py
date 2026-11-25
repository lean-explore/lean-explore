"""Send benchmark requests to the model and journal every answer.

Requests go through production's ``OpenRouterClient`` with the same model and
temperature as the informalization step. Each answer is appended to a JSON
Lines journal together with the token counts and cost OpenRouter reports, so
that an interrupted run resumes where it stopped and the cost comparison reads
measured numbers only.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from openai import NOT_GIVEN

from lean_explore.util import OpenRouterClient

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "google/gemini-3-flash-preview"
"""The informalization model of the extraction pipeline."""

DEFAULT_TEMPERATURE = 0.3
"""The informalization temperature of the extraction pipeline."""


@dataclass(frozen=True)
class ModelSettings:
    """How every request of a run is sent."""

    model: str = DEFAULT_MODEL
    temperature: float | None = DEFAULT_TEMPERATURE
    """None sends no temperature, for models that do not accept one."""
    reasoning_effort: str | None = None
    """None sends no reasoning setting, as production does."""


def parse_temperature(value: str) -> float | None:
    """Read a ``--temperature`` value, where ``none`` means not sent."""
    return None if value.lower() == "none" else float(value)


def add_model_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the model options shared by both arms."""
    parser.add_argument("--model", default=DEFAULT_MODEL, help="OpenRouter model id")
    parser.add_argument(
        "--temperature",
        type=parse_temperature,
        default=DEFAULT_TEMPERATURE,
        help="sampling temperature, or none for models that take none",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        help="OpenRouter reasoning effort; default: the provider's own",
    )


def model_settings(arguments: argparse.Namespace) -> ModelSettings:
    """Return the model settings of the command line."""
    return ModelSettings(
        model=arguments.model,
        temperature=arguments.temperature,
        reasoning_effort=arguments.reasoning_effort,
    )


@dataclass(frozen=True)
class Request:
    """One prompt to send."""

    key: str
    """Unique within a run, such as ``declaration:Nat.cast_pos``."""
    kind: str
    """``declaration``, ``batch`` or ``fallback``."""
    module: str
    prompt: str
    json_output: bool = False


def prompt_digest(prompt: str) -> str:
    """Return the SHA-256 of a prompt."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


class Journal:
    """Append-only record of answered requests.

    A key whose last record has content counts as answered and is not sent
    again; a key whose last record is an error is retried.
    """

    def __init__(self, path: Path):
        """Open the journal at ``path``, reading any earlier records."""
        self.path = path
        self.records: dict[str, dict] = {}
        if path.exists():
            with path.open(encoding="utf-8") as lines:
                for line in lines:
                    if line.strip():
                        record = json.loads(line)
                        self.records[record["key"]] = record

    def answered(self, request: Request) -> dict | None:
        """Return the earlier answer to the same prompt, if any."""
        record = self.records.get(request.key)
        if (
            record is not None
            and record.get("content") is not None
            and record.get("prompt_sha256") == prompt_digest(request.prompt)
        ):
            return record
        return None

    def append(self, record: dict) -> None:
        """Add a record and write it through to disk."""
        self.records[record["key"]] = record
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def usage_fields(usage: object | None) -> dict:
    """Read token counts and cost from an OpenRouter usage object."""
    data = usage.model_dump() if hasattr(usage, "model_dump") else {}
    completion_details = data.get("completion_tokens_details") or {}
    prompt_details = data.get("prompt_tokens_details") or {}
    return {
        "prompt_tokens": data.get("prompt_tokens"),
        "completion_tokens": data.get("completion_tokens"),
        "reasoning_tokens": completion_details.get("reasoning_tokens"),
        "cached_tokens": prompt_details.get("cached_tokens"),
        "cost": data.get("cost"),
    }


def request_options(settings: ModelSettings, request: Request) -> dict:
    """Return the keyword arguments of one API call."""
    extra_body: dict = {"usage": {"include": True}}
    if settings.reasoning_effort is not None:
        extra_body["reasoning"] = {"effort": settings.reasoning_effort}
    options: dict = {"extra_body": extra_body}
    if request.json_output:
        options["response_format"] = {"type": "json_object"}
    return options


async def send(
    client: OpenRouterClient, settings: ModelSettings, request: Request
) -> dict:
    """Send one request and return its journal record."""
    record: dict = {
        "key": request.key,
        "kind": request.kind,
        "module": request.module,
        "model": settings.model,
        "prompt_sha256": prompt_digest(request.prompt),
        "started": time.time(),
    }
    started = time.perf_counter()
    try:
        response = await client.generate(
            model=settings.model,
            messages=[{"role": "user", "content": request.prompt}],
            temperature=(
                NOT_GIVEN if settings.temperature is None else settings.temperature
            ),
            **request_options(settings, request),
        )
        choice = response.choices[0] if response.choices else None
        record["content"] = choice.message.content if choice else None
        record["finish_reason"] = choice.finish_reason if choice else None
        record["served_model"] = response.model
        record.update(usage_fields(response.usage))
    except Exception as error:  # Recorded and retried on the next run.
        record["content"] = None
        record["error"] = repr(error)
    record["elapsed_seconds"] = time.perf_counter() - started
    return record


async def run_requests(
    requests: list[Request],
    settings: ModelSettings,
    journal: Journal,
    concurrency: int,
) -> dict[str, dict]:
    """Send every request that the journal has not answered.

    Args:
        requests: Prompts to send.
        settings: Model and sampling settings.
        journal: Journal to resume from and append to.
        concurrency: Most requests in flight at once.

    Returns:
        The journal record of every request, by key.
    """
    client = OpenRouterClient()
    semaphore = asyncio.Semaphore(concurrency)
    done = 0

    async def one(request: Request) -> dict:
        nonlocal done
        earlier = journal.answered(request)
        if earlier is not None:
            return earlier
        async with semaphore:
            record = await send(client, settings, request)
        journal.append(record)
        done += 1
        if done % 100 == 0:
            logger.info("%d requests answered", done)
        return record

    records = await asyncio.gather(*(one(request) for request in requests))
    return {record["key"]: record for record in records}
