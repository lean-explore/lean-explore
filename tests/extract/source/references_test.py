"""Tests for finding the declarations a signature refers to."""

import pytest

from lean_explore.extract.source.references import (
    FileScopes,
    Scope,
    bound_names,
    find_references,
    opened_namespaces,
    resolve,
    signature_bound_names,
    strip_comments_and_strings,
)
from tests.extract.source.fixtures import make_repository

KNOWN = {
    "Nat",
    "Nat.succ",
    "Set",
    "Set.image",
    "Pkg.Point",
    "Pkg.Point.norm",
    "Pkg.addOne",
    "Other.helper",
    "I",
}


def command(source: str, kind: str, text: str) -> dict:
    """Build a command record for the first occurrence of ``text``."""
    encoded = source.encode("utf-8")
    start = encoded.index(text.encode("utf-8"))
    end = start + len(text.encode("utf-8"))
    return {"kind": kind, "range": (start, 0, 0, end, 0, 0)}


def test_strip_comments_and_strings_removes_their_words():
    """Words in comments and strings are not left in the text."""
    text = 'theorem t /- Set -/ : "Nat" = x -- Other.helper'
    stripped = strip_comments_and_strings(text)
    assert "Set" not in stripped
    assert "Nat" not in stripped
    assert "Other" not in stripped
    assert "theorem t" in stripped


def test_bound_names_reads_bracket_and_keyword_binders():
    """Binder names come from brackets and from quantifier-style binders."""
    text = "(a b : Nat) {s : Set Nat} [inst : Foo] : ∀ x, ∃ y z, fun w => a"
    assert bound_names(text) == {"a", "b", "s", "inst", "x", "y", "z", "w"}


def test_bound_names_ignores_definitions_and_qualified_names():
    """A ``:=`` is not a binder colon and dotted names are not binders."""
    assert bound_names("(x := 1) (Foo.bar : Nat)") == set()


def test_signature_bound_names_ignores_ascriptions_in_the_type():
    """A bracketed ``(x : T)`` after the type colon is not a binder."""
    signature = "theorem t {s : Set Nat} (h := 1) : (I : Set Nat) = ∀ x, s"
    assert signature_bound_names(signature) == {"s", "x"}


def test_signature_bound_names_without_a_type():
    """A signature with no type colon is all binders."""
    assert signature_bound_names("def f (n : Nat)") == {"n"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("open Set Filter", ["Set", "Filter"]),
        ("open scoped Topology in", ["Topology"]),
        ("open Set (image preimage)", ["Set"]),
        ("open Set hiding image", ["Set"]),
        ("open Set renaming image → img", ["Set"]),
    ],
)
def test_opened_namespaces(text, expected):
    """Only the namespaces of an ``open`` command are returned."""
    assert opened_namespaces(text) == expected


def test_resolve_prefers_the_declaration_namespace():
    """A name in the declaration's own namespace wins over the root."""
    assert resolve("addOne", "Pkg.thing", (), KNOWN) == "Pkg.addOne"
    assert resolve("Nat", "Pkg.thing", (), KNOWN) == "Nat"


def test_resolve_uses_opened_namespaces():
    """An opened namespace makes its members available unqualified."""
    assert resolve("helper", "Pkg.thing", (), KNOWN) is None
    assert resolve("helper", "Pkg.thing", ("Other",), KNOWN) == "Other.helper"


def test_resolve_opened_namespace_relative_to_the_current_one():
    """``open Point`` inside ``Pkg`` reaches ``Pkg.Point``."""
    assert resolve("norm", "Pkg.thing", ("Point",), KNOWN) == "Pkg.Point.norm"


def test_resolve_drops_trailing_field_access():
    """A dotted identifier falls back to its longest known prefix."""
    assert resolve("Nat.succ.injective", "t", (), KNOWN) == "Nat.succ"
    assert resolve("unknown.field", "t", (), KNOWN) is None


def test_find_references_lists_names_in_order_without_self():
    """References keep first-appearance order and leave out the declaration."""
    signature = "theorem Pkg.addOne_succ (n : Nat) : Pkg.addOne n = Nat.succ n"
    references = find_references("Pkg.addOne_succ", signature, Scope(), KNOWN)
    assert references == ["Nat", "Pkg.addOne", "Nat.succ"]


def test_find_references_skips_bound_names():
    """A binder named like a constant is not a reference to it."""
    signature = "theorem t (I : Set Nat) : I = I"
    assert find_references("t", signature, Scope(), KNOWN) == ["Set", "Nat"]


def test_find_references_keeps_ascribed_constants():
    """A constant with a type ascription in the type is still a reference."""
    signature = "theorem t : (I : Set Nat) = I"
    assert find_references("t", signature, Scope(), KNOWN) == ["I", "Set", "Nat"]


def test_find_references_skips_variables_and_their_fields():
    """Names bound by ``variable`` are local, including field access on them."""
    scope = Scope(bound=frozenset({"I"}))
    assert find_references("t", "theorem t : I.image = I", scope, KNOWN) is None


def test_find_references_returns_none_without_references():
    """A signature with no known names has no references."""
    assert find_references("t", "theorem t : True", Scope(), KNOWN) is None


def test_file_scopes_follow_sections_and_namespaces():
    """Opens and variables last until the end of their section."""
    source = (
        "open Set\n"
        "section\n"
        "open Other\n"
        "variable (I : Nat)\n"
        "theorem first : True := trivial\n"
        "end\n"
        "theorem second : True := trivial\n"
    )
    commands = [
        command(source, "open", "open Set"),
        command(source, "section", "section"),
        command(source, "open", "open Other"),
        command(source, "variable", "variable (I : Nat)"),
        command(source, "declaration", "theorem first : True := trivial"),
        command(source, "end", "end"),
        command(source, "declaration", "theorem second : True := trivial"),
    ]
    scopes = FileScopes(source, commands)
    first = scopes.at(source.index("theorem first"))
    second = scopes.at(source.index("theorem second"))
    assert first == Scope(("Set", "Other"), frozenset({"I"}))
    assert second == Scope(("Set",), frozenset())


def test_file_scopes_open_in_applies_to_one_command():
    """``open ... in`` reaches only the command that follows it."""
    source = (
        "open Other in\n"
        "theorem first : True := trivial\n"
        "theorem second : True := trivial\n"
    )
    commands = [
        command(source, "open", "open Other in"),
        command(source, "declaration", "theorem first : True := trivial"),
        command(source, "declaration", "theorem second : True := trivial"),
    ]
    scopes = FileScopes(source, commands)
    assert scopes.at(source.index("theorem first")).opened == ("Other",)
    assert scopes.at(source.index("theorem second")).opened == ()


def test_file_scopes_before_the_first_command_and_unbalanced_end():
    """Offsets before any command and a stray ``end`` do not fail."""
    source = "end\ntheorem t : True := trivial\n"
    commands = [command(source, "end", "end")]
    scopes = FileScopes(source, commands)
    assert scopes.at(len(source)) == Scope()
    assert FileScopes("", []).at(0) == Scope()


SOURCE_PACKAGE = """namespace Pkg

/-- A point. -/
structure Point where
  x : Nat

def addOne (n : Nat) : Nat := n + 1

section
variable (addOne : Nat)

theorem shadowed : addOne = addOne := rfl

end

theorem addOne_pos (p : Point) : 0 < addOne p.x := Nat.succ_pos _

end Pkg

open Pkg in
theorem outside : addOne 0 = 1 := rfl
"""


def test_parse_package_sources_stores_references(tmp_path):
    """Source rows carry the references found in their signatures."""
    pytest.importorskip("lean_source_parser")
    from lean_explore.extract.source.reader import parse_package_sources

    root = make_repository(tmp_path / "pkg", {"Pkg/Basic.lean": SOURCE_PACKAGE})
    rows = parse_package_sources(
        root, "https://example.com/", lambda module: True, known_names={"Nat"}
    )
    dependencies = {row.name: row.dependencies for row in rows}
    assert dependencies["Pkg.addOne"] == ["Nat"]
    assert dependencies["Pkg.shadowed"] is None
    assert dependencies["Pkg.addOne_pos"] == ["Pkg.Point", "Pkg.addOne"]
    assert dependencies["outside"] == ["Pkg.addOne"]
