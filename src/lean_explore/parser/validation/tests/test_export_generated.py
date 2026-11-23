"""Check semantic sidecar provenance, Unicode positions, and invalid references."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from export_generated import Source, attribute_tokens, export, source_name  # noqa: E402


class GeneratedExportTests(unittest.TestCase):
    """Use small source files and range records with known byte positions."""

    def test_unicode_positions_and_invalid_ranges(self):
        """Lean columns count Unicode code points; output offsets count bytes."""
        source = Source("αβ\n@[simps] def x := 1\n".encode())
        self.assertEqual(source.span([1, 1, 2, 2]), [2, 1, 1, 7, 2, 2])
        for positions in ([0, 0, 1, 1], [1, 3, 2, 0], [2, 1, 1, 0]):
            with self.assertRaises(ValueError):
                source.span(positions)

    def test_export_filters_and_fingerprints(self):
        """Selected constants retain their kernel names and source provenance."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Mathlib").mkdir()
            data = b"@[simps] def x := 1\n"
            (root / "Mathlib/Test.lean").write_bytes(data)
            record = {
                "m": "Mathlib.Test",
                "n": "_private.Mathlib.Test.0.x_apply",
                "r": [1, 2, 1, 7],
                "s": [1, 2, 1, 7],
                "k": "thm",
                "f": [],
            }
            ranges = root / "ranges.jsonl"
            records = [
                record,
                {"m": "Mathlib.Test", "n": "without_range"},
                {"m": "Mathlibrary.Test", "n": "wrong_prefix"},
            ]
            ranges.write_text("".join(json.dumps(row) + "\n" for row in records))
            output, metadata = export(ranges, root, {"simps"}, "Mathlib")
            self.assertEqual(len(output), 1)
            self.assertEqual(output[0]["name"], "x_apply")
            self.assertEqual(output[0]["kernel_name"], record["n"])
            self.assertEqual(output[0]["source_sha256"], Source(data).sha256)
            self.assertEqual(output[0]["provenance"], "lean-environment")
            self.assertFalse(output[0]["approximate"])
            self.assertEqual(metadata["reference_constants_without_ranges"], 1)
            self.assertEqual(metadata["by_attribute"], {"simps": 1})
            output, _ = export(ranges, root, {"ext"}, "Mathlib")
            self.assertEqual(output, [])
            record["s"] = [1, 2, 1, 999]
            ranges.write_text(json.dumps(record) + "\n")
            with self.assertRaises(ValueError):
                export(ranges, root, {"simps"}, "Mathlib")

    def test_only_attribute_heads_are_attributed(self):
        """Multiline and standalone attributes are accepted; arguments are excluded."""
        text = (
            "@[\n local simps! (other := simp),\n"
            " simps2, simps.foo, simpsα] def x := 1\n"
            "attribute [simps, -simps] x\n"
        )
        heads = attribute_tokens(text)
        self.assertEqual(
            [head[0] for head in heads.values()],
            ["simps!", "simps2", "simps.foo", "simpsα", "simps"],
        )
        self.assertNotIn(text.index("simp)"), heads)
        self.assertNotIn(text.index("-simps") + 1, heads)

    def test_comments_strings_and_quoted_names_are_ignored(self):
        """Attribute-like text in nested comments and literals cannot select rows."""
        text = (
            "/- outer /- @[simps] -/ @[simps] -/\n"
            "-- @[simps]\n"
            'def quoted := "@[simps]"\n'
            'def raw := r#"a " @[simps] b"#\n'
            "def «@[simps]» := 1\n"
            "@[other (x := '['), simps] def real := 1\n"
        )
        self.assertEqual(
            [head[0] for head in attribute_tokens(text).values()], ["other", "simps"]
        )

    def test_nested_attribute_configurations_are_attributed(self):
        """Translation attributes can apply simps to a generated declaration."""
        text = "@[to_additive (attr := simp, simps! (config := {})), simps] def x := 1"
        self.assertEqual(
            list(attribute_tokens(text).values()),
            [
                ("to_additive", "direct"),
                ("simp", "nested-attr"),
                ("simps!", "nested-attr"),
                ("simps", "direct"),
            ],
        )

    def _assert_cli_rejects_aliases(self, reference, output, root):
        metadata = output.with_suffix(output.suffix + ".meta.json")
        before = {
            path: path.read_bytes() if path.exists() else None
            for path in (reference, output, metadata)
        }
        command = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "export_generated.py"),
            str(reference),
            str(root),
            "-o",
            str(output),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("must refer to different files", result.stderr)
        for path, contents in before.items():
            if contents is None:
                self.assertFalse(path.exists())
            else:
                self.assertEqual(path.read_bytes(), contents)

    def test_cli_preserves_reference_when_metadata_path_collides(self):
        """The derived metadata filename must not overwrite the reference input."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "sidecar.jsonl"
            reference = root / "sidecar.jsonl.meta.json"
            reference.write_bytes(b'{"m":"Mathlib.Test","n":"without_range"}\n')
            self._assert_cli_rejects_aliases(reference, output, root)

    def test_cli_rejects_hardlink_aliases_between_all_three_paths(self):
        """Different filenames must not allow either output to overwrite an alias."""
        for pair in ("reference-output", "reference-metadata", "output-metadata"):
            with self.subTest(pair=pair), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                reference = root / "reference.jsonl"
                output = root / "sidecar.jsonl"
                metadata = root / "sidecar.jsonl.meta.json"
                reference.write_bytes(b'{"m":"Mathlib.Test","n":"without_range"}\n')
                if pair == "output-metadata":
                    output.write_bytes(b"previous output\n")
                    source, alias = output, metadata
                else:
                    source = reference
                    alias = output if pair == "reference-output" else metadata
                try:
                    os.link(source, alias)
                except OSError as error:
                    self.skipTest(f"Hard links are unavailable: {error}")
                self._assert_cli_rejects_aliases(reference, output, root)

    def test_private_name_without_standard_prefix_is_preserved(self):
        """Do not guess a source name when Lean's private marker is absent."""
        self.assertEqual(source_name("_private.Other.x"), "_private.Other.x")


if __name__ == "__main__":
    unittest.main()
