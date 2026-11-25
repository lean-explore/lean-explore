"""Freeze the inputs of the benchmark.

Writes, for the files chosen by ``select_files``:

- ``targets.jsonl``: every declaration of those files in the published
  database, with its source, dependencies and published description. Both
  arms describe exactly these names.
- ``context.jsonl``: every dependency of a target, with its published
  description when there is one and the number of declarations in the
  database that depend on it.
- ``sources/``: the Lean files at the pinned Mathlib commit.
- ``manifest.json``: versions, counts and SHA-256 digests.

Usage:
    python -m benchmarks.informalizer.export_inputs --database lean_explore.db \
        --files benchmarks/informalizer/files.json --out inputs
"""

import argparse
import hashlib
import json
import logging
import urllib.request
from collections.abc import Iterable
from pathlib import Path

from benchmarks.informalizer.corpus import (
    DATA_VERSION,
    MATHLIB_COMMIT,
    MATHLIB_REPOSITORY,
    DeclarationRow,
    connect,
    dependency_frequencies,
    informalizations,
    rows_for_modules,
)

logger = logging.getLogger(__name__)

RAW_URL = "https://raw.githubusercontent.com/{repository}/{commit}/{path}"


def target_record(row: DeclarationRow) -> dict:
    """Return the JSON record of one target declaration."""
    location = row.location
    return {
        "name": row.name,
        "module": row.module,
        "path": location.path if location else None,
        "start_line": location.start_line if location else None,
        "end_line": location.end_line if location else None,
        "docstring": row.docstring,
        "source_text": row.source_text,
        "dependencies": row.dependencies,
        "published": row.informalization,
    }


def write_jsonl(path: Path, records: Iterable[dict]) -> int:
    """Write records as JSON Lines and return how many were written."""
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    return count


def fetch_source(path: str, destination: Path, checkout: Path | None) -> bytes:
    """Copy one Mathlib file at the pinned commit into ``destination``.

    Args:
        path: Path of the file in the Mathlib repository.
        destination: Directory that mirrors the repository layout.
        checkout: A local Mathlib checkout at ``MATHLIB_COMMIT`` to read
            from, or None to download the file from GitHub.

    Returns:
        The file contents.
    """
    if checkout is not None:
        content = (checkout / path).read_bytes()
    else:
        url = RAW_URL.format(
            repository=MATHLIB_REPOSITORY, commit=MATHLIB_COMMIT, path=path
        )
        with urllib.request.urlopen(url, timeout=60) as response:
            content = response.read()
    target = destination / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return content


def context_records(
    targets: list[DeclarationRow], descriptions: dict[str, str], frequencies: dict
) -> list[dict]:
    """Return one record per dependency of any target."""
    names = sorted({name for row in targets for name in row.dependencies})
    return [
        {
            "name": name,
            "description": descriptions.get(name),
            "frequency": frequencies.get(name, 0),
        }
        for name in names
    ]


def sha256_of(path: Path) -> str:
    """Return the SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _arguments() -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--files", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--mathlib-checkout",
        type=Path,
        help="local Mathlib checkout at the pinned commit; default: download",
    )
    return parser.parse_args()


def main() -> None:
    """Export targets, context, sources and the manifest."""
    arguments = _arguments()
    selection = json.loads(arguments.files.read_text(encoding="utf-8"))
    if selection["mathlib_commit"] != MATHLIB_COMMIT:
        raise SystemExit("files.json was selected for another Mathlib commit")
    arguments.out.mkdir(parents=True, exist_ok=True)
    connection = connect(arguments.database)
    modules = [item["module"] for item in selection["files"]]
    targets = list(rows_for_modules(connection, modules))
    dependencies = {name for row in targets for name in row.dependencies}
    descriptions = informalizations(connection, dependencies)
    frequencies = dependency_frequencies(connection)
    target_count = write_jsonl(
        arguments.out / "targets.jsonl", map(target_record, targets)
    )
    context_count = write_jsonl(
        arguments.out / "context.jsonl",
        context_records(targets, descriptions, frequencies),
    )
    sources = {}
    for item in selection["files"]:
        content = fetch_source(
            item["path"], arguments.out / "sources", arguments.mathlib_checkout
        )
        sources[item["path"]] = hashlib.sha256(content).hexdigest()
    manifest = {
        "data_version": DATA_VERSION,
        "database_sha256": sha256_of(arguments.database),
        "mathlib_repository": MATHLIB_REPOSITORY,
        "mathlib_commit": MATHLIB_COMMIT,
        "files_sha256": sha256_of(arguments.files),
        "targets": target_count,
        "context": context_count,
        "sources": sources,
    }
    (arguments.out / "manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n", encoding="utf-8"
    )
    logger.info("Exported %d targets and %d context names", target_count, context_count)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
