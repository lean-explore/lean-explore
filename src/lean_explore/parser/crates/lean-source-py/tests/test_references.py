"""Reference resolution in the native Python bindings."""

import tempfile
import unittest
from pathlib import Path

import lean_source_parser

LOWER = """namespace Lib
def helper : Nat := 1
private def secret : Nat := 2
structure Point where
  x : Nat
end Lib
"""

UPPER = """import Lower
open Lib
theorem uses (p : Point) : helper = secret := sorry
@[to_additive]
theorem mul_thing : helper = 1 := sorry
theorem guesses : add_thing = add_thing := sorry
"""


class ReferencesTest(unittest.TestCase):
    """Check `parse_files(references=True)` through the public Python API."""

    def parse(self, **options):
        """Parse the two test files and return each named declaration's dict."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Lower.lean").write_text(LOWER, encoding="utf-8")
            (root / "Upper.lean").write_text(UPPER, encoding="utf-8")
            paths = [root / "Lower.lean", root / "Upper.lean"]
            files = lean_source_parser.parse_files(paths, root=root, **options)
        return {d["name"]: d for declarations in files for d in declarations}

    def test_references_are_absent_unless_requested(self):
        """Existing callers get the same dicts as before."""
        declarations = self.parse()
        self.assertTrue(all("references" not in d for d in declarations.values()))
        ignored = self.parse(known_names=["Nat"], include_approximate=True)
        self.assertTrue(all("references" not in d for d in ignored.values()))

    def test_references_follow_imports_opens_and_known_names(self):
        """Names resolve across files; private names stay in their module."""
        declarations = self.parse(references=True, known_names=["Nat"])
        self.assertEqual(declarations["Lib.helper"]["references"], ["Nat"])
        self.assertEqual(
            declarations["uses"]["references"], ["Lib.Point", "Lib.helper"]
        )
        self.assertEqual(declarations["Lib.Point.x"]["references"], ["Nat"])
        self.assertEqual(declarations["Lib.Point.mk"]["references"], ["Lib.Point"])
        self.assertEqual(declarations["guesses"]["references"], [])
        without_known = self.parse(references=True)
        self.assertEqual(without_known["Lib.helper"]["references"], [])

    def test_approximate_names_resolve_on_request(self):
        """A name derived by a naming rule is referred to only when asked for."""
        declarations = self.parse(references=True, include_approximate=True)
        self.assertEqual(declarations["guesses"]["references"], ["add_thing"])
        # A generated declaration has no signature to read.
        self.assertEqual(declarations["add_thing"]["references"], [])


if __name__ == "__main__":
    unittest.main()
