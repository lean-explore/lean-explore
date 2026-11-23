"""Imported structure context in the native Python bindings."""

import tempfile
import unittest
from pathlib import Path

import lean_source_parser


class ContextFilesTest(unittest.TestCase):
    """Check dependency context through the public Python API."""

    def test_context_resolves_fields_without_emitting_imports(self):
        """Inherited defaults are omitted and new fields remain exact."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "Base.lean"
            child = root / "Child.lean"
            base.write_text("structure Base where\n  x : Nat\n", encoding="utf-8")
            child.write_text(
                "structure Child extends Base where\n  x := 1\n  y := 2\n",
                encoding="utf-8",
            )
            without_context = lean_source_parser.parse_files([child], root=root)[0]
            uncertain = [d for d in without_context if d["kind"] == "field"]
            self.assertEqual({d["name"] for d in uncertain}, {"Child.x", "Child.y"})
            self.assertTrue(all(d["approximate"] for d in uncertain))

            result = lean_source_parser.parse_files(
                [child], root=root, context_paths=[base]
            )
            self.assertEqual(len(result), 1)
            fields = [d for d in result[0] if d["kind"] == "field"]
            self.assertEqual([d["name"] for d in fields], ["Child.y"])
            self.assertFalse(fields[0]["approximate"])
            self.assertTrue(all(d["file"] == "Child.lean" for d in result[0]))

    def test_context_resolves_explicit_simps_prefixes(self):
        """Imported projection configuration resolves prefix names."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "Configuration.lean"
            target = root / "Target.lean"
            base.write_text(
                "structure Box where\n  val : Nat\n"
                "initialize_simps_projections Box (val -> coe, as_prefix coe)\n",
                encoding="utf-8",
            )
            target.write_text(
                "@[simps coe] def copy : Box := Box.mk 1\n", encoding="utf-8"
            )
            unresolved = lean_source_parser.parse_files([target], root=root)[0]
            generated = [d for d in unresolved if d["generated"]]
            self.assertEqual([d["name"] for d in generated], ["copy_coe"])
            self.assertTrue(generated[0]["approximate"])
            resolved = lean_source_parser.parse_files(
                [target], root=root, context_paths=[base]
            )[0]
            generated = [d for d in resolved if d["generated"]]
            self.assertEqual([d["name"] for d in generated], ["coe_copy"])
            self.assertFalse(generated[0]["approximate"])

    def test_missing_context_is_an_error_even_without_target_files(self):
        """Do not silently ignore unreadable dependency files."""
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "Missing.lean"
            with self.assertRaisesRegex(ValueError, "Missing.lean"):
                lean_source_parser.parse_files([], context_paths=[missing])


if __name__ == "__main__":
    unittest.main()
