"""Measure how well the judge agrees with the human review.

The review sheet grades a prefix of the judge's sample, so every graded
description also has a judge verdict. A description counts as faithful for a
person when its ``faithful`` grade is 2, and for the judge when every part
matches and nothing is added. The report gives precision, recall and F1 of the
judge for "faithful" and for "has a major problem" (grade 0), Cohen's kappa,
and the number of graded descriptions, as ProofNetVerif does for its metrics.
Read the judge's scores only as far as this agreement supports them.

Usage:
    python -m benchmarks.informalizer.calibrate --review results/review \
        --judge results/judge --out results/calibration
"""

import argparse
import csv
import json
import logging
from pathlib import Path

from benchmarks.informalizer.inputs import read_jsonl

logger = logging.getLogger(__name__)


def human_grades(review: Path) -> dict[tuple[str, str], int]:
    """Faithfulness grades by (arm, name), read through the review key."""
    key = json.loads((review / "key.json").read_text(encoding="utf-8"))
    grades = {}
    with (review / "grades.csv").open(encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if row["faithful"].strip() == "":
                continue
            arm = key[row["name"]][row["letter"]]
            grades[(arm, row["name"])] = int(row["faithful"])
    return grades


def agreement(pairs: list[tuple[bool, bool]]) -> dict:
    """Precision, recall, F1 and Cohen's kappa of judge against person."""
    true_positive = sum(judge and person for person, judge in pairs)
    judged = sum(judge for _, judge in pairs)
    actual = sum(person for person, _ in pairs)
    count = len(pairs)
    precision = true_positive / judged if judged else 0.0
    recall = true_positive / actual if actual else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    observed = sum(person == judge for person, judge in pairs) / count
    chance = (judged * actual + (count - judged) * (count - actual)) / count**2
    kappa = (observed - chance) / (1 - chance) if chance < 1 else 1.0
    return {
        "described": count,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": observed,
        "kappa": kappa,
    }


def calibrate(grades: dict, verdicts: dict) -> dict:
    """Agreement on faithful and on major problems, over shared descriptions."""
    shared = sorted(set(grades) & set(verdicts))
    if not shared:
        return {"described": 0}
    faithful = [(grades[k] == 2, verdicts[k]["faithful"]) for k in shared]
    major = [(grades[k] == 0, verdicts[k]["majors"] > 0) for k in shared]
    return {"faithful": agreement(faithful), "major": agreement(major)}


def _arguments() -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--judge", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    """Compare the filled-in grades with the judge's verdicts."""
    arguments = _arguments()
    grades = human_grades(arguments.review)
    verdicts = {
        (result["arm"], result["name"]): result
        for result in read_jsonl(arguments.judge / "results.jsonl")
    }
    result = calibrate(grades, verdicts)
    arguments.out.mkdir(parents=True, exist_ok=True)
    (arguments.out / "calibration.json").write_text(json.dumps(result, indent=1) + "\n")
    logger.info("%s", json.dumps(result, indent=1))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
