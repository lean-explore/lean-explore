"""Estimate what the whole benchmark costs before running it.

Every stage's dry run counts the tokens of its exact prompts and estimates its
answers. This script prices those counts with OpenRouter's list prices of
25 November 2026 for each model.

Reasoning tokens are billed as output and cannot be known before a run, so
generation is priced with reasoning equal to 0, 1 and 3 times the visible
output. Prompt caching is shown separately: DeepSeek caches repeated prompt
prefixes automatically, which bills the instructions every request repeats at
the cache price; OpenAI caches only prefixes of at least 1,024 tokens, which
these prompts do not reach.

Usage:
    python -m benchmarks.informalizer.estimate_cost \
        --generation current=dry/current/estimate.json \
        --generation per-file=dry/per-file/estimate.json \
        --stage judge=dry/judge/estimate.json \
        --stage round-trip=dry/round-trip/estimate.json --out dry/cost.md
"""

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from benchmarks.informalizer.arm_per_file import BATCH_PROMPT_PATH
from benchmarks.informalizer.dry_run import token_counter
from lean_explore.extract.informalize.pipeline import PROMPT_PATH

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens."""

    input: float
    output: float
    cache_read: float | None = None
    cache_minimum: int = 0
    """Shortest prompt prefix the provider caches."""


PRICES = {
    "deepseek/deepseek-v4.1-flash": Price(0.14, 0.42, 0.0042, 64),
    "openai/gpt-6.1-terra": Price(1.00, 5.00, 0.05, 1024),
    "anthropic/claude-sonnet-5.5": Price(2.00, 10.00),
    "google/gemini-3-flash-preview": Price(0.50, 3.00, 0.05, 1024),
}
"""OpenRouter list prices on 25 November 2026."""

REASONING_RATIOS = (0, 1, 3)


def shared_prefix_tokens(tokenizer: str | None) -> dict[str, int]:
    """Tokens of the instructions every request of an arm starts with."""
    count = token_counter(tokenizer)
    current = PROMPT_PATH.read_text(encoding="utf-8").split("{name}")[0]
    batch = BATCH_PROMPT_PATH.read_text(encoding="utf-8").split("{items}")[0]
    return {"current": count(current), "per-file": count(batch)}


def generation_cost(
    estimate: dict, price: Price, ratio: int, prefix: int, cached: bool
) -> float:
    """Dollars for one generation run."""
    overall = estimate["overall"]
    cached_tokens = 0
    if cached and price.cache_read is not None and prefix >= price.cache_minimum:
        cached_tokens = overall["requests"] * prefix
    fresh = overall["input_tokens"] - cached_tokens
    output = overall["output_tokens"] * (1 + ratio)
    cache_cost = cached_tokens * (price.cache_read or 0.0)
    return (fresh * price.input + cache_cost + output * price.output) / 1_000_000


def stage_cost(estimate: dict, price: Price) -> float:
    """Dollars for an evaluation stage, with no reasoning."""
    overall = estimate["overall"]
    return (
        overall["input_tokens"] * price.input + overall["output_tokens"] * price.output
    ) / 1_000_000


def generation_rows(
    generations: dict[str, dict], models: list[str], prefixes: dict[str, int]
) -> tuple[list[str], dict[int, float]]:
    """Markdown rows of every model and arm, and totals per reasoning ratio."""
    rows = []
    totals = dict.fromkeys(REASONING_RATIOS, 0.0)
    for model in models:
        price = PRICES[model]
        for arm, estimate in generations.items():
            costs = [
                generation_cost(estimate, price, ratio, prefixes[arm], False)
                for ratio in REASONING_RATIOS
            ]
            cached = generation_cost(estimate, price, 0, prefixes[arm], True)
            for ratio, cost in zip(REASONING_RATIOS, costs, strict=True):
                totals[ratio] += cost
            overall = estimate["overall"]
            rows.append(
                f"| {model} | {arm} | {overall['requests']:,} | "
                f"{overall['input_tokens']:,} | {overall['output_tokens']:,} | "
                + " | ".join(f"${cost:.2f}" for cost in costs)
                + f" | ${cached:.2f} |"
            )
    return rows, totals


def stage_rows(specs: list[str], judge_model: str) -> tuple[list[str], float]:
    """Markdown rows of the evaluation stages, and their total."""
    rows = []
    total = 0.0
    for label, path in (spec.split("=", 1) for spec in specs):
        estimate = json.loads(Path(path).read_text(encoding="utf-8"))
        cost = stage_cost(estimate, PRICES[judge_model])
        total += cost
        overall = estimate["overall"]
        rows.append(
            f"| {label} | {judge_model} | {overall['requests']:,} | "
            f"{overall['input_tokens']:,} | {overall['output_tokens']:,} | "
            f"${cost:.2f} |"
        )
    return rows, total


def report(arguments: argparse.Namespace) -> str:
    """The cost estimate as Markdown."""
    generations = {
        label: json.loads(Path(path).read_text(encoding="utf-8"))
        for label, path in (spec.split("=", 1) for spec in arguments.generation)
    }
    prefixes = shared_prefix_tokens(arguments.tokenizer)
    rows, totals = generation_rows(generations, arguments.models, prefixes)
    lines = [
        "| Model | Arm | Requests | Input tokens | Output tokens | No reasoning | "
        "Reasoning = output | Reasoning = 3 x output | No reasoning, prefix cached |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        *rows,
        "",
        "| Evaluation stage | Model | Requests | Input tokens | Output tokens | Cost |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    stage_lines, stages = stage_rows(arguments.stage, arguments.judge_model)
    lines += stage_lines
    lines += ["", "| Reasoning tokens | Generation, one run per model and arm |"]
    lines.append("| --- | ---: |")
    for ratio, total in totals.items():
        lines.append(f"| {ratio} x output | ${total:.2f} |")
    lower, upper = likely_range(totals, arguments.efforts, stages)
    runs = len(arguments.efforts) * len(arguments.models) * len(generations)
    lines += [
        "",
        "| Efforts | Generation runs | Evaluation | Likely total |",
        "| --- | ---: | ---: | ---: |",
        f"| {', '.join(arguments.efforts)} | {runs} | ${stages:.2f} | "
        f"${lower:.0f} to ${upper:.0f} |",
    ]
    return "\n".join(lines) + "\n"


EFFORT_BOUNDS = {"none": (0, 0), "minimal": (0, 1), "low": (0, 1)}
"""Assumed reasoning tokens, as multiples of the output, per effort; any other
effort, the provider default included, is assumed to fall between 1 and 3."""


def likely_range(
    totals: dict[int, float], efforts: list[str], stages: float
) -> tuple[float, float]:
    """Lower and upper total for the efforts run, evaluation included."""
    lower = sum(totals[EFFORT_BOUNDS.get(effort, (1, 3))[0]] for effort in efforts)
    upper = sum(totals[EFFORT_BOUNDS.get(effort, (1, 3))[1]] for effort in efforts)
    return lower + stages, upper + stages


def _arguments() -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--generation", action="append", required=True)
    parser.add_argument("--stage", action="append", default=[])
    parser.add_argument(
        "--models",
        nargs="+",
        default=["deepseek/deepseek-v4.1-flash", "openai/gpt-6.1-terra"],
    )
    parser.add_argument(
        "--efforts", nargs="+", default=["default"], help="reasoning efforts run"
    )
    parser.add_argument("--judge-model", default="anthropic/claude-sonnet-5.5")
    parser.add_argument("--tokenizer", default=None)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    """Write the estimate."""
    arguments = _arguments()
    text = report(arguments)
    arguments.out.write_text(text, encoding="utf-8")
    logger.info("\n%s", text)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
