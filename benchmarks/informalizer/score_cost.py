"""Compare what each run cost, from its journal.

Every journal line is one API call, retries and failures included, with the
token counts and the dollar cost that OpenRouter reported for it. Costs are
summed per run, divided by the number of target declarations, and weighted
back to the whole of Mathlib by stratum: each stratum holds a fifth of
Mathlib's declarations, and its cost per declaration is measured on the
benchmark files drawn from it.

Usage:
    python -m benchmarks.informalizer.score_cost --inputs inputs \
        --files benchmarks/informalizer/files.json \
        --run deepseek/current=runs/deepseek/current \
        --run deepseek/per-file=runs/deepseek/per-file --out results/cost
"""

import argparse
import json
import logging
from collections import Counter, defaultdict
from pathlib import Path

from benchmarks.informalizer.inputs import load_targets, read_jsonl

logger = logging.getLogger(__name__)

TOKEN_FIELDS = (
    "prompt_tokens",
    "cached_tokens",
    "completion_tokens",
    "reasoning_tokens",
)


def journal_totals(records: list[dict], stratum_of: dict[str, int]) -> dict:
    """Sum calls, tokens and cost of one journal, overall and per stratum."""
    totals: dict = {field: 0 for field in TOKEN_FIELDS}
    totals.update(calls=0, failed=0, cost=0.0, calls_without_cost=0)
    by_kind: Counter[str] = Counter()
    cost_by_stratum: defaultdict[int, float] = defaultdict(float)
    for record in records:
        totals["calls"] += 1
        by_kind[record["kind"]] += 1
        if record.get("content") is None:
            totals["failed"] += 1
        for field in TOKEN_FIELDS:
            totals[field] += record.get(field) or 0
        if record.get("cost") is None:
            totals["calls_without_cost"] += 1
        cost = record.get("cost") or 0.0
        totals["cost"] += cost
        cost_by_stratum[stratum_of.get(record["module"], -1)] += cost
    totals["calls_by_kind"] = dict(by_kind)
    totals["cost_by_stratum"] = dict(sorted(cost_by_stratum.items()))
    starts = [record["started"] for record in records]
    ends = [record["started"] + record["elapsed_seconds"] for record in records]
    totals["wall_seconds"] = max(ends) - min(starts) if records else 0.0
    return totals


def mathlib_estimate(
    cost_by_stratum: dict[int, float],
    targets_by_stratum: Counter[int],
    strata: list[dict],
) -> float:
    """Weight the cost per declaration of each stratum by its Mathlib size."""
    return sum(
        cost_by_stratum.get(stratum["index"], 0.0)
        / max(targets_by_stratum[stratum["index"]], 1)
        * stratum["declarations"]
        for stratum in strata
    )


def coverage(directory: Path, names: set[str]) -> dict:
    """Count the targets a run described, by route."""
    path = directory / "descriptions.jsonl"
    records = list(read_jsonl(path)) if path.exists() else []
    described = [r for r in records if r["name"] in names and r["description"]]
    return {
        "described": len(described),
        "targets": len(names),
        "by_route": dict(Counter(record["route"] for record in described)),
    }


def score_run(directory: Path, inputs: Path, selection: dict) -> dict:
    """Totals, per-declaration cost and Mathlib estimate of one run."""
    stratum_of = {item["module"]: item["stratum"] for item in selection["files"]}
    targets = load_targets(inputs)
    targets_by_stratum = Counter(stratum_of[t["module"]] for t in targets)
    records = list(read_jsonl(directory / "journal.jsonl"))
    totals = journal_totals(records, stratum_of)
    totals["cost_per_1000_declarations"] = 1000 * totals["cost"] / len(targets)
    totals["mathlib_estimate"] = mathlib_estimate(
        totals["cost_by_stratum"], targets_by_stratum, selection["strata"]
    )
    totals["coverage"] = coverage(directory, {t["name"] for t in targets})
    return totals


def markdown(results: dict[str, dict]) -> str:
    """Render the runs as one Markdown table."""
    lines = [
        "| Run | Calls | Failed | Input | Cached | Output | Reasoning | Cost | "
        "$ per 1k declarations | All of Mathlib | Described |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, run in results.items():
        cover = run["coverage"]
        lines.append(
            f"| {label} | {run['calls']:,} | {run['failed']:,} | "
            f"{run['prompt_tokens']:,} | {run['cached_tokens']:,} | "
            f"{run['completion_tokens']:,} | {run['reasoning_tokens']:,} | "
            f"${run['cost']:.2f} | ${run['cost_per_1000_declarations']:.3f} | "
            f"${run['mathlib_estimate']:.0f} | "
            f"{cover['described']:,} / {cover['targets']:,} |"
        )
    return "\n".join(lines) + "\n"


def _arguments() -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--files", type=Path, required=True)
    parser.add_argument("--run", action="append", required=True, help="label=directory")
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    """Score every run and write JSON and Markdown."""
    arguments = _arguments()
    selection = json.loads(arguments.files.read_text(encoding="utf-8"))
    results = {}
    for spec in arguments.run:
        label, directory = spec.split("=", 1)
        results[label] = score_run(Path(directory), arguments.inputs, selection)
    arguments.out.mkdir(parents=True, exist_ok=True)
    (arguments.out / "cost.json").write_text(json.dumps(results, indent=1) + "\n")
    (arguments.out / "cost.md").write_text(markdown(results), encoding="utf-8")
    logger.info("\n%s", markdown(results))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
