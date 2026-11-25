"""Estimate the tokens and cost of a run without calling the model.

Input tokens are counted on the exact prompts. Output tokens are estimated
from the published descriptions of the same declarations, in the format each
arm asks for. Counts use a Hugging Face tokenizer when one is named, otherwise
about 3.2 characters per token; the model's own tokenizer differs, so the
estimate is for checking a run before paying for it, not for the comparison.
"""

import argparse
import json
import math
from collections.abc import Callable
from pathlib import Path

from benchmarks.informalizer.client import Request

DEFAULT_INPUT_PRICE = 0.50
"""Dollars per million input tokens of the default model on OpenRouter."""

DEFAULT_OUTPUT_PRICE = 3.00
"""Dollars per million output tokens of the default model on OpenRouter."""


def add_dry_run_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the options shared by every arm's dry run."""
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tokenizer", default=None, help="Hugging Face model name")
    parser.add_argument("--input-price", type=float, default=DEFAULT_INPUT_PRICE)
    parser.add_argument("--output-price", type=float, default=DEFAULT_OUTPUT_PRICE)


def token_counter(tokenizer_name: str | None) -> Callable[[str], int]:
    """Return a function that counts the tokens of a text."""
    if tokenizer_name is None:
        return lambda text: math.ceil(len(text) / 3.2)
    from transformers import AutoTokenizer  # Only dry runs with a tokenizer need it.

    tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)
    return lambda text: len(tokenizer(text, add_special_tokens=False)["input_ids"])


def estimate(
    requests: list[Request],
    expected: dict[str, str],
    count: Callable[[str], int],
    prices: tuple[float, float],
) -> dict:
    """Count tokens and price a list of requests.

    Args:
        requests: The prompts of a run.
        expected: Expected answer text of each request, by key.
        count: Token counter.
        prices: Dollars per million input and output tokens.

    Returns:
        Totals by request kind and overall.
    """
    totals: dict[str, dict[str, float]] = {}
    for request in requests:
        kind = totals.setdefault(
            request.kind, {"requests": 0, "input_tokens": 0, "output_tokens": 0}
        )
        kind["requests"] += 1
        kind["input_tokens"] += count(request.prompt)
        kind["output_tokens"] += count(expected.get(request.key, ""))
    overall = {
        field: sum(kind[field] for kind in totals.values())
        for field in ("requests", "input_tokens", "output_tokens")
    }
    overall["dollars"] = (
        overall["input_tokens"] * prices[0] + overall["output_tokens"] * prices[1]
    ) / 1_000_000
    return {"by_kind": totals, "overall": overall}


def write_estimate(
    arguments: argparse.Namespace,
    requests: list[Request],
    expected: dict[str, str],
    path: Path,
) -> dict:
    """Estimate a run with the command-line settings and write it as JSON."""
    result = estimate(
        requests,
        expected,
        token_counter(arguments.tokenizer),
        (arguments.input_price, arguments.output_price),
    )
    result["tokenizer"] = arguments.tokenizer or "3.2 characters per token"
    result["prices_per_million"] = {
        "input": arguments.input_price,
        "output": arguments.output_price,
    }
    path.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return result
