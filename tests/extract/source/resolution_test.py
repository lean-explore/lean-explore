"""Tests for resolving the references of a package's parsed declarations."""

from lean_explore.extract.source.resolution import (
    Row,
    SourceFile,
    is_named,
    lookup_namespaces,
    name_table,
    owner_context,
    resolve_rows,
)
from lean_explore.extract.source.scopes import Scope
from lean_explore.extract.types import Declaration


def parsed(name, signature="", kind="def", offset=0, **options) -> dict:
    """Build a declaration record as the parser reports it."""
    return {
        "name": name,
        "kind": kind,
        "signature_text": signature,
        "text": signature,
        "range": (offset, 0, 0, offset + 1, 0, 0),
        "modifiers": options.get("modifiers", []),
        "approximate": options.get("approximate", False),
        "parent": options.get("parent"),
    }


def row(record: dict, stored: bool = True, **options) -> Row:
    """Build a row for a parsed declaration."""
    declaration = Declaration(
        name=record["name"],
        module="M",
        docstring=None,
        source_text="",
        source_link="",
        dependencies=None,
    )
    return Row(
        record,
        options.get("scope", Scope()),
        declaration if stored else None,
        options.get("namespaces", ()),
        options.get("bound", []),
        options.get("siblings", {}),
    )


def source_file(module: str, rows: list[Row], imports: list[str] = ()) -> SourceFile:
    """Build a file from its rows."""
    return SourceFile(module, rows, list(imports), {}, [r.parsed["name"] for r in rows])


def dependencies(files: list[SourceFile], known=()) -> dict:
    """Resolve the files and return each stored name's dependencies."""
    rows = resolve_rows(files, name_table(files, known))
    return {declaration.name: declaration.dependencies for declaration in rows}


def test_is_named():
    """Unnamed and guessed declarations cannot be referred to."""
    assert is_named(parsed("a"), False)
    assert not is_named(parsed(None), False)
    assert not is_named(parsed("a", approximate=True), False)
    assert is_named(parsed("a", approximate=True), True)


def test_lookup_namespaces():
    """A dotted name adds its namespaces; a rooted one keeps the enclosing."""
    inside = Scope(namespace="A")
    assert lookup_namespaces(parsed("A.Foo.bar"), inside, []) == ("A.Foo", "A")
    assert lookup_namespaces(parsed("Foo.bar"), inside, []) == ("A",)
    assert lookup_namespaces(parsed("Foo.bar"), Scope(), []) == ("Foo",)
    assert lookup_namespaces(parsed(None), Scope(), []) == ()


def test_fields_and_constructors_use_their_owner():
    """A field is resolved where its structure is, with its binders bound."""
    owner = parsed("N.S", "structure S (α : Type)", "structure")
    first = parsed("N.S.val", "val : α", "field", parent=0)
    second = parsed("N.S.other", "other : val = val", "field", parent=0)
    constructor = parsed("N.S.mk", "| mk", "constructor", parent=0)
    records = [owner, first, second, constructor]
    assert lookup_namespaces(second, Scope(namespace="N"), records) == ("N",)
    bound, siblings = owner_context(second, records)
    assert bound == ["α"]
    assert siblings == {"val": "N.S.val", "other": "N.S.other"}
    assert owner_context(constructor, records) == (["α"], {})
    assert owner_context(owner, records) == ([], {})


def test_references_within_a_file_follow_declaration_order():
    """A declaration refers to earlier ones, not to later ones."""
    files = [
        source_file(
            "M",
            [
                row(parsed("T", "abbrev T", offset=0)),
                row(parsed("A.f", "def f : T", offset=10), namespaces=("A",)),
                row(parsed("A.T", "theorem T : f = f", offset=20), namespaces=("A",)),
            ],
        )
    ]
    assert dependencies(files) == {"T": None, "A.f": ["T"], "A.T": ["A.f"]}


def test_references_across_files_follow_imports():
    """A name is visible from a module only through its imports."""
    lower = source_file("Lower", [row(parsed("base", "def base"))])
    upper = source_file(
        "Upper", [row(parsed("uses", "def uses : base = Nat"))], ["Lower", "Elsewhere"]
    )
    apart = source_file("Apart", [row(parsed("misses", "def misses : base"))])
    top = source_file("Top", [row(parsed("far", "def far : base"))], ["Upper"])
    found = dependencies([lower, upper, apart, top], known={"Nat"})
    assert found == {
        "base": None,
        "uses": ["base", "Nat"],
        "misses": None,
        "far": ["base"],
    }


def test_private_and_protected_names():
    """A private name hides a root name; a protected one needs its namespace."""
    files = [
        source_file(
            "M",
            [
                row(parsed("T", "abbrev T", offset=0)),
                row(
                    parsed("A.T", "abbrev T", offset=10, modifiers=["private"]),
                    stored=False,
                ),
                row(parsed("A.f", "def f : T", offset=20), namespaces=("A",)),
                row(parsed("B.T", "abbrev T", offset=30, modifiers=["protected"])),
                row(parsed("B.g", "def g : T", offset=40), namespaces=("B",)),
            ],
        )
    ]
    found = dependencies(files)
    assert found["A.f"] is None
    assert found["B.g"] == ["T"]


def test_a_name_declared_twice_is_stored_once():
    """The first declaration of a name is the one that is kept."""
    files = [
        source_file("M", [row(parsed("a", "def a")), row(parsed("a", "def a : a"))])
    ]
    assert dependencies(files) == {"a": None}


def test_constructor_refers_to_its_type():
    """A constructor without a written type still refers to its inductive."""
    owner = row(parsed("Color", "inductive Color", "inductive", offset=0))
    red = row(parsed("Color.red", "| red", "constructor", offset=5, parent=0))
    hidden = row(parsed("Hidden", "inductive Hidden", "inductive", offset=9), False)
    only = row(parsed("Hidden.one", "| one", "constructor", offset=12, parent=2))
    found = dependencies([source_file("M", [owner, red, hidden, only])])
    assert found == {"Color": None, "Color.red": ["Color"], "Hidden.one": None}
