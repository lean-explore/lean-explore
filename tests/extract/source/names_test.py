"""Tests for resolving written identifiers to declaration names."""

from lean_explore.extract.source.names import NameTable, Position, Use, resolve
from lean_explore.extract.source.scopes import OpenDeclaration, parse_open

NAMES = {
    "T",
    "Nat",
    "Nat.succ",
    "Pkg.T",
    "Pkg.Point",
    "Pkg.Point.norm",
    "Pkg.addOne",
    "Other.helper",
    "Other.Path.mark",
    "Path.mark",
    "A.f",
    "B.f",
    "A.g",
}


def table(**options) -> NameTable:
    """Build a table of the test names."""
    return NameTable(NAMES, **options)


def opens(text: str, current: str = "") -> tuple[OpenDeclaration, ...]:
    """Return the open declarations of an ``open`` command."""
    return tuple(parse_open(text, current))


def test_namespaces_are_the_prefixes_of_names():
    """Every proper prefix of a name is a namespace."""
    assert table().namespaces == {"Nat", "Pkg", "Pkg.Point", "Other", "Other.Path"} | {
        "Path",
        "A",
        "B",
    }


def test_innermost_namespace_wins_over_the_root():
    """A name in the declaration's namespace shadows the root name."""
    assert resolve("T", table(), Use("Pkg.thing")) == "Pkg.T"
    assert resolve("T", table(), Use("Elsewhere.thing")) == "T"
    assert resolve("missing", table(), Use("Pkg.thing")) is None


def test_explicit_namespaces_replace_the_ones_of_the_name():
    """A rooted declaration is resolved in the namespace it is written in."""
    use = Use("Elsewhere.thing", namespaces=("Pkg",))
    assert resolve("addOne", table(), use) == "Pkg.addOne"


def test_root_marker_is_an_exact_lookup():
    """``_root_.T`` skips the declaration's namespaces."""
    assert resolve("_root_.T", table(), Use("Pkg.thing")) == "T"
    assert resolve("_root_.missing", table(), Use("Pkg.thing")) is None


def test_trailing_components_are_field_access():
    """A dotted identifier falls back to its longest known prefix."""
    assert resolve("Nat.succ.injective", table(), Use("t")) == "Nat.succ"
    assert resolve("unknown.field", table(), Use("t")) is None


def test_open_makes_members_available():
    """An opened namespace is searched after the root."""
    assert resolve("helper", table(), Use("t")) is None
    assert resolve("helper", table(), Use("t", opens("open Other"))) == "Other.helper"


def test_open_binds_to_the_innermost_namespace():
    """``open Point`` inside ``Pkg`` opens ``Pkg.Point``."""
    use = Use("Pkg.thing", opens("open Point", "Pkg"))
    assert resolve("norm", table(), use) == "Pkg.Point.norm"


def test_open_reaches_a_namespace_of_an_earlier_open():
    """``open Other Path`` opens ``Other.Path`` when there is no root ``Path``."""
    names = NameTable(NAMES - {"Path.mark"})
    use = Use("t", opens("open Other Path"))
    assert resolve("mark", names, use) == "Other.Path.mark"


def test_exact_dotted_root_name_beats_an_open():
    """A dotted name that exists at the root is not looked up through opens."""
    use = Use("t", opens("open Other"))
    assert resolve("Path.mark", table(), use) == "Path.mark"


def test_equal_candidates_resolve_to_nothing():
    """Lean chooses between two opened names by type, so neither is taken."""
    assert resolve("f", table(), Use("t", opens("open A B"))) is None
    assert resolve("g", table(), Use("t", opens("open A B"))) == "A.g"


def test_selective_hiding_and_renaming_opens():
    """Selections, exclusions and renamings limit what an open provides."""
    assert resolve("f", table(), Use("t", opens("open A (g)"))) is None
    assert resolve("g", table(), Use("t", opens("open A (g)"))) == "A.g"
    assert resolve("g", table(), Use("t", opens("open A hiding g"))) is None
    renamed = Use("t", opens("open A renaming g → h"))
    assert resolve("h", table(), renamed) == "A.g"


def test_unknown_opened_namespace_provides_nothing():
    """An ``open`` of a namespace with no known names is ignored."""
    assert resolve("f", table(), Use("t", opens("open Missing"))) is None


def test_exported_alias_resolves_to_its_target():
    """An alias from ``export`` stands for the exported declaration."""
    (exported,) = parse_open("export A (g)", "N")
    names = table(aliases={"N.g": exported})
    assert resolve("g", names, Use("N.thing")) == "A.g"
    assert resolve("g", names, Use("M.thing")) is None


def test_protected_names_need_a_qualified_spelling():
    """A protected name is not reached by its last component alone."""
    names = table(protected={"Pkg.T"})
    assert resolve("T", names, Use("Pkg.thing")) == "T"
    assert resolve("Pkg.T", names, Use("thing")) == "Pkg.T"
    selected = Use("t", opens("open Pkg (T)"))
    assert resolve("T", names, selected) is None


def test_private_names_shadow_only_in_their_module():
    """A private name hides the root name in its own module and is not stored."""
    positions = {"Pkg.T": Position(0, 10)}
    names = table(private={"Pkg.T"}, positions=positions)
    same_module = Use("Pkg.thing", position=Position(0, 50))
    other_module = Use("Pkg.thing", position=Position(1, 50))
    assert resolve("T", names, same_module) is None
    assert resolve("T", names, other_module) == "T"


def test_unrecorded_names_shadow_without_being_references():
    """A name that is declared but not stored hides others and resolves to none."""
    names = table(unrecorded={"Pkg.T"})
    assert resolve("T", names, Use("Pkg.thing")) is None
    assert resolve("_root_.Pkg.T", names, Use("thing")) is None


def test_later_declarations_of_a_file_are_not_visible():
    """A name declared after its use does not shadow an earlier one."""
    positions = {"T": Position(0, 0), "Pkg.T": Position(0, 100)}
    names = table(positions=positions)
    before = Use("Pkg.thing", position=Position(0, 50))
    after = Use("Pkg.thing", position=Position(0, 150))
    assert resolve("T", names, before) == "T"
    assert resolve("T", names, after) == "Pkg.T"


def test_a_mutual_block_sees_its_later_declarations():
    """Inside ``mutual`` a later declaration of the block is visible."""
    positions = {"Pkg.T": Position(0, 100)}
    names = table(positions=positions)
    use = Use("Pkg.thing", position=Position(0, 50), mutual_start=40)
    assert resolve("T", names, use) == "Pkg.T"


def test_names_of_modules_that_are_not_imported_are_not_visible():
    """A name is visible only from modules that import its module."""
    positions = {"Pkg.T": Position(1, 0)}
    use = Use("Pkg.thing", position=Position(0, 50))
    imported = table(positions=positions, visible=[0b11, 0b10])
    separate = table(positions=positions, visible=[0b01, 0b10])
    assert resolve("T", imported, use) == "Pkg.T"
    assert resolve("T", separate, use) == "T"
    assert resolve("T", table(positions=positions), use) == "Pkg.T"


def test_sibling_fields_resolve_to_the_structure_field():
    """A field named in another field's type is that structure's field."""
    use = Use("S.other", namespaces=(), siblings={"val": "S.val"})
    names = NameTable({"S.val", "S.other", "val"})
    assert resolve("val", names, use) == "S.val"
