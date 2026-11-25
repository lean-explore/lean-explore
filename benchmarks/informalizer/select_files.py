"""Choose the Mathlib files of the benchmark.

Files are split into strata by their number of declarations, so that small and
large files are both represented and cost per declaration can be weighted back
to the whole of Mathlib. Within a stratum, files that hold more answers of the
search benchmark come first, so that the search comparison has queries to
score; ties are broken by a seeded hash.

Usage:
    python -m benchmarks.informalizer.select_files --database lean_explore.db \
        --queries bench/queries --out benchmarks/informalizer/files.json
"""

import argparse
import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from benchmarks.informalizer.corpus import (
    DATA_VERSION,
    MATHLIB_COMMIT,
    connect,
    mathlib_modules,
    name_modules,
)
from benchmarks.informalizer.queries import BenchmarkQuery, load_queries, query_files

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SelectedFile:
    """A file of the benchmark."""

    module: str
    path: str
    declarations: int
    stratum: int
    covered_queries: int


@dataclass(frozen=True)
class Stratum:
    """Files of Mathlib whose declaration count lies in one band."""

    index: int
    minimum_declarations: int
    maximum_declarations: int
    files: int
    declarations: int


def stratify(counts: dict[str, int], strata: int) -> dict[str, int]:
    """Assign each module to a stratum by declaration count.

    Modules are sorted by count and cut where the running total of
    declarations crosses each multiple of ``total / strata``, so every stratum
    holds about the same share of Mathlib's declarations and stratum 0 holds
    the smallest files.

    Args:
        counts: Declaration count of each module.
        strata: Number of strata.

    Returns:
        Stratum index of each module.
    """
    ordered = sorted(counts, key=lambda module: (counts[module], module))
    total = sum(counts.values())
    assignment: dict[str, int] = {}
    running = 0
    for module in ordered:
        # The midpoint of the file decides, so a large file is not split.
        midpoint = running + counts[module] / 2
        assignment[module] = min(int(midpoint * strata / total), strata - 1)
        running += counts[module]
    return assignment


def coverage(
    queries: list[BenchmarkQuery], modules_by_name: dict[str, str]
) -> dict[str, set[str]]:
    """Map each module to the queries with a hit among its declarations."""
    covered: dict[str, set[str]] = defaultdict(set)
    for query in queries:
        for name in query.hits():
            module = modules_by_name.get(name)
            if module is not None:
                covered[module].add(query.identifier)
    return covered


def tie_breaker(seed: int, module: str) -> str:
    """Return a stable pseudo-random key for a module."""
    return hashlib.sha256(f"{seed}:{module}".encode()).hexdigest()


def select(
    modules: dict[str, tuple[str, int]],
    covered: dict[str, set[str]],
    files: int,
    strata: int,
    seed: int,
) -> tuple[list[SelectedFile], list[Stratum]]:
    """Pick ``files // strata`` files from each stratum.

    Args:
        modules: Path and declaration count of each Mathlib module.
        covered: Benchmark queries answered in each module.
        files: Number of files to pick.
        strata: Number of strata.
        seed: Seed of the tie-breaking hash.

    Returns:
        The selected files and a summary of every stratum.
    """
    counts = {module: count for module, (_, count) in modules.items()}
    stratum_of = stratify(counts, strata)
    members: dict[int, list[str]] = defaultdict(list)
    for module, index in stratum_of.items():
        members[index].append(module)
    selected: list[SelectedFile] = []
    summary: list[Stratum] = []
    for index in range(strata):
        group = members[index]
        ranked = sorted(
            group,
            key=lambda module: (
                -len(covered.get(module, ())),
                tie_breaker(seed, module),
            ),
        )
        for module in sorted(ranked[: files // strata]):
            selected.append(
                SelectedFile(
                    module=module,
                    path=modules[module][0],
                    declarations=counts[module],
                    stratum=index,
                    covered_queries=len(covered.get(module, ())),
                )
            )
        group_counts = [counts[module] for module in group]
        summary.append(
            Stratum(
                index=index,
                minimum_declarations=min(group_counts),
                maximum_declarations=max(group_counts),
                files=len(group),
                declarations=sum(group_counts),
            )
        )
    return selected, summary


def file_digest(path: Path) -> str:
    """Return the SHA-256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _arguments() -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--queries", type=Path, nargs="+", required=True)
    parser.add_argument("--files", type=int, default=100)
    parser.add_argument("--strata", type=int, default=5)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    """Select the files and write them with the selection settings."""
    arguments = _arguments()
    connection = connect(arguments.database)
    modules = mathlib_modules(connection)
    queries = load_queries(arguments.queries)
    covered = coverage(queries, name_modules(connection))
    selected, strata = select(
        modules, covered, arguments.files, arguments.strata, arguments.seed
    )
    answered = set().union(*(covered.get(item.module, set()) for item in selected))
    document = {
        "data_version": DATA_VERSION,
        "mathlib_commit": MATHLIB_COMMIT,
        "seed": arguments.seed,
        "query_files": [
            {"path": path.name, "sha256": file_digest(path)}
            for path in query_files(arguments.queries)
        ],
        "covered_queries": len(answered),
        "declarations": sum(item.declarations for item in selected),
        "strata": [asdict(stratum) for stratum in strata],
        "files": [asdict(item) for item in selected],
    }
    arguments.out.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    logger.info(
        "Selected %d files, %d declarations, %d covered queries",
        len(selected),
        document["declarations"],
        len(answered),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
