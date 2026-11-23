"""Track what is in scope at each command of a Lean source file.

The source parser reports a file's commands in order. This module replays the
ones that change name resolution (``namespace``, ``section``, ``mutual``,
``end``, ``open``, ``export``, ``variable`` and ``universe``) and records the
scope in effect at every command, so a declaration can be resolved with the
namespaces that are open where it is written.
"""

import bisect
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from lean_explore.extract.source.lexer import Group, Token, nest, tokenize

_BINDER_BRACKETS = ("(", "{", "[", "⦃")
_FRAME_OPENERS = ("namespace", "section")
_SCOPE_KINDS = frozenset(
    {
        "open",
        "variable",
        "universe",
        "namespace",
        "section",
        "mutual",
        "end",
        "set_option",
        "attribute",
        "other",
    }
)
"""Command kinds that can change the scope or prefix the next command."""


@dataclass(frozen=True)
class OpenDeclaration:
    """One namespace made available by ``open`` or ``export``."""

    candidates: tuple[str, ...]
    """Full names the written namespace can stand for, most specific first."""

    only: frozenset[str] | None = None
    """Names selected with ``open A (x y)``; None opens every name."""

    hidden: frozenset[str] = frozenset()
    """Names left out with ``open A hiding x``."""

    renamed: tuple[tuple[str, str], ...] = ()
    """Pairs ``(new, old)`` from ``open A renaming old → new``."""


@dataclass(frozen=True)
class Variable:
    """One binder of a ``variable`` command."""

    names: tuple[str, ...]
    """The names it binds; empty for an anonymous instance ``[Foo a]``."""

    type_items: tuple[Token | Group, ...] = ()
    """The tokens of its type."""

    is_instance: bool = False
    """Whether it is an instance binder."""


@dataclass(frozen=True)
class Scope:
    """What is in scope at one command of a file."""

    namespace: str = ""
    """The namespace the command is in."""

    opened: tuple[OpenDeclaration, ...] = ()
    """Open declarations in effect, outermost first."""

    variables: tuple[Variable, ...] = ()
    """Binders of the ``variable`` commands in effect, in order."""

    included: frozenset[str] = frozenset()
    """Variables that ``include`` adds to every declaration."""

    omitted: frozenset[str] = frozenset()
    """Variables and instance types that ``omit`` leaves out."""

    mutual_start: int | None = None
    """Byte offset of the enclosing ``mutual`` block, if there is one."""


@dataclass
class _Frame:
    """What one ``namespace``, ``section`` or ``mutual`` adds to the scope."""

    kind: str = "file"
    start: int = 0
    opened: list[OpenDeclaration] = field(default_factory=list)
    variables: list[Variable] = field(default_factory=list)
    included: list[str] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)


def is_plain(item: Token | Group) -> bool:
    """Tell whether an item is an unqualified identifier that can be bound."""
    return (
        isinstance(item, Token)
        and item.is_identifier
        and not item.after_dot
        and "." not in item.text
    )


def _plain_names(items: Iterable[Token | Group]) -> list[str]:
    """Collect the unqualified identifiers in items, including nested ones."""
    names: list[str] = []
    for item in items:
        if isinstance(item, Group):
            names.extend(_plain_names(item.items))
        elif is_plain(item):
            names.append(item.text)
    return names


def colon_index(items: list[Token | Group]) -> int | None:
    """Return the index of the first ``:`` among items, not inside brackets."""
    for index, item in enumerate(items):
        if isinstance(item, Token) and not item.is_identifier and item.text == ":":
            return index
    return None


def group_binder_names(group: Group) -> list[str]:
    """Return the names a bracketed binder such as ``(a b : T)`` introduces.

    Args:
        group: A bracket group in binder position.

    Returns:
        The names before the colon, the names of an untyped ``{a b}`` or
        ``(a b)`` binder, or nothing for an instance binder ``[Foo a]``.
    """
    if group.opening not in _BINDER_BRACKETS:
        return _plain_names(group.items)
    colon = colon_index(group.items)
    if colon is not None:
        return _plain_names(group.items[:colon])
    return [] if group.opening == "[" else _plain_names(group.items)


def binder_names(items: Iterable[Token | Group]) -> list[str]:
    """Return the names a run of binders introduces.

    Args:
        items: Binders as written after ``variable``, ``∀`` or ``fun``:
            bracketed binders, patterns and bare names.

    Returns:
        The bound names in order.
    """
    names: list[str] = []
    for item in items:
        if isinstance(item, Group):
            names.extend(group_binder_names(item))
        elif is_plain(item):
            names.append(item.text)
    return names


def instance_key(items: Iterable[Token | Group]) -> str:
    """Return a key that identifies an instance binder by the names in it."""
    return "[" + " ".join(_plain_names(items)) + "]"


def omitted_keys(text: str) -> list[str]:
    """Return what an ``omit`` command leaves out.

    Args:
        text: The command as written, such as ``omit x [Foo a] in``.

    Returns:
        The omitted variable names and the keys of omitted instance binders.
    """
    keys = []
    for item in nest(tokenize(text))[1:]:
        if isinstance(item, Group):
            keys.append(instance_key(item.items))
        elif is_plain(item) and item.text != "in":
            keys.append(item.text)
    return keys


def parse_variables(text: str) -> list[Variable]:
    """Read the binders of a ``variable`` command.

    Args:
        text: The command as written.

    Returns:
        One variable per bracketed binder. A binder without a type, which
        changes how an earlier variable is bound, has no type items.
    """
    variables = []
    for item in nest(tokenize(text))[1:]:
        if not isinstance(item, Group):
            continue
        colon = colon_index(item.items)
        is_instance = item.opening == "["
        if colon is not None:
            names = tuple(_plain_names(item.items[:colon]))
            variables.append(
                Variable(names, tuple(item.items[colon + 1 :]), is_instance)
            )
        elif is_instance:
            variables.append(Variable((), tuple(item.items), True))
        else:
            variables.append(Variable(tuple(_plain_names(item.items))))
    return variables


def _namespace_candidates(written: str, current: str) -> tuple[str, ...]:
    """List the full names a namespace written inside ``current`` can mean."""
    if written.startswith("_root_."):
        return (written.removeprefix("_root_."),)
    parts = current.split(".") if current else []
    relative = [
        ".".join(parts[:size]) + "." + written for size in range(len(parts), 0, -1)
    ]
    return (*relative, written)


def _identifier_texts(items: Iterable[Token | Group]) -> list[str]:
    """Return the identifiers among items, not looking inside brackets."""
    return [
        item.text for item in items if isinstance(item, Token) and item.is_identifier
    ]


def parse_open(text: str, current: str) -> list[OpenDeclaration]:
    """Read the open declarations of an ``open`` or ``export`` command.

    Args:
        text: The command as written, such as ``open Set Filter in``.
        current: The namespace the command is in.

    Returns:
        One open declaration per namespace. ``open scoped`` opens no names,
        so it returns nothing.
    """
    items = nest(tokenize(text))[1:]
    words = _identifier_texts(items)
    if not words or words[0] == "scoped":
        return []
    if words[-1] == "in":
        items = items[: max(i for i, item in enumerate(items) if _is_word(item, "in"))]
        words = words[:-1]
    candidates = _namespace_candidates(words[0], current)
    selection = next((item for item in items if isinstance(item, Group)), None)
    if selection is not None:
        return [OpenDeclaration(candidates, only=frozenset(_plain_names([selection])))]
    if "hiding" in words:
        return [OpenDeclaration(candidates, hidden=frozenset(words[2:]))]
    if "renaming" in words:
        pairs = zip(words[3::2], words[2::2], strict=False)
        return [OpenDeclaration(candidates, renamed=tuple(pairs))]
    return [OpenDeclaration(_namespace_candidates(word, current)) for word in words]


def _is_word(item: Token | Group, word: str) -> bool:
    """Tell whether an item is the identifier ``word``."""
    return isinstance(item, Token) and item.is_identifier and item.text == word


def _ends_with_in(text: str) -> bool:
    """Tell whether a command is a prefix of the next one, as ``open A in``."""
    tokens = tokenize(text)
    return bool(tokens) and tokens[-1].is_identifier and tokens[-1].text == "in"


class FileScopes:
    """The scope in effect at every command of one source file.

    Args:
        source: The file's text.
        commands: The file's commands as ``lean_source_parser.commands``
            reports them, in file order.
    """

    def __init__(self, source: str, commands: Iterable[dict[str, Any]]):
        """Replay the commands once and record the scope at each of them."""
        source_bytes = source.encode("utf-8")
        self._starts: list[int] = []
        self._scopes: list[Scope] = []
        self._frames = [_Frame()]
        self._pending = _Frame()
        self.aliases: dict[str, OpenDeclaration] = {}
        """Names that ``export`` makes available, by their full alias name."""
        for command in commands:
            start, end = command["range"][0], command["range"][3]
            namespace = command.get("namespace") or ""
            self._starts.append(start)
            self._scopes.append(self._snapshot(namespace))
            if command["kind"] in _SCOPE_KINDS:
                text = source_bytes[start:end].decode("utf-8", "replace")
                self._replay(command["kind"], text, namespace, start)
            else:
                # A declaration ends any ``open ... in`` prefixes before it.
                self._pending = _Frame()

    def _replay(self, kind: str, text: str, namespace: str, start: int) -> None:
        """Apply one command to the frames."""
        prefix = _ends_with_in(text)
        target = self._pending if prefix else self._frames[-1]
        if kind == "open":
            target.opened.extend(parse_open(text, namespace))
        elif kind == "variable":
            known = {
                name
                for frame in [*self._frames, self._pending]
                for variable in frame.variables
                for name in variable.names
            }
            # A binder without a type only changes how a known variable binds.
            target.variables.extend(
                variable
                for variable in parse_variables(text)
                if variable.type_items or not set(variable.names) <= known
            )
        elif text.startswith("include"):
            target.included.extend(_identifier_texts(nest(tokenize(text))[1:]))
        elif text.startswith("omit"):
            target.omitted.extend(omitted_keys(text))
        elif kind in _FRAME_OPENERS or kind == "mutual":
            self._push(kind, text, start)
        elif kind == "end":
            self._pop(text)
        elif text.startswith("export"):
            self._export(text, namespace)
        if not prefix:
            self._pending = _Frame()

    def _push(self, kind: str, text: str, start: int) -> None:
        """Open one frame per component of a namespace or section name."""
        words = _identifier_texts(nest(tokenize(text))[1:])
        depth = len(words[0].split(".")) if words and kind != "mutual" else 1
        self._frames.extend(_Frame(kind, start) for _ in range(depth))

    def _pop(self, text: str) -> None:
        """Close the frames that an ``end`` command ends."""
        words = _identifier_texts(nest(tokenize(text))[1:])
        depth = len(words[0].split(".")) if words else 1
        if self._frames[-1].kind == "mutual":
            depth = 1
        for _ in range(min(depth, len(self._frames) - 1)):
            self._frames.pop()

    def _export(self, text: str, namespace: str) -> None:
        """Record the aliases an ``export A (x y)`` command creates."""
        for declaration in parse_open(text, namespace):
            for name in declaration.only or ():
                alias = f"{namespace}.{name}" if namespace else name
                self.aliases[alias] = declaration

    def _snapshot(self, namespace: str) -> Scope:
        """Freeze the current frames into a scope."""
        frames = [*self._frames, self._pending]
        mutual = [frame.start for frame in self._frames if frame.kind == "mutual"]
        return Scope(
            namespace,
            tuple(opened for frame in frames for opened in frame.opened),
            tuple(variable for frame in frames for variable in frame.variables),
            frozenset(name for frame in frames for name in frame.included),
            frozenset(name for frame in frames for name in frame.omitted),
            mutual[0] if mutual else None,
        )

    def at(self, byte_offset: int) -> Scope:
        """Return the scope of the command that contains a byte offset."""
        index = bisect.bisect_right(self._starts, byte_offset) - 1
        return self._scopes[index] if index >= 0 else Scope()
