"""Read the frozen inputs written by ``export_inputs`` and the run outputs."""

import json
from collections.abc import Iterable, Iterator
from pathlib import Path


def read_jsonl(path: Path) -> Iterator[dict]:
    """Yield the JSON value of every non-empty line."""
    with path.open(encoding="utf-8") as lines:
        for line in lines:
            if line.strip():
                yield json.loads(line)


def write_jsonl(path: Path, records: Iterable[dict]) -> int:
    """Write records as JSON Lines and return how many were written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    return count


def load_targets(inputs: Path) -> list[dict]:
    """Return the target declarations, in export order."""
    return list(read_jsonl(inputs / "targets.jsonl"))


def published_descriptions(inputs: Path) -> dict[str, str]:
    """Return the published description of every dependency that has one."""
    return {
        entry["name"]: entry["description"]
        for entry in read_jsonl(inputs / "context.jsonl")
        if entry["description"] is not None
    }


def load_descriptions(path: Path) -> dict[str, str]:
    """Return the descriptions of a run, by name."""
    return {record["name"]: record["description"] for record in read_jsonl(path)}
