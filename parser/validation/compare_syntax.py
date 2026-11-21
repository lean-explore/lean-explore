"""Compare independent Lean command-AST signatures with Rust JSONL output."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

CURRENT_AUDIT_VERSION = 2


def read_records(path):
    """Read strict JSONL records with source locations for malformed data."""
    with path.open(encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            try:
                yield json.loads(line)
            except (ValueError, TypeError) as error:
                raise ValueError(f"{path}:{number}: invalid JSON record") from error


def file_groups(paths):
    """Yield complete, unique file groups and reject truncated shards."""
    seen = set()
    for path in paths:
        rows = []
        for row in read_records(path):
            if not row.get("summary"):
                rows.append(row)
                continue
            file = row["file"]
            if file in seen:
                raise ValueError(f"duplicate reference file: {file}")
            if any(item["file"] != file for item in rows):
                raise ValueError(f"interleaved reference file: {file}")
            if row["declarations"] != len(rows):
                raise ValueError(f"incomplete declaration records: {file}")
            seen.add(file)
            yield row, rows
            rows = []
        if rows:
            raise ValueError(f"reference file has no summary: {path}")


def main():
    """Compare signatures and record the limits of independent coverage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parsed", type=Path, required=True)
    parser.add_argument(
        "--inventory",
        type=Path,
        required=True,
        help="independent inventory of every expected source file",
    )
    parser.add_argument("--reference", type=Path, nargs="+", required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--strip-reference-prefix", default="")
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="also fail on Lean parse errors or tainted signatures",
    )
    args = parser.parse_args()
    expected_files = {
        file.removeprefix(args.strip_reference_prefix)
        for file in args.inventory.read_text(encoding="utf-8-sig").splitlines()
        if file
    }
    seen_files = set()
    by_pos = defaultdict(list)
    parser_files = set()
    for record in read_records(args.parsed):
        parser_files.add(record["file"])
        if not record["generated"] and record["parent"] is None:
            by_pos[(record["file"], record["signature"][0])].append(
                {key: record[key] for key in ("name", "kind", "signature", "body")}
            )
    counts = Counter()
    kinds = defaultdict(Counter)
    unsupported_parser_kinds = Counter()
    mismatches, errors, unmatched, tainted_disagreements = [], [], [], []
    supported_kinds = {
        "def",
        "theorem",
        "lemma",
        "instance",
        "example",
        "structure",
        "class",
        "abbrev",
        "axiom",
        "inductive",
        "coinductive",
        "opaque",
        "irreducible_def",
        "proof_wanted",
    }
    by_file = defaultdict(list)
    for (file, start), records in by_pos.items():
        by_file[file].extend(records)
    for summary, rows in file_groups(args.reference):
        counts["files"] += 1
        seen_files.add(summary["file"].removeprefix(args.strip_reference_prefix))
        counts["commands"] += summary["commands"]
        counts["files_with_lean_errors"] += bool(summary["errors"])
        counts["lean_errors"] += len(summary["errors"])
        context_errors = summary.get("context_errors", [])
        counts["files_without_current_audit"] += (
            summary.get("audit_version") != CURRENT_AUDIT_VERSION
        )
        counts["files_without_namespace_audit"] += not summary.get(
            "namespace_tracking", False
        )
        counts["synthesized_namespaces"] += len(
            summary.get("namespace_registrations", [])
        )
        counts["files_without_context_audit"] += not summary.get(
            "context_tracking", False
        )
        counts["context_errors"] += len(context_errors)
        counts["grammar_context_errors"] += sum(
            error["affects_grammar"] for error in context_errors
        )
        if summary["errors"] or context_errors:
            errors.append(summary)
        error_positions = [
            error["pos"] for error in summary["errors"] if "pos" in error
        ]
        reference_positions, tainted_positions = set(), set()
        for reference in rows:
            kind = reference["kind"].rsplit(".", 1)[-1]
            category = kinds[kind]
            counts["reference_declarations"] += 1
            category["total"] += 1
            start, end = reference["start"], reference["signature_end"]
            body = reference["body_start"]
            tainted = reference["signature_tainted"] or start is None or end is None
            if body is None:
                tainted |= reference["command_missing"]
                # Recovery can report an error in the next command at this boundary.
                tainted |= end in error_positions
            elif start is not None:
                tainted |= any(start <= pos < body for pos in error_positions)
            if tainted:
                tainted_positions.add(start)
                counts["tainted_signatures"] += 1
                category["tainted"] += 1
                file = reference["file"].removeprefix(args.strip_reference_prefix)
                matches = by_pos.get((file, start), [])
                if len(matches) == 1 and matches[0]["signature"][3] == end:
                    counts["tainted_exact_boundary"] += 1
                else:
                    counts["tainted_boundary_disagreements"] += 1
                    tainted_disagreements.append(
                        {
                            "reference": reference,
                            "matches": matches,
                            "status": "unverified_reference",
                        }
                    )
                continue
            reference_positions.add(start)
            counts["untainted_signatures"] += 1
            file = reference["file"].removeprefix(args.strip_reference_prefix)
            matches = by_pos.get((file, start), [])
            if len(matches) != 1:
                counts["missing_or_ambiguous"] += 1
                category["missing_or_ambiguous"] += 1
                mismatches.append(
                    {
                        "reason": "missing_or_ambiguous",
                        "reference": reference,
                        "matches": len(matches),
                    }
                )
                continue
            parsed = matches[0]
            counts["found"] += 1
            category["found"] += 1
            if parsed["signature"][3] == end:
                counts["exact_signature"] += 1
                category["exact_signature"] += 1
            else:
                counts["signature_mismatch"] += 1
                category["signature_mismatch"] += 1
                mismatches.append(
                    {
                        "reason": "signature",
                        "reference": reference,
                        "parser_name": parsed["name"],
                        "parser_signature": parsed["signature"],
                        "parser_body": parsed["body"],
                    }
                )
        file = summary["file"].removeprefix(args.strip_reference_prefix)
        for parsed in by_file.get(file, []):
            counts["parser_top_level_authored"] += 1
            start = parsed["signature"][0]
            if start in reference_positions:
                counts["parser_matched_usable_reference"] += 1
            elif start in tainted_positions:
                counts["parser_matched_tainted_reference"] += 1
            elif parsed["kind"] not in supported_kinds:
                counts["parser_unsupported_kind"] += 1
                unsupported_parser_kinds[parsed["kind"]] += 1
            else:
                state = "error_file" if summary["errors"] else "clean_file"
                counts["parser_unmatched_" + state] += 1
                unmatched.append({"file": file, "state": state, "parser": parsed})
    args.report_dir.mkdir(parents=True, exist_ok=True)
    unexpected_parser_files = sorted(parser_files - expected_files)
    missing_files = sorted(expected_files - seen_files)
    unexpected_files = sorted(seen_files - expected_files)
    counts["unexpected_parser_files"] = len(unexpected_parser_files)
    counts["inventory_files"] = len(expected_files)
    counts["missing_reference_files"] = len(missing_files)
    counts["unexpected_reference_files"] = len(unexpected_files)
    report = {
        "counts": dict(counts),
        "kinds": dict(kinds),
        "unsupported_parser_kinds": dict(unsupported_parser_kinds),
        "unexpected_parser_files": unexpected_parser_files,
        "missing_reference_files": missing_files,
        "unexpected_reference_files": unexpected_files,
    }
    for filename, value in [
        ("summary.json", report),
        ("mismatches.json", mismatches),
        ("lean-errors.json", errors),
        ("unmatched-parser.json", unmatched),
        ("tainted-disagreements.json", tainted_disagreements),
    ]:
        (args.report_dir / filename).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "counts": dict(counts),
                "kinds": dict(kinds),
                "unsupported_parser_kinds": dict(unsupported_parser_kinds),
            },
            indent=2,
        )
    )
    failed = (
        bool(mismatches or missing_files or unexpected_files or unexpected_parser_files)
        or counts["reference_declarations"] == 0
    )
    if args.require_clean:
        failed |= bool(
            counts["lean_errors"]
            or counts["tainted_signatures"]
            or unmatched
            or counts["files_without_context_audit"]
            or counts["grammar_context_errors"]
            or counts["files_without_current_audit"]
            or counts["files_without_namespace_audit"]
            or counts["synthesized_namespaces"]
        )
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
