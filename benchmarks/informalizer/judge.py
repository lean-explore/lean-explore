"""Grade how faithfully each run's descriptions state their declarations.

The method follows LeanScorer (Mathesis, arXiv 2506.07047), which reached an
F1 of 0.92 against human labels where a single holistic LLM judgment reached
0.85: the formal statement is the reference, broken into the parts a faithful
text must state, and each part is rated "match", "minor" or "major" instead of
the whole text receiving one verdict. Here the parts are written once per
declaration and shared by every run, so all runs are graded against the same
checklist. Claims the declaration does not make are rated as one more part.

Each rating is repeated ``--votes`` times and the majority kept, ties going to
the more severe rating; majority voting lifted judge accuracy on exact matches
from 73% to 93% in "Formal-Informal Mathematical Alignment through and beyond
Human Annotations" (CIKM 2026, arXiv 2610.31011).
The judge comes from a model family that wrote none of the descriptions.

Scores use LeanScorer's levels: a declaration scores 0 with any major problem,
otherwise 1 halved once per minor one. "Faithful" means every part matches
and nothing is added.

Usage:
    python -m benchmarks.informalizer.judge --inputs inputs \
        --files benchmarks/informalizer/files.json \
        --statements results/statements.jsonl \
        --run deepseek/current=runs/deepseek/current \
        --run deepseek/per-file=runs/deepseek/per-file --out results/judge
"""

import argparse
import asyncio
import json
import logging
from collections import Counter
from pathlib import Path

import numpy as np

from benchmarks.informalizer.client import (
    Journal,
    ModelSettings,
    Request,
    parse_temperature,
    run_requests,
)
from benchmarks.informalizer.comparisons import bootstrap_interval, default_pairs
from benchmarks.informalizer.dry_run import add_dry_run_arguments, write_estimate
from benchmarks.informalizer.inputs import load_descriptions, load_targets, write_jsonl
from benchmarks.informalizer.sample import load_order, reference_text

logger = logging.getLogger(__name__)

DEFAULT_JUDGE = "anthropic/claude-sonnet-5.5"
"""A family that wrote none of the compared descriptions."""

PROMPTS = Path(__file__).parent / "prompts"
SEVERITY = {"match": 0, "none": 0, "minor": 1, "major": 2}


def extract_object(content: str | None) -> dict | None:
    """The JSON object in an answer, or None."""
    if not content or "{" not in content:
        return None
    try:
        value = json.loads(content[content.find("{") : content.rfind("}") + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def note_line(reference: dict) -> str:
    """Explain a generated or nested name, which the source does not write."""
    if not reference.get("note"):
        return ""
    return (
        f"The name is not written in the source: it is {reference['note']}. "
        "Judge it as that declaration, which the shown source determines."
    )


def decompose_request(reference: dict) -> Request:
    """Ask for the parts of one declaration."""
    template = (PROMPTS / "judge_decompose.txt").read_text(encoding="utf-8")
    prompt = template.format(
        name=reference["name"],
        reference=reference_text(reference),
        note_line=note_line(reference),
    )
    return Request(
        key=f"decompose:{reference['name']}",
        kind="decompose",
        module=reference["module"],
        prompt=prompt,
    )


def parts_of(content: str | None) -> list[dict] | None:
    """The parts listed in a decomposition answer."""
    value = extract_object(content)
    parts = value.get("parts") if value else None
    if not isinstance(parts, list) or not parts:
        return None
    return [part for part in parts if isinstance(part, dict) and "id" in part]


def rate_request(
    reference: dict, parts: list[dict], arm: str, description: str, vote: int
) -> Request:
    """Ask for the ratings of one description against the parts."""
    template = (PROMPTS / "judge_rate.txt").read_text(encoding="utf-8")
    listed = "\n".join(
        f"- {part['id']} ({part.get('role', 'part')}): {part.get('text', '')}"
        for part in parts
    )
    prompt = template.format(
        name=reference["name"],
        reference=reference_text(reference),
        note_line=note_line(reference),
        parts=listed,
        description=description,
    )
    return Request(
        key=f"rate:{arm}:{reference['name']}:{vote}",
        kind="rate",
        module=reference["module"],
        prompt=prompt,
    )


def ratings_of(content: str | None, parts: list[dict]) -> dict | None:
    """Ratings by part identifier plus ``unsupported``, or None if unusable."""
    value = extract_object(content)
    if value is None or not isinstance(value.get("ratings"), list):
        return None
    given = {
        entry.get("id"): entry.get("rating")
        for entry in value["ratings"]
        if isinstance(entry, dict)
    }
    ratings = {part["id"]: given.get(part["id"]) for part in parts}
    if any(rating not in SEVERITY for rating in ratings.values()):
        return None
    unsupported = value.get("unsupported", "none")
    ratings["unsupported"] = unsupported if unsupported in SEVERITY else "major"
    return ratings


def majority(levels: list[str]) -> str:
    """The most frequent rating, the more severe one on a tie."""
    counts = Counter(levels)
    return max(counts, key=lambda level: (counts[level], SEVERITY[level]))


def aggregate(votes: list[dict]) -> dict:
    """Combine the votes on one description into its verdict and score."""
    final = {key: majority([vote[key] for vote in votes]) for key in votes[0]}
    severities = [SEVERITY[level] for level in final.values()]
    majors = severities.count(2)
    minors = severities.count(1)
    return {
        "ratings": final,
        "majors": majors,
        "minors": minors,
        "faithful": majors == 0 and minors == 0,
        "score": 0.0 if majors else 0.5**minors,
        "votes": len(votes),
    }


def load_arms(arguments: argparse.Namespace) -> dict[str, dict[str, str]]:
    """Published descriptions and those of every run, by arm label.

    In a dry run the runs do not exist yet, so every run is given the
    published descriptions, which have about the same length.
    """
    published = {
        target["name"]: target["published"]
        for target in load_targets(arguments.inputs)
        if target["published"]
    }
    arms = {"published": published}
    for spec in arguments.run:
        label, directory = spec.split("=", 1)
        path = Path(directory) / "descriptions.jsonl"
        arms[label] = published if arguments.dry_run else load_descriptions(path)
    return arms


def rating_requests(
    sample: list[dict],
    parts: dict[str, list[dict]],
    arms: dict[str, dict[str, str]],
    votes: int,
) -> list[Request]:
    """Every rating request of the run."""
    requests = []
    for reference in sample:
        name = reference["name"]
        for arm, descriptions in arms.items():
            if name in parts and descriptions.get(name):
                requests.extend(
                    rate_request(reference, parts[name], arm, descriptions[name], vote)
                    for vote in range(votes)
                )
    return requests


def verdicts(
    records: dict[str, dict],
    sample: list[dict],
    parts: dict[str, list[dict]],
    arms: dict[str, dict[str, str]],
    votes: int,
) -> list[dict]:
    """One verdict per arm and declaration that has usable votes."""
    results = []
    for reference in sample:
        name = reference["name"]
        for arm in arms:
            usable = [
                ratings_of(
                    records.get(f"rate:{arm}:{name}:{vote}", {}).get("content"),
                    parts.get(name, []),
                )
                for vote in range(votes)
            ]
            usable = [vote for vote in usable if vote is not None]
            if usable:
                verdict = aggregate(usable)
                results.append(
                    {"arm": arm, "name": name, "kind": reference["kind"]} | verdict
                )
    return results


def summarize(results: list[dict], pairs: list[str]) -> dict:
    """Mean score, faithful and major rates per arm, and paired differences."""
    by_arm: dict[str, dict[str, dict]] = {}
    for result in results:
        by_arm.setdefault(result["arm"], {})[result["name"]] = result
    summary: dict = {"arms": {}, "comparisons": {}}
    for arm, entries in by_arm.items():
        values = list(entries.values())
        summary["arms"][arm] = {
            "judged": len(values),
            "score": float(np.mean([v["score"] for v in values])),
            "faithful": float(np.mean([v["faithful"] for v in values])),
            "major": float(np.mean([v["majors"] > 0 for v in values])),
        }
    for pair in pairs:
        first, second = pair.split(":")
        shared = sorted(set(by_arm.get(first, {})) & set(by_arm.get(second, {})))
        if not shared:
            continue
        differences = np.array(
            [by_arm[first][n]["score"] - by_arm[second][n]["score"] for n in shared]
        )
        summary["comparisons"][pair] = {
            "declarations": len(shared),
            "score_difference": float(differences.mean()),
            "interval": list(bootstrap_interval(differences)),
        }
    return summary


def markdown(summary: dict) -> str:
    """Render the summary as Markdown."""
    lines = [
        "| Arm | Judged | Score | Faithful | Any major problem |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for arm, values in summary["arms"].items():
        lines.append(
            f"| {arm} | {values['judged']} | {values['score']:.3f} | "
            f"{values['faithful']:.3f} | {values['major']:.3f} |"
        )
    lines += ["", "| Comparison | Declarations | Score difference | 95% interval |"]
    lines.append("| --- | ---: | ---: | --- |")
    for pair, values in summary["comparisons"].items():
        low, high = values["interval"]
        lines.append(
            f"| {pair} | {values['declarations']} | "
            f"{values['score_difference']:+.3f} | [{low:+.3f}, {high:+.3f}] |"
        )
    return "\n".join(lines) + "\n"


def _arguments() -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--files", type=Path, required=True)
    parser.add_argument("--statements", type=Path, required=True)
    parser.add_argument("--run", action="append", default=[], help="label=directory")
    parser.add_argument("--compare", action="append", default=[], help="label:label")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--size", type=int, default=500)
    parser.add_argument("--votes", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE)
    parser.add_argument("--judge-temperature", type=parse_temperature, default=None)
    parser.add_argument("--concurrency", type=int, default=20)
    add_dry_run_arguments(parser)
    return parser.parse_args()


EXPECTED_RATING = json.dumps(
    {
        "ratings": [
            {"id": "P1", "rating": "match"},
            {"id": "P2", "rating": "match"},
            {"id": "P3", "rating": "minor"},
        ],
        "unsupported": "none",
        "comment": "One sentence about the most serious problem.",
    }
)
"""A typical rating answer, for dry-run estimates."""


def dry_run(arguments: argparse.Namespace, sample: list[dict], arms: dict) -> None:
    """Estimate both stages, with each statement standing in for its parts."""
    stand_in = {r["name"]: [{"id": "P1", "text": r["statement"]}] for r in sample}
    requests = [decompose_request(reference) for reference in sample]
    requests += rating_requests(sample, stand_in, arms, arguments.votes)
    expected = {
        request.key: (
            stand_in[request.key.split(":", 1)[1]][0]["text"]
            if request.kind == "decompose"
            else EXPECTED_RATING
        )
        for request in requests
    }
    write_estimate(arguments, requests, expected, arguments.out / "estimate.json")


def decompose_all(
    sample: list[dict], settings: ModelSettings, journal: Journal, concurrency: int
) -> dict[str, list[dict]]:
    """Decompose every sampled declaration once; return the usable parts."""
    records = asyncio.run(
        run_requests(
            [decompose_request(reference) for reference in sample],
            settings,
            journal,
            concurrency,
        )
    )
    parts = {}
    for key, record in records.items():
        found = parts_of(record.get("content"))
        if found is not None:
            parts[key.removeprefix("decompose:")] = found
    return parts


def main() -> None:
    """Decompose, rate, aggregate and summarize."""
    arguments = _arguments()
    arguments.out.mkdir(parents=True, exist_ok=True)
    selection = json.loads(arguments.files.read_text(encoding="utf-8"))
    sample = load_order(arguments.statements, selection, arguments.seed)
    sample = sample[: arguments.size]
    arms = load_arms(arguments)
    if arguments.dry_run:
        dry_run(arguments, sample, arms)
        return
    settings = ModelSettings(
        model=arguments.judge_model, temperature=arguments.judge_temperature
    )
    journal = Journal(arguments.out / "journal.jsonl")
    parts = decompose_all(sample, settings, journal, arguments.concurrency)
    requests = rating_requests(sample, parts, arms, arguments.votes)
    records = asyncio.run(
        run_requests(requests, settings, journal, arguments.concurrency)
    )
    results = verdicts(records, sample, parts, arms, arguments.votes)
    write_jsonl(arguments.out / "results.jsonl", results)
    summary = summarize(results, arguments.compare or default_pairs(list(arms)))
    summary.update(decomposed=len(parts), sample=len(sample))
    (arguments.out / "judge.json").write_text(json.dumps(summary, indent=1) + "\n")
    (arguments.out / "judge.md").write_text(markdown(summary), encoding="utf-8")
    logger.info("\n%s", markdown(summary))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
