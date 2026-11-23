"""Resolve identifiers written in Lean source to declaration names.

The rules follow ``Lean/ResolveName.lean``: an identifier is looked up in the
namespaces of the declaration it is written in, innermost first, then as a
root name, through the namespaces opened there and through exported aliases.
Where several names match equally, Lean chooses by type, so nothing is
resolved.
"""

from collections.abc import Collection
from dataclasses import dataclass, field

from lean_explore.extract.source.scopes import OpenDeclaration


@dataclass(frozen=True)
class Position:
    """Where a declaration is written."""

    module: int
    """Index of the declaration's module."""

    offset: int
    """Byte offset of the declaration in its file."""


@dataclass(frozen=True)
class Use:
    """The place an identifier is written."""

    name: str
    """Fully qualified name of the declaration the identifier is in."""

    opened: tuple[OpenDeclaration, ...] = ()
    """Open declarations in effect there."""

    position: Position | None = None
    """Where the declaration is written, if known."""

    mutual_start: int | None = None
    """Byte offset of the enclosing ``mutual`` block, if there is one."""

    namespaces: tuple[str, ...] | None = None
    """Namespaces searched first, innermost first; None derives them from the name."""

    siblings: dict[str, str] = field(default_factory=dict)
    """Short names of the fields beside a structure field, with their full names."""


@dataclass
class NameTable:
    """The declarations that identifiers can resolve to."""

    names: Collection[str]
    """Every declaration name that can be referred to."""

    private: Collection[str] = frozenset()
    """Names that are visible only in the module that declares them."""

    unrecorded: Collection[str] = frozenset()
    """Names that shadow others but are not stored, so never a reference."""

    protected: Collection[str] = frozenset()
    """Names that cannot be reached by their last component alone."""

    positions: dict[str, Position] = field(default_factory=dict)
    """Where the package's own declarations are written."""

    visible: list[int] = field(default_factory=list)
    """Per module, a bit set of the modules whose declarations it can see."""

    aliases: dict[str, OpenDeclaration] = field(default_factory=dict)
    """Names that ``export`` makes available, by their full alias name."""

    namespaces: set[str] = field(init=False)
    """Every proper prefix of a known name."""

    _opened: dict[tuple[OpenDeclaration, ...], list[tuple[str, OpenDeclaration]]] = (
        field(init=False, default_factory=dict)
    )

    def __post_init__(self) -> None:
        """Derive the namespaces from the names."""
        self.namespaces = set()
        for name in self.names:
            end = name.rfind(".")
            while end > 0 and name[:end] not in self.namespaces:
                self.namespaces.add(name[:end])
                end = name.rfind(".", 0, end)

    def open_namespaces(
        self, opened: tuple[OpenDeclaration, ...]
    ) -> list[tuple[str, OpenDeclaration]]:
        """Return the namespaces a run of open declarations opens, cached."""
        if opened not in self._opened:
            self._opened[opened] = _open_namespaces(opened, self)
        return self._opened[opened]

    def records(self, name: str) -> bool:
        """Tell whether a name is stored, so that it can be a reference."""
        return name not in self.private and name not in self.unrecorded

    def has(self, name: str, use: Use) -> bool:
        """Tell whether ``name`` is declared and visible where it is used."""
        if name not in self.names:
            return False
        position = self.positions.get(name)
        if position is None or use.position is None:
            return True
        if name in self.private and position.module != use.position.module:
            return False
        if position.module != use.position.module:
            if use.position.module >= len(self.visible):
                return True
            return bool(self.visible[use.position.module] >> position.module & 1)
        if position.offset < use.position.offset:
            return True
        return use.mutual_start is not None and position.offset > use.mutual_start


def _enclosing_namespaces(name: str) -> list[str]:
    """List the namespaces a declaration is in, innermost first."""
    parts = name.split(".")
    return [".".join(parts[:size]) for size in range(len(parts) - 1, 0, -1)]


def _reachable(candidate: str, identifier: str, table: NameTable, use: Use) -> bool:
    """Tell whether a short identifier can stand for a full candidate name."""
    if not table.has(candidate, use):
        return False
    return "." in identifier or candidate not in table.protected


def _open_namespaces(
    opened: tuple[OpenDeclaration, ...], table: NameTable
) -> list[tuple[str, OpenDeclaration]]:
    """Bind each open declaration to the namespace it opens.

    Lean binds the written namespace to its first match, innermost first. A
    namespace that is not found that way can be one inside a namespace that
    an earlier ``open`` opened, as in ``open Outer Nested``.
    """
    bound: list[tuple[str, OpenDeclaration]] = []
    for declaration in opened:
        written = declaration.candidates[-1]
        inside = [f"{namespace}.{written}" for namespace, _ in reversed(bound)]
        for candidate in (*declaration.candidates, *inside):
            if candidate in table.namespaces:
                bound.append((candidate, declaration))
                break
    return bound


def _through_open(
    identifier: str,
    namespace: str,
    opened: OpenDeclaration,
    table: NameTable,
    use: Use,
) -> set[str]:
    """Return what an identifier can mean through one open declaration."""
    if opened.only is not None and identifier not in opened.only:
        return set()
    if identifier.split(".")[0] in opened.hidden:
        return set()
    target = dict(opened.renamed).get(identifier, identifier)
    candidate = f"{namespace}.{target}"
    selected = opened.only is not None or target != identifier
    if selected and table.has(candidate, use):
        return {candidate}
    return {candidate} if _reachable(candidate, target, table, use) else set()


def _candidates(identifier: str, table: NameTable, use: Use) -> set[str]:
    """Return the names an identifier can stand for, at the first level found.

    Names in the declaration's own namespaces come first, innermost first,
    then an exact dotted root name. After them, the root name, opened
    namespaces and exported aliases count equally, as Lean would choose
    among them by type.
    """
    if identifier in use.siblings:
        return {use.siblings[identifier]}
    namespaces = (
        _enclosing_namespaces(use.name)
        if use.namespaces is None
        else list(use.namespaces)
    )
    for namespace in namespaces:
        if _reachable(f"{namespace}.{identifier}", identifier, table, use):
            return {f"{namespace}.{identifier}"}
    found = {identifier} if table.has(identifier, use) else set()
    if found and "." in identifier:
        return found
    for namespace, opened in table.open_namespaces(use.opened):
        found |= _through_open(identifier, namespace, opened, table, use)
    for namespace in [*namespaces, ""]:
        alias = f"{namespace}.{identifier}" if namespace else identifier
        exported = table.aliases.get(alias)
        if exported is not None:
            for target, opened in _open_namespaces((exported,), table):
                found |= _through_open(identifier, target, opened, table, use)
    return found


def resolve(identifier: str, table: NameTable, use: Use) -> str | None:
    """Resolve an identifier written inside a declaration to a known name.

    A dotted identifier whose full form is unknown is retried without its
    trailing components, so ``Foo.bar.baz`` resolves to ``Foo.bar`` when
    ``baz`` is a field access.

    Args:
        identifier: The identifier as written.
        table: The declarations that can be referred to.
        use: Where the identifier is written.

    Returns:
        The full name of the constant, or None if nothing matches, several
        names match equally, or the match is not a recorded declaration.
    """
    if identifier.startswith("_root_."):
        exact = identifier.removeprefix("_root_.")
        return exact if table.has(exact, use) and table.records(exact) else None
    parts = identifier.split(".")
    for size in range(len(parts), 0, -1):
        found = _candidates(".".join(parts[:size]), table, use)
        if len(found) == 1:
            (name,) = found
            return name if table.records(name) else None
        if found:
            return None
    return None
