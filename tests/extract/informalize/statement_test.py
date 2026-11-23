"""Tests for finding the source that a declaration's description depends on."""

import pytest

from lean_explore.extract.informalize import statement
from lean_explore.extract.informalize.generation import (
    DeclarationData,
    build_prompt,
)
from lean_explore.extract.informalize.statement import statement_text

THEOREM = "/-- Doc. -/\n@[simp]\ntheorem foo (n : Nat) : n + 0 = n := by\n  simp\n"
STATEMENT = "/-- Doc. -/\n@[simp]\ntheorem foo (n : Nat) : n + 0 = n"


class FakeParser:
    """Stands in for the parser's bindings with fixed results."""

    def __init__(self, declarations: list[dict]):
        """Remember what ``parse`` returns."""
        self.declarations = declarations

    def parse(self, source: str) -> list[dict]:
        """Return the fixed declarations."""
        return self.declarations


def use_parser(monkeypatch, parser) -> None:
    """Make ``statement_text`` use the given parser, or none."""
    monkeypatch.setattr(statement, "_load_parser", lambda: parser)


def signature(end: int) -> tuple:
    """Build a signature span that ends at a byte offset."""
    return (0, 1, 0, end, 1, 0)


def test_theorem_is_cut_where_its_proof_starts(monkeypatch):
    """A theorem keeps its doc comment, attributes and statement."""
    end = len(STATEMENT.encode("utf-8"))
    parser = FakeParser([{"kind": "theorem", "signature": signature(end)}])
    use_parser(monkeypatch, parser)

    assert statement_text(THEOREM) == STATEMENT


def test_statement_end_is_a_byte_offset(monkeypatch):
    """Characters of several bytes before the end are kept whole."""
    source = "lemma t : α = α := rfl"
    end = len("lemma t : α = α".encode())
    use_parser(
        monkeypatch, FakeParser([{"kind": "lemma", "signature": signature(end)}])
    )

    assert statement_text(source) == "lemma t : α = α"


@pytest.mark.parametrize("kind", ["def", "structure", "instance", "abbrev"])
def test_other_declarations_keep_their_body(monkeypatch, kind):
    """The body of a definition is its meaning, so it stays."""
    use_parser(monkeypatch, FakeParser([{"kind": kind, "signature": signature(5)}]))

    assert statement_text("def f : Nat := 1") == "def f : Nat := 1"


def test_unrecognised_source_is_kept_whole(monkeypatch):
    """Source the parser finds no declaration in is returned unchanged."""
    use_parser(monkeypatch, FakeParser([]))

    assert statement_text("not a declaration") == "not a declaration"


def test_without_the_parser_the_whole_source_is_used(monkeypatch):
    """Without the bindings the proof is kept, as before."""
    use_parser(monkeypatch, None)

    assert statement_text(THEOREM) == THEOREM


def test_missing_bindings_are_reported_once(monkeypatch, caplog):
    """A failed import of the bindings yields no parser and one warning."""
    statement._load_parser.cache_clear()

    def fail(name):
        raise ImportError(name)

    monkeypatch.setattr(statement.importlib, "import_module", fail)
    assert statement._load_parser() is None
    assert statement._load_parser() is None
    assert caplog.text.count("lean_source_parser is not installed") == 1
    statement._load_parser.cache_clear()


def test_prompt_and_cache_use_the_statement(monkeypatch):
    """The model is shown the statement, and a proof edit keeps the cache key."""
    end = len(STATEMENT.encode("utf-8"))
    parser = FakeParser([{"kind": "theorem", "signature": signature(end)}])
    use_parser(monkeypatch, parser)
    first = DeclarationData(1, "foo", THEOREM, None, None, None)
    edited = DeclarationData(
        1, "foo", STATEMENT + " := by\n  omega\n", None, None, None
    )

    prompt = build_prompt(first, "{name}|{source_text}|{docstring}|{dependencies}", {})

    assert prompt == f"foo|{STATEMENT}|No docstring available|"
    assert first.statement == edited.statement == STATEMENT


def test_real_parser_cuts_theorems_only():
    """With the bindings, proofs are dropped and definitions are kept."""
    pytest.importorskip("lean_source_parser")
    statement._load_parser.cache_clear()

    assert statement_text(THEOREM) == STATEMENT
    assert statement_text("theorem t : ∀ n : Nat, n = n\n  | _ => rfl\n") == (
        "theorem t : ∀ n : Nat, n = n"
    )
    assert statement_text("def f (n : Nat) : Nat := n + 1") == (
        "def f (n : Nat) : Nat := n + 1"
    )
    assert statement_text("structure S where\n  x : Nat\n") == (
        "structure S where\n  x : Nat\n"
    )
