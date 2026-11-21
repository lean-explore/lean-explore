"""Compare FLT main statements with Anthropic's independent source-extractor output.

Generate thm.jsonl with tools/docs-site/gen/extract.py from the pinned Anthropic
checkout. Pass that file and lean-source --text output from the same checkout.
This checks authored main statements, not elaborated or generated constants.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

HEAD = re.compile(r"\b(?:theorem|lemma)\s+([^\s{(\[:]+?)(?:\.\{[^}]*\})?(?=[\s{(\[:])")


def compare(reference: Path, parsed: Path) -> tuple[dict, list[dict]]:
    """Check every reference theorem, retaining missing and duplicate matches."""
    expected = {}
    for line in reference.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        match = HEAD.search(record["decl"])
        if match is None:
            raise ValueError(f"Cannot read reference name: {record['stem']}")
        written = match.group(1)
        name = (
            written.removeprefix("_root_.")
            if written.startswith("_root_.")
            else ".".join([*record["namespaces"], written])
        )
        file = f"Theorems/Thm_{record['stem']}.lean"
        if file in expected:
            raise ValueError(f"Duplicate reference file: {file}")
        expected[file] = (name, record)
    if not expected:
        raise ValueError("Empty reference")

    found = collections.defaultdict(list)
    with parsed.open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            file = record["file"]
            if (
                file in expected
                and record["name"] == expected[file][0]
                and not record["generated"]
            ):
                found[file].append(record)

    counts = collections.Counter(reference_theorems=len(expected))
    errors = []
    for file, (name, reference_record) in expected.items():
        records = found[file]
        if len(records) != 1:
            counts["missing_or_ambiguous"] += 1
            errors.append({"file": file, "name": name, "matches": len(records)})
            continue
        counts["exact_names"] += 1
        record = records[0]
        counts["exact_kinds"] += record["kind"] == reference_record["kw"]
        actual = record["signature_text"].rstrip()
        wanted = reference_record["decl"]
        if actual == wanted:
            counts["exact_signatures"] += 1
        else:
            errors.append(
                {
                    "file": file,
                    "name": name,
                    "expected_signature": wanted,
                    "actual_signature": actual,
                    "signature": record["signature"],
                    "body": record["body"],
                }
            )
        if record["kind"] != reference_record["kw"]:
            errors.append(
                {
                    "file": file,
                    "name": name,
                    "expected_kind": reference_record["kw"],
                    "actual_kind": record["kind"],
                }
            )
    return dict(counts), errors


def main() -> int:
    """Write all mismatches and fail if any reference theorem differs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("parsed", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    counts, errors = compare(args.reference, args.parsed)
    args.report.mkdir(parents=True, exist_ok=True)
    (args.report / "summary.json").write_text(
        json.dumps(counts, indent=2) + "\n", encoding="utf-8"
    )
    (args.report / "mismatches.json").write_text(
        json.dumps(errors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(counts, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
