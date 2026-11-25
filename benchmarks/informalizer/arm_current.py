"""Arm "current": one request per declaration, as production makes them.

Prompts come from production's own ``build_prompt`` and prompt template, with
the proof of a theorem left out by the source parser and the published
descriptions of each declaration's dependencies as context. The parser's
Python bindings are required: without them production would send whole
proofs, and the baseline would cost more than production does.

Usage:
    python -m benchmarks.informalizer.arm_current --inputs inputs \
        --out runs/current [--dry-run]
"""

import argparse
import asyncio
import importlib.util
import json
import logging
from pathlib import Path

from benchmarks.informalizer.client import (
    Journal,
    Request,
    add_model_arguments,
    model_settings,
    run_requests,
)
from benchmarks.informalizer.dry_run import add_dry_run_arguments, write_estimate
from benchmarks.informalizer.inputs import (
    load_targets,
    published_descriptions,
    write_jsonl,
)
from lean_explore.extract.informalize.generation import DeclarationData, build_prompt
from lean_explore.extract.informalize.pipeline import PROMPT_PATH

logger = logging.getLogger(__name__)


def require_parser() -> None:
    """Stop unless the source parser's Python bindings can be imported."""
    if importlib.util.find_spec("lean_source_parser") is None:
        raise SystemExit(
            "lean_source_parser is not installed; build it with maturin from "
            "src/lean_explore/parser/crates/lean-source-py (see the README)"
        )


def declaration_request(
    target: dict, index: int, descriptions: dict[str, str], kind: str
) -> Request:
    """Build the production prompt of one target.

    Args:
        target: A record of ``targets.jsonl``.
        index: Position of the target, used as its identifier.
        descriptions: Existing descriptions of dependencies, by name.
        kind: ``declaration`` for the current arm, ``fallback`` when the
            per-file arm falls back to it.

    Returns:
        The request, keyed by kind and name.
    """
    data = DeclarationData(
        id=index,
        name=target["name"],
        source_text=target["source_text"],
        docstring=target["docstring"],
        dependencies=json.dumps(target["dependencies"]),
        informalization=None,
    )
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return Request(
        key=f"{kind}:{target['name']}",
        kind=kind,
        module=target["module"],
        prompt=build_prompt(data, template, descriptions),
    )


def current_requests(inputs: Path, limit: int | None) -> list[Request]:
    """Build one request per target."""
    targets = load_targets(inputs)[:limit]
    descriptions = published_descriptions(inputs)
    return [
        declaration_request(target, index, descriptions, "declaration")
        for index, target in enumerate(targets)
    ]


def descriptions_from(records: dict[str, dict], kind: str) -> dict[str, str]:
    """Return the stripped answers of the records of one kind, by name."""
    prefix = f"{kind}:"
    return {
        key.removeprefix(prefix): record["content"].strip()
        for key, record in records.items()
        if key.startswith(prefix) and record.get("content")
    }


def _arguments() -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    add_model_arguments(parser)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--limit", type=int, default=None)
    add_dry_run_arguments(parser)
    return parser.parse_args()


def main() -> None:
    """Build every prompt, then estimate or send them."""
    arguments = _arguments()
    require_parser()
    arguments.out.mkdir(parents=True, exist_ok=True)
    requests = current_requests(arguments.inputs, arguments.limit)
    write_jsonl(
        arguments.out / "prompts.jsonl",
        ({"key": r.key, "prompt": r.prompt} for r in requests),
    )
    if arguments.dry_run:
        expected = {
            f"declaration:{target['name']}": target["published"] or ""
            for target in load_targets(arguments.inputs)
        }
        write_estimate(arguments, requests, expected, arguments.out / "estimate.json")
        return
    settings = model_settings(arguments)
    journal = Journal(arguments.out / "journal.jsonl")
    records = asyncio.run(
        run_requests(requests, settings, journal, arguments.concurrency)
    )
    descriptions = descriptions_from(records, "declaration")
    write_jsonl(
        arguments.out / "descriptions.jsonl",
        (
            {"name": name, "description": text, "route": "model"}
            for name, text in descriptions.items()
        ),
    )
    logger.info("%d of %d declarations described", len(descriptions), len(requests))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
