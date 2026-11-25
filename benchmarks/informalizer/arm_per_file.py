"""Arm "per-file": the Rust planner turns each file into a few requests.

Steps:

1. ``lean-informal plan`` reads the files and writes one request per run of
   declarations, the names it routes without the model (aliases, ``reassoc``
   and ``elementwise`` lemmas) and the names it cannot find.
2. Each request is sent with the prompt in ``prompts/batch.txt``. The names
   the planner cannot find are sent with production's per-declaration prompt.
3. ``lean-informal assemble`` checks every answer against the names its
   request listed. Names still without a description are sent once more with
   the per-declaration prompt, and the run is assembled again.

Every request, fallbacks included, is journaled and counted in the cost.

Usage:
    python -m benchmarks.informalizer.arm_per_file --inputs inputs \
        --out runs/per-file --planner path/to/lean-informal [--dry-run]
"""

import argparse
import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path

from benchmarks.informalizer.arm_current import (
    declaration_request,
    descriptions_from,
    require_parser,
)
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
    read_jsonl,
    write_jsonl,
)
from lean_explore.extract.informalize.generation import COMMON_DEPENDENCIES_PATH

logger = logging.getLogger(__name__)

BATCH_PROMPT_PATH = Path(__file__).parent / "prompts" / "batch.txt"
"""Template with {items}, {excerpt} and {context}."""


def render_items(items: list[dict]) -> str:
    """List the names a request asks for, with their notes."""
    lines = []
    for item in items:
        detail = item["kind"]
        if item.get("note"):
            detail += f"; {item['note']}"
        lines.append(f"- {item['name']} ({detail})")
    return "\n".join(lines)


def render_batch(batch: dict, template: str) -> str:
    """Fill the batch template for one planned request."""
    context = "\n".join(
        f"- {line['name']}: {line['description']}" for line in batch["context"]
    )
    return template.format(
        items=render_items(batch["items"]),
        excerpt=batch["excerpt"],
        context=context or "None.",
    )


def run_planner(arguments: argparse.Namespace, plan_directory: Path) -> dict:
    """Run ``lean-informal plan`` and return its counts."""
    inputs = arguments.inputs
    command = [
        str(arguments.planner),
        "plan",
        "--sources",
        str(inputs / "sources"),
        "--targets",
        str(inputs / "targets.jsonl"),
        "--context",
        str(inputs / "context.jsonl"),
        "--common",
        str(COMMON_DEPENDENCIES_PATH),
        "--out",
        str(plan_directory),
        "--batch-tokens",
        str(arguments.batch_tokens),
        "--batch-items",
        str(arguments.batch_items),
        "--context-tokens",
        str(arguments.context_tokens),
    ]
    subprocess.run(command, check=True)
    return json.loads((plan_directory / "plan.json").read_text(encoding="utf-8"))


def run_assembler(arguments: argparse.Namespace, out: Path) -> dict:
    """Run ``lean-informal assemble`` on the answers so far."""
    command = [
        str(arguments.planner),
        "assemble",
        "--plan",
        str(out / "plan"),
        "--responses",
        str(out / "responses.jsonl"),
        "--fallback",
        str(out / "fallback.jsonl"),
        "--context",
        str(arguments.inputs / "context.jsonl"),
        "--out",
        str(out / "assembly"),
    ]
    subprocess.run(command, check=True)
    return json.loads((out / "assembly" / "assembly.json").read_text(encoding="utf-8"))


def batch_requests(plan_directory: Path) -> list[Request]:
    """Build one request per planned batch."""
    template = BATCH_PROMPT_PATH.read_text(encoding="utf-8")
    return [
        Request(
            key=f"batch:{batch['id']}",
            kind="batch",
            module=batch["module"],
            prompt=render_batch(batch, template),
        )
        for batch in read_jsonl(plan_directory / "batches.jsonl")
    ]


def fallback_requests(names: list[str], inputs: Path) -> list[Request]:
    """Build production's per-declaration request for each name."""
    wanted = set(names)
    descriptions = published_descriptions(inputs)
    return [
        declaration_request(target, index, descriptions, "fallback")
        for index, target in enumerate(load_targets(inputs))
        if target["name"] in wanted
    ]


def expected_answers(plan_directory: Path, inputs: Path) -> dict[str, str]:
    """Published descriptions in the shape each request asks for."""
    published = {t["name"]: t["published"] or "" for t in load_targets(inputs)}
    expected = {f"fallback:{name}": text for name, text in published.items()}
    for batch in read_jsonl(plan_directory / "batches.jsonl"):
        expected[f"batch:{batch['id']}"] = "\n\n".join(
            f"### {item['name']}\n{published[item['name']]}" for item in batch["items"]
        )
    return expected


def write_answers(records: dict[str, dict], out: Path) -> None:
    """Write the batch answers and the fallback descriptions for the assembler."""
    write_jsonl(
        out / "responses.jsonl",
        (
            {"batch": key.removeprefix("batch:"), "content": record["content"]}
            for key, record in records.items()
            if key.startswith("batch:") and record.get("content") is not None
        ),
    )
    write_jsonl(
        out / "fallback.jsonl",
        (
            {"name": name, "description": text}
            for name, text in descriptions_from(records, "fallback").items()
        ),
    )


async def send_all(
    arguments: argparse.Namespace, requests: list[Request], out: Path
) -> dict:
    """Send the planned requests, assemble, retry what is missing, assemble again."""
    settings = model_settings(arguments)
    journal = Journal(out / "journal.jsonl")
    records = await run_requests(requests, settings, journal, arguments.concurrency)
    write_answers(records, out)
    first = run_assembler(arguments, out)
    missing = [
        entry["name"] for entry in read_jsonl(out / "assembly" / "missing.jsonl")
    ]
    retried = fallback_requests(missing, arguments.inputs)
    if retried:
        logger.info("Sending %d missing names one at a time", len(retried))
        records.update(
            await run_requests(retried, settings, journal, arguments.concurrency)
        )
        write_answers(records, out)
    final = run_assembler(arguments, out)
    return {"first_assembly": first, "retried": len(retried), "final_assembly": final}


def _arguments() -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--planner", type=Path, required=True, help="the lean-informal binary"
    )
    add_model_arguments(parser)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--batch-tokens", type=int, default=3000)
    parser.add_argument("--batch-items", type=int, default=40)
    parser.add_argument("--context-tokens", type=int, default=1500)
    add_dry_run_arguments(parser)
    return parser.parse_args()


def main() -> None:
    """Plan, then estimate or send, and write the descriptions."""
    arguments = _arguments()
    require_parser()
    out = arguments.out
    out.mkdir(parents=True, exist_ok=True)
    plan = run_planner(arguments, out / "plan")
    unmatched = [
        entry["name"] for entry in read_jsonl(out / "plan" / "unmatched.jsonl")
    ]
    requests = batch_requests(out / "plan") + fallback_requests(
        unmatched, arguments.inputs
    )
    write_jsonl(
        out / "prompts.jsonl",
        ({"key": r.key, "prompt": r.prompt} for r in requests),
    )
    if arguments.dry_run:
        expected = expected_answers(out / "plan", arguments.inputs)
        estimate = write_estimate(arguments, requests, expected, out / "estimate.json")
        estimate["plan"] = plan
        (out / "estimate.json").write_text(json.dumps(estimate, indent=1) + "\n")
        return
    summary = asyncio.run(send_all(arguments, requests, out))
    summary["plan"] = plan
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    shutil.copyfile(out / "assembly" / "descriptions.jsonl", out / "descriptions.jsonl")
    logger.info("Assembled: %s", summary["final_assembly"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
