"""Translate descriptions back to Lean and check them with BEq+.

This is the reference that statement-autoformalization benchmarks use
(ProofNet# and RLM25, from "Reliable Evaluation and Benchmarks for Statement
Autoformalization", EMNLP 2025): the original formal statement, compared with
a candidate by proofs that Lean checks (see ``lean_check``). A description
passes if
a model that sees only the description writes a theorem that Lean proves
equivalent, in both directions, to the Mathlib declaration it describes. The
original side is the declaration itself, as ``type_of% @Name``, so the
hypotheses its ``variable`` lines supply are part of the reference.

The sample is the written theorems and lemmas of the evaluation order. The
translator comes from a family that wrote none of the descriptions and is the
same for every run, so differences between runs come from the descriptions.
BEq+ misses about half of true equivalences, which lowers every run alike;
compare runs, do not read the rates as absolute accuracy.

Usage:
    python -m benchmarks.informalizer.round_trip --inputs inputs \
        --files benchmarks/informalizer/files.json \
        --statements results/statements.jsonl --mathlib path/to/mathlib4 \
        --run deepseek/current=runs/deepseek/current --out results/round-trip
"""

import argparse
import asyncio
import json
import logging
import multiprocessing
import re
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
from benchmarks.informalizer.inputs import read_jsonl, write_jsonl
from benchmarks.informalizer.judge import DEFAULT_JUDGE, load_arms
from benchmarks.informalizer.lean_check import LeanWorker, reference_path
from benchmarks.informalizer.sample import PROVED_KINDS, load_order

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "formalize.txt"
_LEAN_BLOCK = re.compile(r"```lean\s*\n(.*?)```", re.DOTALL)


def round_trip_sample(statements: Path, selection: dict, size: int, seed: int) -> list:
    """The first ``size`` written theorems and lemmas of the evaluation order."""
    order = load_order(statements, selection, seed)
    proved = [r for r in order if r["kind"] in PROVED_KINDS and not r.get("note")]
    return proved[:size]


def formalize_request(arm: str, name: str, module: str, description: str) -> Request:
    """Ask for the theorem a description states, without showing the name."""
    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(description=description)
    return Request(
        key=f"formalize:{arm}:{name}", kind="formalize", module=module, prompt=prompt
    )


def candidate_theorem(content: str | None) -> str | None:
    """The theorem in an answer, without imports or `open` lines."""
    if not content:
        return None
    blocks = _LEAN_BLOCK.findall(content)
    code = blocks[-1] if blocks else content
    lines = [
        line
        for line in code.splitlines()
        if not line.startswith(("import ", "open ", "set_option "))
    ]
    code = "\n".join(lines).strip()
    code = re.sub(r"^lemma\b", "theorem", code, flags=re.MULTILINE)
    return code if "theorem" in code else None


def reference_theorem(name: str) -> str:
    """The Mathlib declaration's own statement, read from the constant."""
    return f"theorem reference_statement : type_of% @{name} := by sorry"


_worker: LeanWorker | None = None


def _start_worker(mathlib: str, reference: str, timeout: int) -> None:
    """Start one Lean server with Mathlib imported."""
    global _worker
    _worker = LeanWorker(Path(mathlib), Path(reference), timeout)


def _check(task: dict) -> dict:
    """Typecheck one candidate and run BEq+ against its reference."""
    assert _worker is not None
    result = dict(task)
    reference = reference_theorem(task["name"])
    result["reference_typechecks"] = _worker.typechecks(reference)
    candidate = task["candidate"]
    result["typechecks"] = candidate is not None and _worker.typechecks(candidate)
    result["equivalent"] = bool(
        result["typechecks"]
        and result["reference_typechecks"]
        and _worker.equivalent(reference, candidate)
    )
    return result


def check_all(tasks: list[dict], arguments: argparse.Namespace) -> list[dict]:
    """Run the Lean checks in parallel, resuming from ``lean.jsonl``."""
    path = arguments.out / "lean.jsonl"
    done = {
        (r["arm"], r["name"]): r for r in (read_jsonl(path) if path.exists() else [])
    }
    pending = [t for t in tasks if (t["arm"], t["name"]) not in done]
    reference = reference_path(arguments.out / "reference")
    context = multiprocessing.get_context("spawn")
    with (
        context.Pool(
            arguments.workers,
            initializer=_start_worker,
            initargs=(str(arguments.mathlib), str(reference), arguments.timeout),
        ) as pool,
        path.open("a", encoding="utf-8") as stream,
    ):
        for result in pool.imap_unordered(_check, pending):
            done[(result["arm"], result["name"])] = result
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            stream.flush()
    return list(done.values())


def summarize(results: list[dict], sample_size: int, pairs: list[str]) -> dict:
    """Rates per arm over the whole sample, and paired differences."""
    by_arm: dict[str, dict[str, dict]] = {}
    for result in results:
        if result["reference_typechecks"]:
            by_arm.setdefault(result["arm"], {})[result["name"]] = result
    summary: dict = {"sample": sample_size, "arms": {}, "comparisons": {}}
    for arm, entries in by_arm.items():
        values = list(entries.values())
        summary["arms"][arm] = {
            "checked": len(values),
            "formalized": sum(v["candidate"] is not None for v in values),
            "typechecks": float(np.mean([v["typechecks"] for v in values])),
            "equivalent": float(np.mean([v["equivalent"] for v in values])),
        }
    for pair in pairs:
        first, second = pair.split(":")
        shared = sorted(set(by_arm.get(first, {})) & set(by_arm.get(second, {})))
        if not shared:
            continue
        differences = np.array(
            [
                float(by_arm[first][n]["equivalent"])
                - float(by_arm[second][n]["equivalent"])
                for n in shared
            ]
        )
        summary["comparisons"][pair] = {
            "declarations": len(shared),
            "equivalent_difference": float(differences.mean()),
            "interval": list(bootstrap_interval(differences)),
        }
    return summary


def markdown(summary: dict) -> str:
    """Render the summary as Markdown."""
    lines = [
        f"Theorems in the sample: {summary['sample']}",
        "",
        "| Arm | Checked | Formalized | Type-checks | BEq+ equivalent |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for arm, values in summary["arms"].items():
        lines.append(
            f"| {arm} | {values['checked']} | {values['formalized']} | "
            f"{values['typechecks']:.3f} | {values['equivalent']:.3f} |"
        )
    lines += ["", "| Comparison | Theorems | Difference | 95% interval |"]
    lines.append("| --- | ---: | ---: | --- |")
    for pair, values in summary["comparisons"].items():
        low, high = values["interval"]
        lines.append(
            f"| {pair} | {values['declarations']} | "
            f"{values['equivalent_difference']:+.3f} | [{low:+.3f}, {high:+.3f}] |"
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
    parser.add_argument("--size", type=int, default=300)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--formalizer-model", default=DEFAULT_JUDGE)
    parser.add_argument(
        "--formalizer-temperature", type=parse_temperature, default=None
    )
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--mathlib", type=Path, help="Mathlib checkout at the pin")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--skip-lean", action="store_true")
    add_dry_run_arguments(parser)
    return parser.parse_args()


def formalize_requests(sample: list[dict], arms: dict) -> list[Request]:
    """One back-translation request per arm and sampled theorem."""
    return [
        formalize_request(arm, r["name"], r["module"], descriptions[r["name"]])
        for r in sample
        for arm, descriptions in arms.items()
        if descriptions.get(r["name"])
    ]


def main() -> None:
    """Back-translate, then check every candidate in Lean."""
    arguments = _arguments()
    arguments.out.mkdir(parents=True, exist_ok=True)
    selection = json.loads(arguments.files.read_text(encoding="utf-8"))
    sample = round_trip_sample(
        arguments.statements, selection, arguments.size, arguments.seed
    )
    arms = load_arms(arguments)
    requests = formalize_requests(sample, arms)
    if arguments.dry_run:
        expected = {
            r.key: "theorem candidate : 0 = 0 := by sorry" * 4 for r in requests
        }
        write_estimate(arguments, requests, expected, arguments.out / "estimate.json")
        return
    settings = ModelSettings(
        model=arguments.formalizer_model, temperature=arguments.formalizer_temperature
    )
    journal = Journal(arguments.out / "journal.jsonl")
    records = asyncio.run(
        run_requests(requests, settings, journal, arguments.concurrency)
    )
    tasks = [
        {
            "arm": key.split(":")[1],
            "name": key.split(":", 2)[2],
            "candidate": candidate_theorem(record.get("content")),
        }
        for key, record in records.items()
    ]
    write_jsonl(arguments.out / "candidates.jsonl", tasks)
    if arguments.skip_lean or arguments.mathlib is None:
        logger.info("Wrote %d candidates; Lean checks skipped", len(tasks))
        return
    results = check_all(tasks, arguments)
    summary = summarize(
        results, len(sample), arguments.compare or default_pairs(list(arms))
    )
    (arguments.out / "round_trip.json").write_text(json.dumps(summary, indent=1) + "\n")
    (arguments.out / "round_trip.md").write_text(markdown(summary), encoding="utf-8")
    logger.info("\n%s", markdown(summary))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
