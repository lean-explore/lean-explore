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


NOTATION = """prelude
class HAdd (α β γ) where
  hAdd : α → β → γ
inductive Eq : α → α → Prop
infixl:65 " + " => HAdd.hAdd
infix:50 " = " => Eq
"""

USES_NOTATION = """structure Point where
  x : Nat
def Point.norm (p : Point) : Nat := p.x
def twice (n : Nat) : Nat := n
infixl:70 " ⋆ " => Point.norm
theorem uses (p : Point) (h : p.norm = twice 1) : p.x + 1 = p.norm := sorry
"""


class NotationAndFieldsTest(unittest.TestCase):
    """Check references made through notation and field access."""

    def parse(self, with_context):
        """Parse a file, with or without a stand-in for Lean's `Init`."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pkg").mkdir()
            (root / "pkg" / "Uses.lean").write_text(USES_NOTATION, encoding="utf-8")
            (root / "lean").mkdir()
            (root / "lean" / "Init.lean").write_text(NOTATION, encoding="utf-8")
            files = lean_source_parser.parse_files(
                [root / "pkg" / "Uses.lean"],
                root=root / "pkg",
                references=True,
                context_paths=[root / "lean" / "Init.lean"] if with_context else None,
            )
        return {d["name"]: d for declarations in files for d in declarations}

    def test_context_files_supply_notations(self):
        """A token stands for the constants of the notation that declares it."""
        declarations = self.parse(with_context=True)
        self.assertEqual(
            declarations["uses"]["references"],
            ["Point", "Point.norm", "Eq", "twice", "Point.x", "HAdd.hAdd"],
        )
        without = self.parse(with_context=False)
        self.assertEqual(
            without["uses"]["references"],
            ["Point", "Point.norm", "twice", "Point.x"],
        )

    def test_type_references_are_the_ones_after_the_colon(self):
        """Names written only in binders are left out of `type_references`."""
        declarations = self.parse(with_context=True)
        self.assertEqual(
            declarations["uses"]["type_references"],
            ["Point.norm", "Eq", "Point.x", "HAdd.hAdd"],
        )
        self.assertEqual(declarations["twice"]["type_references"], [])


if __name__ == "__main__":
    unittest.main()
