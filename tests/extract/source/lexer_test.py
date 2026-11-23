"""Tests for splitting Lean source into identifiers and symbols."""

import time

from lean_explore.extract.source.lexer import (
    Group,
    Token,
    nest,
    normalise_name,
    tokenize,
)


def texts(source: str) -> list[str]:
    """Return the token texts of a piece of source."""
    return [token.text for token in tokenize(source)]


def test_identifiers_keep_dots_primes_and_unicode():
    """Dotted names, primes, subscripts and Greek letters are identifiers."""
    assert texts("Nat.succ x' α₁ ℘ foo? bar!") == [
        "Nat.succ",
        "x'",
        "α₁",
        "℘",
        "foo?",
        "bar!",
    ]


def test_lambda_and_big_operators_are_symbols():
    """``λ``, ``Π`` and ``Σ`` are not identifier characters."""
    tokens = tokenize("λ x, Σ y, Π z")
    assert [token.text for token in tokens if not token.is_identifier] == [
        "λ",
        ",",
        "Σ",
        ",",
        "Π",
    ]


def test_guillemets_are_one_component():
    """An escaped component stays whole; a needless escape is removed."""
    assert texts("A.«two words».b «exists»") == ["A.«two words».b", "exists"]
    assert normalise_name("Nat.succ") == "Nat.succ"


def test_comments_are_skipped_including_nested_ones():
    """Line comments and nested block comments leave no tokens."""
    assert texts("a /- b /- c -/ d -/ e -- f\ng") == ["a", "e", "g"]
    assert texts("a /- never closed") == ["a"]


def test_literals_are_skipped():
    """Strings, characters, numbers and name literals leave no tokens."""
    source = "a \"b c\" 'd' '\\n' 12 0xFF 1.5e3 `Nat.succ ``Foo e"
    assert texts(source) == ["a", "e"]
    assert texts('a "unterminated') == ["a"]


def test_raw_strings_are_skipped():
    """A raw string ends at its own closing quote and hashes."""
    assert texts('r#"say "A""# b r"c" d') == ["b", "d"]
    assert texts('r#"never closed') == []
    assert texts("r x") == ["r", "x"]


def test_interpolated_strings_keep_their_terms():
    """The terms of an interpolated string are read; its text is not."""
    assert texts('s!"text {marker} and {f {x}}" tail') == [
        "s!",
        "marker",
        "f",
        "{",
        "x",
        "}",
        "tail",
    ]


def test_identifier_after_a_dot_is_marked():
    """Field access on a term and leading-dot names are marked."""
    tokens = [token for token in tokenize("(f x).symm = .zero") if token.is_identifier]
    assert [(token.text, token.after_dot) for token in tokens] == [
        ("f", False),
        ("x", False),
        ("symm", True),
        ("zero", True),
    ]


def test_two_character_symbols():
    """Arrows, definitions and universe brackets are single tokens."""
    assert texts("a := b => c -> d .{u}") == [
        "a",
        ":=",
        "b",
        "=>",
        "c",
        "->",
        "d",
        ".{",
        "u",
        "}",
    ]


def test_nest_groups_by_brackets():
    """Brackets become groups and tolerate being unbalanced."""
    (first, group, last) = nest(tokenize("a (b {c}) d"))
    assert isinstance(group, Group) and group.opening == "("
    assert first == Token("a", True) and last == Token("d", True)
    inner = group.items[1]
    assert isinstance(inner, Group) and inner.opening == "{"
    assert nest(tokenize("a ) b ( c"))[0] == Token("a", True)
    assert len(nest(tokenize("a ) b ( c"))) == 3


def test_long_whitespace_is_linear():
    """A long run of spaces does not slow the lexer down."""
    start = time.perf_counter()
    tokenize("def t : hfun " + " " * 200_000 + "10")
    assert time.perf_counter() - start < 2
