"""Split Lean source text into identifiers and symbols.

This is a small lexer for the parts of Lean's token grammar that matter when
reading the names written in a signature. Comments, string and character
literals, numbers and name literals are skipped. Identifiers follow the
character classes of ``Lean/Parser/Basic.lean`` (``isIdFirst``, ``isIdRest``),
including ``«»`` escapes and dotted components.
"""

import re
from dataclasses import dataclass, field

_LETTER_LIKE = (
    "A-Za-z_"
    "À-ÖØ-öø-ÿ"  # Latin-1 letters
    "α-κμ-ω"  # Greek lower case, without lambda
    "Α-ΟΡ΢Τ-Ω"  # Greek upper case, without Pi, Sigma
    "ϊ-ϻἀ-῾"  # Coptic and extended Greek
    "℀-⅏"  # letter-like symbols
    "\U0001d49c-\U0001d59f"  # mathematical script and fraktur
)
_SUBSCRIPTS = "₀-₉ₐ-ₜᵢ-ᵪⱼ"
_COMPONENT = f"(?:[{_LETTER_LIKE}][{_LETTER_LIKE}0-9'!?{_SUBSCRIPTS}]*|«[^»\n]*»)"
_IDENTIFIER = re.compile(f"{_COMPONENT}(?:\\.{_COMPONENT})*")
_PLAIN_COMPONENT = re.compile(f"[{_LETTER_LIKE}][{_LETTER_LIKE}0-9'!?{_SUBSCRIPTS}]*")
_NUMBER = re.compile(
    r"0[xXbBoO][0-9a-fA-F_]+|[0-9][0-9_]*(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
)
_STRING = re.compile(r'"(?:\\.|[^"\\])*"?', flags=re.DOTALL)
_RAW_STRING = re.compile(r'r(#*)"')
_CHARACTER = re.compile(r"'(?:\\(?:x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)|[^'\\\n])'")
_NAME_LITERAL = re.compile(f"`+{_COMPONENT}(?:\\.{_COMPONENT})*")
_INTERPOLATION_PREFIXES = ("s!", "m!", "f!", "throwError", "panic!", "dbgTrace")

OPENING = {"(": ")", "{": "}", ".{": "}", "[": "]", "⦃": "⦄", "⟨": "⟩"}
"""Opening brackets and the brackets that close them."""

CLOSING = frozenset(OPENING.values())
"""Closing brackets."""

_TWO_CHARACTER_SYMBOLS = ("=>", "->", ":=", "//", "|>", "<|", ".{")


@dataclass(frozen=True)
class Token:
    """One identifier or symbol of a piece of source."""

    text: str
    """The identifier with ``«»`` escapes normalised, or the symbol."""

    is_identifier: bool = False
    """Whether the token is an identifier rather than a symbol."""

    after_dot: bool = False
    """Whether a ``.`` directly precedes the identifier, as in ``(f x).symm``."""


@dataclass
class Group:
    """A bracketed run of tokens."""

    opening: str
    """The opening bracket."""

    items: list["Token | Group"] = field(default_factory=list)
    """The tokens and nested groups between the brackets."""


def normalise_name(identifier: str) -> str:
    """Write an identifier the way Lean prints it.

    Args:
        identifier: An identifier as written, possibly with ``«»`` escapes.

    Returns:
        The identifier with escapes removed from components that do not need
        them, such as ``«exists»``.
    """
    if "«" not in identifier:
        return identifier
    parts = re.findall(_COMPONENT, identifier)
    return ".".join(
        part[1:-1] if _PLAIN_COMPONENT.fullmatch(part[1:-1]) else part for part in parts
    )


def _skip_block_comment(text: str, position: int) -> int:
    """Return the position after the block comment that starts at ``position``."""
    depth = 0
    while position < len(text):
        if text.startswith("/-", position):
            depth += 1
            position += 2
        elif text.startswith("-/", position):
            depth -= 1
            position += 2
            if depth == 0:
                return position
        else:
            position += 1
    return position


def _skip_raw_string(text: str, position: int, hashes: str) -> int:
    """Return the position after the raw string whose quote is at ``position``."""
    end = text.find('"' + hashes, position + 1)
    return len(text) if end < 0 else end + 1 + len(hashes)


def _interpolated(literal: str) -> list[str]:
    """Return the ``{...}`` terms inside an interpolated string literal."""
    terms = []
    depth = 0
    start = 0
    for index, character in enumerate(literal):
        if character == "{":
            if depth == 0:
                start = index + 1
            depth += 1
        elif character == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                terms.append(literal[start:index])
    return terms


class _Lexer:
    """State of one pass over a piece of source."""

    def __init__(self, text: str):
        """Start at the beginning of ``text``."""
        self.text = text
        self.position = 0
        self.tokens: list[Token] = []

    def _skip_literal(self) -> bool:
        """Skip a comment or literal at the current position, if there is one."""
        text, position = self.text, self.position
        if text.startswith("--", position):
            end = text.find("\n", position)
            self.position = len(text) if end < 0 else end
        elif text.startswith("/-", position):
            self.position = _skip_block_comment(text, position)
        elif text[position] == '"':
            self._string(_STRING.match(text, position))
        elif text[position] == "'" and (found := _CHARACTER.match(text, position)):
            self.position = found.end()
        elif text[position] == "`" and (found := _NAME_LITERAL.match(text, position)):
            self.position = found.end()
        elif found := _NUMBER.match(text, position):
            self.position = found.end()
        else:
            return False
        return True

    def _string(self, found: re.Match[str] | None) -> None:
        """Skip a string literal, reading the terms of an interpolated one."""
        assert found is not None
        self.position = found.end()
        previous = self.tokens[-1].text if self.tokens else ""
        if previous in _INTERPOLATION_PREFIXES:
            for term in _interpolated(found.group()):
                self.tokens.extend(tokenize(term))

    def _identifier(self, found: re.Match[str]) -> None:
        """Add the identifier that ``found`` matched."""
        start = _RAW_STRING.match(self.text, found.start())
        if found.group() == "r" and start:
            self.position = _skip_raw_string(self.text, start.end() - 1, start.group(1))
            return
        after_dot = found.start() > 0 and self.text[found.start() - 1] == "."
        self.tokens.append(Token(normalise_name(found.group()), True, after_dot))
        self.position = found.end()

    def _symbol(self) -> None:
        """Add the symbol at the current position."""
        for symbol in _TWO_CHARACTER_SYMBOLS:
            if self.text.startswith(symbol, self.position):
                self.tokens.append(Token(symbol))
                self.position += len(symbol)
                return
        self.tokens.append(Token(self.text[self.position]))
        self.position += 1

    def run(self) -> list[Token]:
        """Read every token of the text."""
        while self.position < len(self.text):
            if self.text[self.position].isspace():
                self.position += 1
            elif self._skip_literal():
                continue
            elif found := _IDENTIFIER.match(self.text, self.position):
                self._identifier(found)
            else:
                self._symbol()
        return self.tokens


def tokenize(text: str) -> list[Token]:
    """Read the identifiers and symbols of a piece of Lean source.

    Args:
        text: Source text, such as a signature or a command.

    Returns:
        The tokens in order, without comments, literals and numbers.
    """
    return _Lexer(text).run()


def nest(tokens: list[Token]) -> list[Token | Group]:
    """Arrange tokens into a tree by their brackets.

    Unbalanced brackets do not fail: a stray closing bracket is dropped and
    an unclosed group ends at the end of the tokens.

    Args:
        tokens: Tokens of one piece of source.

    Returns:
        The top-level tokens and bracket groups.
    """
    root: list[Token | Group] = []
    stack = [root]
    for token in tokens:
        if not token.is_identifier and token.text in OPENING:
            group = Group(token.text)
            stack[-1].append(group)
            stack.append(group.items)
        elif not token.is_identifier and token.text in CLOSING:
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1].append(token)
    return root
