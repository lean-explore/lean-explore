"""Find the declarations a statement refers to without elaborating it.

The doc-gen4 path stores, as a declaration's dependencies, the constants that
appear in its elaborated type. Source extraction has no elaborated type, so
this module reads the identifiers written in the signature and resolves them
the way Lean resolves a name: against the namespaces of the declaration, the
root namespace and the namespaces opened at that point of the file.

The result is an approximation. References made through notation (``ℝ``,
``+``, ``∈``), through field access on a term (``s.card``) and through
elaboration (instances, coercions) are not found, and a local name that is
not bound in the signature can be mistaken for a constant of the same name.
"""

import bisect
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from typing import Any

_IDENTIFIER = re.compile(r"(?<![\w.'])[^\W\d][\w'!?]*(?:\.[^\W\d][\w'!?]*)*")
_COMMENT_OR_STRING = re.compile(r'/-.*?-/|--[^\n]*|"(?:\\.|[^"\\])*"', flags=re.DOTALL)
_BRACKET_BINDER = re.compile(r"[({\[⦃]([^(){}\[\]⦃⦄:]*?):(?!=)")
_KEYWORD_BINDER = re.compile(
    r"(?:∀|∃|∃!|λ|fun|Σ|Π|∑|∏|⋃|⋂|⨆|⨅)\s+([^,:=⇒]*?)\s*(?:,|:|=>|↦)"
)
_OPEN_OPTIONS = re.compile(r"\bhiding\b|\brenaming\b|\(")
_SCOPE_OPENERS = ("namespace", "section")
_OPENING_BRACKETS = "([{⦃"
_CLOSING_BRACKETS = ")]}⦄"


@dataclass
class _Frame:
    """Names that one ``namespace`` or ``section`` adds to the scope."""

    opened: list[str] = field(default_factory=list)
    bound: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Scope:
    """What is in scope at one point of a file."""

    opened: tuple[str, ...] = ()
    """Namespaces opened with ``open``, innermost last."""

    bound: frozenset[str] = frozenset()
    """Names bound by ``variable`` commands."""


def strip_comments_and_strings(text: str) -> str:
    """Blank out comments and string literals so their words are not read."""
    return _COMMENT_OR_STRING.sub(" ", text)


def _names_in(pattern: re.Pattern[str], text: str) -> set[str]:
    """Collect the unqualified names in the first group of each match."""
    return {
        name
        for match in pattern.finditer(text)
        for name in _IDENTIFIER.findall(match.group(1))
        if "." not in name
    }


def bound_names(text: str) -> set[str]:
    """Collect the names that binders in a piece of source introduce.

    Args:
        text: A run of binders, such as the body of a ``variable`` command.

    Returns:
        Names bound in ``(a b : T)``-style binders and by ``∀``, ``∃``,
        ``fun`` and big-operator binders.
    """
    return _names_in(_BRACKET_BINDER, text) | _names_in(_KEYWORD_BINDER, text)


def _before_type(signature: str) -> str:
    """Return the part of a signature before the colon that starts its type."""
    depth = 0
    for index, character in enumerate(signature):
        if character in _OPENING_BRACKETS:
            depth += 1
        elif character in _CLOSING_BRACKETS:
            depth -= 1
        elif (
            character == ":" and depth == 0 and signature[index + 1 : index + 2] != "="
        ):
            return signature[:index]
    return signature


def signature_bound_names(signature: str) -> set[str]:
    """Collect the names a signature binds.

    Bracketed binders are read only before the type, because ``(x : T)``
    inside the type is usually a type ascription on a constant.

    Args:
        signature: A declaration's signature, without comments and strings.

    Returns:
        Names of the signature's binders and of quantifier-style binders
        anywhere in it.
    """
    return _names_in(_BRACKET_BINDER, _before_type(signature)) | _names_in(
        _KEYWORD_BINDER, signature
    )


def opened_namespaces(open_command: str) -> list[str]:
    """Return the namespaces an ``open`` command opens.

    Args:
        open_command: The command as written, such as ``open Set Filter in``.

    Returns:
        The namespace names before any ``hiding``, ``renaming`` or selection.
    """
    text = _OPEN_OPTIONS.split(strip_comments_and_strings(open_command))[0]
    return [
        name
        for name in _IDENTIFIER.findall(text)
        if name not in ("open", "scoped", "in")
    ]


class FileScopes:
    """The scope in effect at every command of one source file.

    Args:
        source: The file's text.
        commands: The file's commands as ``lean_source_parser.commands``
            reports them, in file order.
    """

    def __init__(self, source: str, commands: Iterable[dict[str, Any]]):
        """Walk the commands once and record the scope at each of them."""
        source_bytes = source.encode("utf-8")
        self._starts: list[int] = []
        self._scopes: list[Scope] = []
        frames = [_Frame()]
        next_only: list[str] = []
        for command in commands:
            start, end = command["range"][0], command["range"][3]
            self._starts.append(start)
            self._scopes.append(self._snapshot(frames, next_only))
            kind = command["kind"]
            next_only = []
            if kind in _SCOPE_OPENERS:
                frames.append(_Frame())
            elif kind == "end" and len(frames) > 1:
                frames.pop()
            elif kind in ("open", "variable"):
                text = source_bytes[start:end].decode("utf-8", "replace")
                next_only = self._apply(kind, text, frames[-1])

    @staticmethod
    def _apply(kind: str, text: str, frame: _Frame) -> list[str]:
        """Add a command's names to a frame; return those for one command only."""
        if kind == "variable":
            frame.bound.extend(bound_names(strip_comments_and_strings(text)))
            return []
        namespaces = opened_namespaces(text)
        if text.rstrip().endswith(" in"):
            return namespaces
        frame.opened.extend(namespaces)
        return []

    @staticmethod
    def _snapshot(frames: list[_Frame], next_only: list[str]) -> Scope:
        """Freeze the current frames into a scope."""
        opened = [name for frame in frames for name in frame.opened]
        bound = {name for frame in frames for name in frame.bound}
        return Scope(tuple(opened + next_only), frozenset(bound))

    def at(self, byte_offset: int) -> Scope:
        """Return the scope of the command that contains a byte offset."""
        index = bisect.bisect_right(self._starts, byte_offset) - 1
        return self._scopes[index] if index >= 0 else Scope()


def _candidates(identifier: str, name: str, opened: Iterable[str]) -> Iterable[str]:
    """Yield the full names an identifier can stand for, most specific first."""
    parts = name.split(".")
    namespaces = [".".join(parts[:size]) for size in range(len(parts) - 1, 0, -1)]
    for namespace in namespaces:
        yield f"{namespace}.{identifier}"
    yield identifier
    for opened_namespace in reversed(list(opened)):
        yield f"{opened_namespace}.{identifier}"
        for namespace in namespaces:
            yield f"{namespace}.{opened_namespace}.{identifier}"


def resolve(
    identifier: str, name: str, opened: Iterable[str], known_names: Collection[str]
) -> str | None:
    """Resolve an identifier written inside a declaration to a known name.

    A dotted identifier whose full form is unknown is retried without its
    trailing components, so ``Foo.bar.baz`` resolves to ``Foo.bar`` when
    ``baz`` is a field access.

    Args:
        identifier: The identifier as written.
        name: Fully qualified name of the declaration it appears in.
        opened: Namespaces opened at the declaration.
        known_names: Every declaration name that can be referred to.

    Returns:
        The full name of the constant, or None if nothing matches.
    """
    parts = identifier.split(".")
    for size in range(len(parts), 0, -1):
        for candidate in _candidates(".".join(parts[:size]), name, opened):
            if candidate in known_names:
                return candidate
    return None


def find_references(
    name: str, signature: str, scope: Scope, known_names: Collection[str]
) -> list[str] | None:
    """List the known declarations that a signature refers to.

    Args:
        name: Fully qualified name of the declaration.
        signature: The declaration's signature as written.
        scope: The scope in effect at the declaration.
        known_names: Every declaration name that can be referred to.

    Returns:
        The referenced names in order of first appearance, without the
        declaration itself, or None if there are none.
    """
    text = strip_comments_and_strings(signature)
    local_names = scope.bound | signature_bound_names(text)
    references: dict[str, None] = {}
    for identifier in _IDENTIFIER.findall(text):
        if identifier.split(".")[0] in local_names:
            continue
        resolved = resolve(identifier, name, scope.opened, known_names)
        if resolved is not None and resolved != name:
            references[resolved] = None
    return list(references) or None
