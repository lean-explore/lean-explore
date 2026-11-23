"""Tests for finding the declarations a signature refers to."""

from lean_explore.extract.source.names import NameTable, Use
from lean_explore.extract.source.references import (
    find_references,
    signature_binder_names,
)
from lean_explore.extract.source.scopes import Scope, parse_open, parse_variables

NAMES = {
    "Nat",
    "Set",
    "Good",
    "Flag",
    "Box",
    "X",
    "x",
    "b",
    "u",
    "helper",
    "Pkg.addOne",
    "Pkg.Point",
}


def references(
    signature: str,
    name: str = "t",
    scope: Scope | None = None,
    **options,
) -> list[str] | None:
    """Find the references of a signature among the test names."""
    use = Use(name, (scope or Scope()).opened)
    return find_references(
        signature, scope or Scope(), NameTable(NAMES), use, **options
    )


def variables(*commands: str, **options) -> Scope:
    """Build a scope from ``variable`` commands."""
    found = tuple(v for command in commands for v in parse_variables(command))
    return Scope(variables=found, **options)


def test_references_keep_first_appearance_order():
    """References are listed once, in the order they are written."""
    signature = "theorem Pkg.t (n : Nat) (p : Point) : addOne n = addOne (addOne n)"
    assert references(signature, "Pkg.t") == ["Nat", "Pkg.Point", "Pkg.addOne"]


def test_no_references_is_none():
    """A signature with no known names has no references."""
    assert references("theorem t : True") is None
    assert references("def t") is None


def test_the_declaration_itself_is_not_a_reference():
    """A declaration does not refer to itself."""
    assert references("theorem x : x = x", "x") is None


def test_head_binders_are_local():
    """Bracketed, untyped and bare binders are not references."""
    assert references("theorem t (x : Nat) {b} : x = b") == ["Nat"]
    assert references("def t x b : Nat") == ["Nat"]
    assert references("instance (priority := 100) t : Good x") == ["Good", "x"]


def test_words_before_the_name_are_not_binders():
    """A keyword before the declaration's name does not hide a constant."""
    assert references("theorem t : theorem") is None
    assert signature_binder_names("structure Box (X : Nat) extends Good X", "Box") == [
        "X"
    ]
    assert signature_binder_names("instance : Good x", "inst") == []


def test_ascription_in_the_type_is_not_a_binder():
    """``(x : T)`` after the colon ascribes a type to the constant ``x``."""
    assert references("theorem t : (x : Nat) = x") == ["x", "Nat"]


def test_quantifier_binders_are_scoped():
    """A bound name is local only inside its binder's scope."""
    assert references("theorem t : (∀ x : Nat, x = x) ∧ x = x") == ["Nat", "x"]
    assert references("theorem t : ∀ (a : Nat) (b : Nat), a = b") == ["Nat"]
    assert references("theorem t : ∃ x b, x = b") is None
    assert references("theorem t : ∀ x ∈ b, x = x") == ["b"]


def test_binder_marks_and_stop_words():
    """``Σ'``, ``∃!`` and ``∫ x in …`` bind the name after them."""
    assert references("def t : Σ' X : Nat, X = X") == ["Nat"]
    assert references("theorem t : ∃! x, x = b") == ["b"]
    assert references("theorem t : ∫ x in b, x = 0") == ["b"]


def test_a_binder_without_an_end_binds_nothing():
    """A quantifier with no body is read as ordinary names."""
    assert references("theorem t : ∀ x") == ["x"]


def test_function_and_pi_binders():
    """``fun`` and dependent-arrow binders are local to what follows."""
    assert references("def t : (fun x => x) = fun b ↦ b") is None
    assert references("def t : (X : Nat) → X = X") == ["Nat"]
    assert references("def t : (X : Nat) = X") == ["X", "Nat"]


def test_set_builder_binds_inside_the_braces():
    """The name of ``{x | …}`` and ``{x // …}`` is local to the braces."""
    assert references("def t : Set Nat → {x | x = b}") == ["Set", "Nat", "b"]
    assert references("def t : {x : Nat // x = b} → x") == ["Nat", "b", "x"]


def test_local_definitions_bind_the_rest():
    """``let`` and ``have`` introduce a local name."""
    assert references("def t : let x := b; x = x") == ["b"]


def test_named_arguments_and_field_access_are_not_references():
    """A label before ``:=`` and a field after a dot are not constants."""
    assert references("theorem t : helper (x := b) = (b).x") == ["helper", "b"]


def test_automatic_argument_tactics_are_not_read():
    """The tactic of ``(h : P := by …)`` is not part of the type."""
    assert references("def t (h : Good x := by exact helper b) : Nat") == [
        "Good",
        "x",
        "Nat",
    ]


def test_default_values_are_part_of_the_type():
    """The type before ``:=`` in a binder is read, and so is the default."""
    assert references("theorem t (n : Nat := b) : Good n") == ["Nat", "b", "Good"]


def test_syntax_declarations_and_values_are_not_types():
    """A macro has no type; a value after the type is not read."""
    assert references('macro "m" : term => helper', kind="macro") is None
    assert references("initialize t : Box ← helper", kind="initialize") == ["Box"]
    option = "register_option t : Nat := { defValue := b }"
    assert references(option, kind="register_option") == ["Nat"]


def test_universe_levels_are_not_constants():
    """What follows ``Type`` or ``Sort`` is a level, not the constant ``u``."""
    assert references("def t (α : Type u) (β : Sort (max u b)) : Nat.{u}") == ["Nat"]
    assert references("def t : Type → u") == ["u"]


def test_deriving_and_extends():
    """A deriving clause is not read; structure parents are references."""
    assert references("| k : Box deriving Good", "Box.k", kind="constructor") == ["Box"]
    assert references("structure t (x : Nat) extends Good x") == ["Nat", "Good"]


def test_parent_projection_is_all_type():
    """A parent projection's signature is its parent, with no head."""
    found = references("Good b", "t.toGood", kind="parent_projection")
    assert found == ["Good", "b"]


def test_further_bound_names():
    """Binders of the owning structure are local to its fields."""
    assert references("val : X", "S.val", bound=["X"]) is None
    assert references("val : X", "S.val") == ["X"]


def test_opened_names_resolve():
    """Open declarations of the scope are used."""
    scope = Scope(opened=tuple(parse_open("open Pkg", "")))
    assert references("theorem t : addOne = addOne", scope=scope) == ["Pkg.addOne"]


def test_used_variable_brings_its_type():
    """A section variable in the signature adds the references of its type."""
    scope = variables("variable (v : Box) (w : Good v)")
    assert references("theorem t : v = v", scope=scope) == ["Box"]
    assert references("theorem t : w = w", scope=scope) == ["Good", "Box"]
    assert references("theorem t : True", scope=scope) is None


def test_local_binder_shadows_a_variable():
    """A binder with a variable's name does not include the variable."""
    scope = variables("variable (v : Box)")
    assert references("theorem t (v : Nat) : v = v", scope=scope) == ["Nat"]


def test_instance_variables_follow_their_variables():
    """An instance variable is included once everything it mentions is."""
    scope = variables("variable {α : Type} [Good α] [Flag]")
    assert references("theorem t (a : α) : a = a", scope=scope) == ["Good", "Flag"]
    assert references("theorem t : True", scope=scope) == ["Flag"]
    assert references("structure t", scope=scope, kind="structure") is None


def test_omitted_and_included_variables():
    """``omit`` drops an instance variable; ``include`` forces a variable."""
    omitted = variables("variable {α : Type} [Good α]", omitted=frozenset({"[Good α]"}))
    assert references("theorem t (a : α) : a = a", scope=omitted) is None
    included = variables("variable (v : Box)", included=frozenset({"v"}))
    assert references("theorem t : True", scope=included) == ["Box"]


def test_definition_bodies_include_their_variables():
    """A definition includes variables its body uses; a theorem does not."""
    scope = variables("variable (v : Box)")
    body = "def t : Nat := v.val"
    assert references("def t : Nat", scope=scope, kind="def", body=body) == [
        "Nat",
        "Box",
    ]
    assert references("theorem t : Nat", scope=scope, kind="theorem", body=body) == [
        "Nat"
    ]
