"""The order in which declarations enter the quality evaluations.

The judge, the human review and the round trip each take a prefix of one
ordering, so that every declaration a person grades is also judged, and the
judge can be calibrated on those grades. The ordering interleaves the strata
of ``files.json`` round-robin, each shuffled with a fixed seed: every prefix
gives each fifth of Mathlib's declarations an equal share, as the cost
weighting does.
"""

import random
from collections import defaultdict

from benchmarks.informalizer.inputs import read_jsonl

PROVED_KINDS = frozenset({"theorem", "lemma"})
"""Kinds whose statement is a proposition, as the round trip needs."""


def evaluation_order(
    references: list[dict], selection: dict, seed: int = 0
) -> list[dict]:
    """Order the references for evaluation.

    Args:
        references: Records written by ``lean-informal statements``.
        selection: The contents of ``files.json``.
        seed: Seed of the per-stratum shuffles.

    Returns:
        All references, strata interleaved, each stratum in seeded order.
    """
    stratum_of = {item["module"]: item["stratum"] for item in selection["files"]}
    by_stratum: dict[int, list[dict]] = defaultdict(list)
    for reference in sorted(references, key=lambda r: r["name"]):
        by_stratum[stratum_of[reference["module"]]].append(reference)
    generator = random.Random(seed)
    queues = []
    for stratum in sorted(by_stratum):
        members = by_stratum[stratum]
        generator.shuffle(members)
        queues.append(members)
    ordered: list[dict] = []
    while any(queues):
        for queue in queues:
            if queue:
                ordered.append(queue.pop(0))
    return ordered


def load_order(statements_path, selection: dict, seed: int = 0) -> list[dict]:
    """Read ``statements.jsonl`` and return it in evaluation order."""
    return evaluation_order(list(read_jsonl(statements_path)), selection, seed)


def reference_text(reference: dict) -> str:
    """The Lean source a description is judged against, as one block."""
    lines = list(reference["scope"]) + [reference["statement"]]
    return "\n".join(lines)
