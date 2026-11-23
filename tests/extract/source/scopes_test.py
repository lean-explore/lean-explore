"""Tests for tracking what is in scope at each command of a file."""

import pytest

from lean_explore.extract.source.lexer import nest, tokenize
from lean_explore.extract.source.scopes import (
    FileScopes,
    OpenDeclaration,
    Scope,
    binder_names,
    colon_index,
    group_binder_names,
    instance_key,
    omitted_keys,
    parse_open,
    parse_variables,
)

KINDS = {
    "theorem": "declaration",
    "def": "declaration",
    "export": "other",
    "include": "other",
    "omit": "other",
}


def scopes_of(source: str) -> FileScopes:
    """Build the scopes of a file with one command per line."""
    commands = []
    offset = 0
    namespaces: list[str] = []
    for line in source.splitlines(keepends=True):
        word = line.split()[0]
        commands.append(
            {
                "kind": KINDS.get(word, word),
                "range": (offset, 0, 0, offset + len(line.rstrip("\n").encode()), 0, 0),
                "namespace": ".".join(namespaces),
            }
        )
        if word == "namespace":
            namespaces.extend(line.split()[1].split("."))
        elif word == "end" and len(line.split()) > 1:
            del namespaces[-len(line.split()[1].split(".")) :]
        offset += len(line.encode())
    return FileScopes(source, commands)


def scope_at(source: str, marker: str) -> Scope:
    """Return the scope at the line that starts with ``marker``."""
    return scopes_of(source).at(source.encode().index(marker.encode()))


def opened(scope: Scope) -> list[str]:
    """Return the written namespaces a scope has open."""
    return [declaration.candidates[-1] for declaration in scope.opened]


def test_binder_names_cover_the_binder_forms():
    """Typed, untyped, strict and pattern binders bind; instances do not."""
    items = nest(tokenize("(a b : T) {c} ⦃d : T⦄ [inst : C a] [C b] (⟨p, q⟩ : P) e"))
    assert binder_names(items) == ["a", "b", "c", "d", "inst", "p", "q", "e"]


def test_group_binder_names_of_a_pattern():
    """An anonymous-constructor pattern binds every name in it."""
    (group,) = nest(tokenize("⟨a, ⟨b, c⟩⟩"))
    assert group_binder_names(group) == ["a", "b", "c"]


def test_colon_index_ignores_definitions():
    """``:=`` is not a type colon."""
    assert colon_index(nest(tokenize("x := 1"))) is None
    assert colon_index(nest(tokenize("x : T"))) == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("open Set Filter", [("Set", None), ("Filter", None)]),
        ("open Set in", [("Set", None)]),
        ("open A (x y)", [("A", frozenset({"x", "y"}))]),
        ("open scoped Topology", []),
        ("open", []),
    ],
)
def test_parse_open_namespaces(text, expected):
    """Each opened namespace is returned with its selection."""
    found = parse_open(text, "")
    assert [(item.candidates[-1], item.only) for item in found] == expected


def test_parse_open_hiding_and_renaming():
    """``hiding`` and ``renaming`` are kept with their single namespace."""
    (hiding,) = parse_open("open A hiding x y", "")
    assert hiding.hidden == frozenset({"x", "y"})
    (renaming,) = parse_open("open A renaming x → y, p → q", "")
    assert renaming.renamed == (("y", "x"), ("q", "p"))


def test_parse_open_candidates_follow_the_current_namespace():
    """A namespace written inside ``N.M`` can be relative to either."""
    (declaration,) = parse_open("open A", "N.M")
    assert declaration.candidates == ("N.M.A", "N.A", "A")
    (rooted,) = parse_open("open _root_.A", "N.M")
    assert rooted.candidates == ("A",)


def test_parse_variables():
    """Typed, instance and typeless binders are told apart."""
    typed, named, anonymous, untyped = parse_variables(
        "variable (a b : T) [inst : C a] [C b] {c} extra"
    )
    assert typed.names == ("a", "b") and not typed.is_instance
    assert named.names == ("inst",) and named.is_instance
    assert anonymous.names == () and anonymous.is_instance
    assert untyped.names == ("c",) and untyped.type_items == ()


def test_omitted_keys_and_instance_key():
    """Omitted variables are names; omitted instances are keyed by content."""
    assert omitted_keys("omit x [Good a] in") == ["x", "[Good a]"]
    assert instance_key(nest(tokenize("Good a"))) == "[Good a]"


def test_opens_and_variables_end_with_their_section():
    """What a section opens and binds is gone after its ``end``."""
    source = (
        "open Set\n"
        "section\n"
        "open Other\n"
        "variable (x : Nat)\n"
        "theorem first : True\n"
        "end\n"
        "theorem second : True\n"
    )
    first = scope_at(source, "theorem first")
    second = scope_at(source, "theorem second")
    assert opened(first) == ["Set", "Other"]
    assert [variable.names for variable in first.variables] == [("x",)]
    assert opened(second) == ["Set"] and second.variables == ()


def test_end_closes_one_frame_per_component():
    """``end A.B`` closes both namespaces; ``end B`` only one."""
    source = (
        "namespace A.B\n"
        "open Inner\n"
        "end B\n"
        "theorem middle : True\n"
        "end A\n"
        "namespace C.D\n"
        "open Other\n"
        "end C.D\n"
        "theorem last : True\n"
        "end\n"
    )
    assert opened(scope_at(source, "theorem middle")) == []
    assert scope_at(source, "theorem middle").namespace == "A"
    assert opened(scope_at(source, "theorem last")) == []


def test_mutual_end_keeps_the_enclosing_scope():
    """The ``end`` of a mutual block does not close the namespace."""
    source = (
        "namespace P\nopen Nat\nmutual\ndef a : Nat\nend\ntheorem t : True\nend P\n"
    )
    inside = scope_at(source, "def a")
    assert inside.mutual_start == source.index("mutual")
    after = scope_at(source, "theorem t")
    assert opened(after) == ["Nat"] and after.mutual_start is None


def test_prefix_commands_reach_the_next_declaration_only():
    """``open … in`` and ``variable … in`` chains apply to one declaration."""
    source = (
        "open A in\n"
        "set_option pp.all true in\n"
        "variable (x : Nat) in\n"
        "theorem first : True\n"
        "theorem second : True\n"
    )
    first = scope_at(source, "theorem first")
    second = scope_at(source, "theorem second")
    assert opened(first) == ["A"] and first.variables[0].names == ("x",)
    assert opened(second) == [] and second.variables == ()


def test_typeless_variable_keeps_the_earlier_type():
    """``variable {x}`` changes how ``x`` binds but not its type."""
    source = "variable (x : Nat)\nvariable {x}\nvariable {y}\ntheorem t : True\n"
    variables = scope_at(source, "theorem t").variables
    assert [variable.names for variable in variables] == [("x",), ("y",)]
    assert variables[0].type_items and not variables[1].type_items


def test_include_and_omit_are_recorded():
    """Included and omitted variables are part of the scope."""
    source = "variable (x : Nat)\ninclude x\nomit [Good a] in\ntheorem t : True\n"
    scope = scope_at(source, "theorem t")
    assert scope.included == frozenset({"x"})
    assert scope.omitted == frozenset({"[Good a]"})


def test_export_records_aliases():
    """``export A (x)`` makes ``x`` available under the current namespace."""
    source = "namespace N\nexport A (x y)\nend N\n"
    aliases = scopes_of(source).aliases
    assert set(aliases) == {"N.x", "N.y"}
    assert aliases["N.x"] == OpenDeclaration(("N.A", "A"), frozenset({"x", "y"}))


def test_scope_before_the_first_command_and_stray_end():
    """Offsets before any command and an unmatched ``end`` do not fail."""
    assert FileScopes("", []).at(0) == Scope()
    assert scope_at("end\ntheorem t : True\n", "theorem t") == Scope()
