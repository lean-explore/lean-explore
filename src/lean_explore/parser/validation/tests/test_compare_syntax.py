"""Exercise failure gates in independent syntax validation."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "compare_syntax.py"


class SyntaxComparisonTests(unittest.TestCase):
    """Keep missing references and tainted syntax from becoming clean coverage."""

    def run_comparison(
        self,
        *,
        inventory=("A.lean",),
        tainted=False,
        complete=True,
        strict=False,
        prefix="",
        context_audit=True,
        extra_parser_file=False,
        namespace_audit=True,
        synthesized_namespace=False,
        reference_end=12,
        audit_version=2,
    ):
        """Run the CLI against one small authored declaration."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parsed = {
                "file": "A.lean",
                "generated": False,
                "parent": None,
                "kind": "theorem",
                "name": "a",
                "signature": [0, 1, 0, 12],
                "body": [13, 1, 13, 20],
            }
            reference = {
                "file": prefix + "A.lean",
                "kind": "Lean.Parser.Command.theorem",
                "start": 0,
                "signature_end": reference_end,
                "body_start": 13,
                "signature_tainted": tainted,
                "command_missing": False,
            }
            summary = {
                "file": prefix + "A.lean",
                "summary": True,
                "commands": 1,
                "declarations": 1,
                "errors": [],
                "context_errors": [],
                "audit_version": audit_version,
                "context_tracking": context_audit,
                "namespace_tracking": namespace_audit,
                "namespace_registrations": [{"name": "Project", "pos": 0}]
                if synthesized_namespace
                else [],
            }
            (root / "parsed.jsonl").write_text(
                json.dumps(parsed)
                + "\n"
                + (
                    json.dumps({**parsed, "file": "B.lean"}) + "\n"
                    if extra_parser_file
                    else ""
                ),
                encoding="utf-8",
            )
            rows = [reference, summary] if complete else [reference]
            (root / "syntax.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
            )
            (root / "files.txt").write_text(
                "\n".join(inventory) + "\n", encoding="utf-8"
            )
            command = [
                sys.executable,
                str(SCRIPT),
                "--parsed",
                str(root / "parsed.jsonl"),
                "--reference",
                str(root / "syntax.jsonl"),
                "--inventory",
                str(root / "files.txt"),
                "--report-dir",
                str(root / "report"),
            ]
            if strict:
                command.append("--require-clean")
            if prefix:
                command.extend(["--strip-reference-prefix", prefix])
            result = subprocess.run(
                command, capture_output=True, text=True, check=False
            )
            report = root / "report" / "summary.json"
            return result, json.loads(report.read_text()) if report.exists() else None

    def test_exact_complete_inventory_passes(self):
        """A complete clean reference matches in both directions."""
        result, report = self.run_comparison(strict=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["counts"]["exact_signature"], 1)
        self.assertEqual(report["counts"]["parser_matched_usable_reference"], 1)

    def test_incomplete_file_fails(self):
        """A declaration without its closing file summary is rejected."""
        result, _ = self.run_comparison(complete=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no summary", result.stderr)

    def test_whole_missing_file_fails(self):
        """An inventory file cannot disappear with a missing worker shard."""
        result, report = self.run_comparison(inventory=("A.lean", "B.lean"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["missing_reference_files"], ["B.lean"])

    def test_taint_is_reported_and_rejected_by_clean_gate(self):
        """Conditional agreement is distinct from clean full-reference coverage."""
        result, report = self.run_comparison(tainted=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["counts"]["tainted_signatures"], 1)
        self.assertEqual(report["counts"]["parser_matched_tainted_reference"], 1)
        result, _ = self.run_comparison(tainted=True, strict=True)
        self.assertNotEqual(result.returncode, 0)

    def test_tainted_disagreement_remains_unverified(self):
        """A malformed reference remains visible without becoming a parser defect."""
        result, report = self.run_comparison(tainted=True, reference_end=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["counts"]["tainted_boundary_disagreements"], 1)
        self.assertNotIn("signature_mismatch", report["counts"])

    def test_inventory_prefix_is_normalized(self):
        """Inventory and reference paths use the same explicit prefix mapping."""
        result, _ = self.run_comparison(
            inventory=("src/A.lean",), prefix="src/", strict=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_parser_file_outside_inventory_fails(self):
        """Extra parser declarations cannot disappear from reverse validation."""
        result, report = self.run_comparison(extra_parser_file=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["unexpected_parser_files"], ["B.lean"])

    def test_missing_namespace_audit_fails_clean_gate(self):
        """Namespace registration must be audited before a clean result."""
        result, report = self.run_comparison(namespace_audit=False, strict=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["counts"]["files_without_namespace_audit"], 1)

    def test_synthesized_namespace_fails_clean_gate(self):
        """Inventing a namespace name does not verify its imported grammar."""
        result, report = self.run_comparison(synthesized_namespace=True, strict=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["counts"]["synthesized_namespaces"], 1)

    def test_old_audit_version_fails_clean_gate(self):
        """Earlier audit capabilities remain visible after the oracle changes."""
        result, report = self.run_comparison(audit_version=1, strict=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["counts"]["files_without_current_audit"], 1)

    def test_missing_context_audit_fails_clean_gate(self):
        """Older reference output cannot silently claim audited parsing context."""
        result, report = self.run_comparison(context_audit=False, strict=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["counts"]["files_without_context_audit"], 1)


if __name__ == "__main__":
    unittest.main()
