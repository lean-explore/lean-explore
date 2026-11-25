"""Load the queries of lean-explore-bench.

Version 1 of the benchmark stores one query per line with ``answers``, a list
of ``{name, grade}``, and a ``bucket``. The earlier build of 12 November 2026
stores ``judgments``, a map of name to grade, and a ``query_type``; it is read
too so that the harness can be tested without the published dataset.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

HIT_GRADE = 2
"""Answers of at least this grade count as hits."""

_EARLIER_BUCKETS = {
    "name": "Names and keywords",
    "names": "Names and keywords",
    "names+keywords": "Names and keywords",
    "keywords": "Names and keywords",
    "prose": "Natural language",
    "prose+names": "Natural language",
    "formal": "Formal",
    "goal": "Formal",
}
"""Bucket of each query form, for the earlier build."""


@dataclass(frozen=True)
class BenchmarkQuery:
    """One query with its graded answers."""

    identifier: str
    text: str
    bucket: str
    split: str
    answers: dict[str, int] = field(default_factory=dict)

    def hits(self) -> set[str]:
        """Return the answers that count as hits."""
        return {name for name, grade in self.answers.items() if grade >= HIT_GRADE}


def _from_record(record: dict) -> BenchmarkQuery:
    """Read one query in either format."""
    if "answers" in record:
        answers = {answer["name"]: int(answer["grade"]) for answer in record["answers"]}
        bucket = record["bucket"]
    else:
        answers = {name: int(grade) for name, grade in record["judgments"].items()}
        bucket = (
            "Real questions"
            if record.get("source") == "zulip"
            else _EARLIER_BUCKETS.get(record.get("query_type", ""), "Other")
        )
    return BenchmarkQuery(
        identifier=record["id"],
        text=record["query"],
        bucket=bucket,
        split=record.get("split", ""),
        answers=answers,
    )


def query_files(paths: Iterable[Path]) -> list[Path]:
    """Expand directories to the JSON Lines files they hold, sorted."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("*.jsonl")))
        else:
            files.append(path)
    return files


def load_queries(paths: Iterable[Path]) -> list[BenchmarkQuery]:
    """Load the queries of every given file or directory.

    Args:
        paths: JSON Lines files, or directories such as ``bench/queries``.

    Returns:
        The queries in file order, without duplicates by identifier.
    """
    queries: dict[str, BenchmarkQuery] = {}
    for path in query_files(paths):
        with path.open(encoding="utf-8") as lines:
            for line in lines:
                if line.strip():
                    query = _from_record(json.loads(line))
                    queries.setdefault(query.identifier, query)
    return list(queries.values())
