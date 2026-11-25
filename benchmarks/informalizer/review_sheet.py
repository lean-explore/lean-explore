"""Write a blind side-by-side review of the runs' descriptions.

The sheet takes the first declarations of the evaluation order that every run
described, the same order the judge samples from, so every graded
description also has a judge verdict to calibrate against. Each declaration
shows its Lean statement in scope and each description under a letter, in an
order shuffled per declaration. ``key.json`` maps the letters back to runs,
and ``grades.csv`` has one row per description to fill in: faithful (0 wrong
or missing something that changes the meaning, 1 loose but readable, 2
equivalent to the statement) and clear (0 to 2).

Usage:
    python -m benchmarks.informalizer.review_sheet --inputs inputs \
        --files benchmarks/informalizer/files.json \
        --statements results/statements.jsonl \
        --run deepseek/current=runs/deepseek/current \
        --run deepseek/per-file=runs/deepseek/per-file --out results/review
"""

import argparse
import csv
import html
import json
import logging
import random
from pathlib import Path

from benchmarks.informalizer.inputs import load_descriptions, load_targets
from benchmarks.informalizer.sample import load_order, reference_text

logger = logging.getLogger(__name__)

SOURCE_LIMIT = 2000
"""Characters of Lean source shown per declaration."""

STYLE = """
body { font: 15px/1.5 system-ui, sans-serif; max-width: 980px; margin: 0 auto;
       padding: 16px; color: #1d1d1f; background: #ffffff; }
section { border-top: 1px solid #d0d0d0; padding: 12px 0; }
pre { background: #f4f4f4; padding: 8px; overflow-x: auto; white-space: pre-wrap; }
.description { margin: 8px 0; }
.letter { font-weight: 600; }
"""


def review_sample(
    order: list[dict], arms: dict[str, dict[str, str]], size: int
) -> list[dict]:
    """The first ``size`` references of the order that every arm described."""
    described = [r for r in order if all(r["name"] in arm for arm in arms.values())]
    return described[:size]


def section(reference: dict, entries: list[tuple[str, str]]) -> str:
    """One declaration with its lettered descriptions."""
    source = reference_text(reference)[-SOURCE_LIMIT:]
    note = f"<p>{html.escape(reference['note'])}</p>" if reference.get("note") else ""
    parts = [
        f"<section><h3>{html.escape(reference['name'])}</h3>",
        f"<p>{html.escape(reference['module'])}</p>{note}",
        f"<pre>{html.escape(source)}</pre>",
    ]
    for letter, text in entries:
        parts.append(
            f'<div class="description"><span class="letter">{letter}.</span> '
            f"{html.escape(text)}</div>"
        )
    parts.append("</section>")
    return "\n".join(parts)


def _arguments() -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--files", type=Path, required=True)
    parser.add_argument("--statements", type=Path, required=True)
    parser.add_argument("--run", action="append", required=True, help="label=directory")
    parser.add_argument("--size", type=int, default=60)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def arm_texts(arguments: argparse.Namespace) -> dict[str, dict[str, str]]:
    """Published descriptions and those of every run, by arm label."""
    arms = {
        "published": {
            target["name"]: target["published"]
            for target in load_targets(arguments.inputs)
            if target["published"]
        }
    }
    for spec in arguments.run:
        label, directory = spec.split("=", 1)
        arms[label] = load_descriptions(Path(directory) / "descriptions.jsonl")
    return arms


def write_sheet(out: Path, sections: list[str], key: dict, rows: list) -> None:
    """Write review.html, key.json and grades.csv."""
    out.mkdir(parents=True, exist_ok=True)
    page = (
        "<!doctype html><html><head><meta charset='utf-8'><title>Description review"
        f"</title><style>{STYLE}</style></head><body><h1>Description review</h1>"
        + "\n".join(sections)
        + "</body></html>"
    )
    (out / "review.html").write_text(page, encoding="utf-8")
    (out / "key.json").write_text(json.dumps(key, indent=1) + "\n")
    with (out / "grades.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["name", "letter", "faithful", "clear", "notes"])
        writer.writerows(rows)


def main() -> None:
    """Sample, shuffle and write the sheet."""
    arguments = _arguments()
    arms = arm_texts(arguments)
    selection = json.loads(arguments.files.read_text(encoding="utf-8"))
    order = load_order(arguments.statements, selection, arguments.seed)
    shuffler = random.Random(arguments.seed + 1)
    sections, key, rows = [], {}, []
    for reference in review_sample(order, arms, arguments.size):
        name = reference["name"]
        texts = [(label, descriptions[name]) for label, descriptions in arms.items()]
        shuffler.shuffle(texts)
        letters = "ABCDEFGHIJ"[: len(texts)]
        key[name] = {letter: label for letter, (label, _) in zip(letters, texts)}
        lettered = [(letter, text) for letter, (_, text) in zip(letters, texts)]
        sections.append(section(reference, lettered))
        rows += [[name, letter, "", "", ""] for letter in letters]
    write_sheet(arguments.out, sections, key, rows)
    logger.info("Wrote %d declarations to %s", len(sections), arguments.out)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
