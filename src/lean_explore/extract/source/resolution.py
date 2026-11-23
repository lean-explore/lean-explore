"""Resolve the references of a package's parsed declarations.

The reader collects every file's declarations with the scope each one is
written in. This module builds the table of names they can refer to (with
the module imports and declaration order that decide what is visible where)
and fills in each stored row's dependencies.
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from lean_explore.extract.source.names import NameTable, Position, Use
from lean_explore.extract.source.references import (
    find_references,
    signature_binder_names,
)
from lean_explore.extract.source.scopes import OpenDeclaration, Scope
from lean_explore.extract.types import Declaration


@dataclass
class Row:
    """One parsed declaration with what is needed to resolve its references."""

    parsed: dict[str, Any]
    """The declaration as the parser reports it."""

    scope: Scope
    """The scope in effect where it is written."""

    declaration: Declaration | None
    """The row to store, or None for a declaration that only shadows others."""

    namespaces: tuple[str, ...]
    """Namespaces its signature is resolved in, innermost first."""

    bound: list[str] = field(default_factory=list)
    """Names bound by the declaration it belongs to, such as a structure."""

    siblings: dict[str, str] = field(default_factory=dict)
    """The other fields of the structure a field belongs to."""


@dataclass
class SourceFile:
    """The declarations of one file and what it imports and exports."""

    module: str
    rows: list[Row]
    imports: list[str]
    aliases: dict[str, OpenDeclaration]
    owner_names: list[str | None]
    """The name of every declaration of the file, by its index."""


def _prefixes(name: str) -> tuple[str, ...]:
    """Return a name and its prefixes, longest first."""
    parts = name.split(".") if name else []
    return tuple(".".join(parts[:size]) for size in range(len(parts), 0, -1))


def lookup_namespaces(
    declaration: dict[str, Any],
    scope: Scope,
    file_declarations: list[dict[str, Any]],
) -> tuple[str, ...]:
    """Return the namespaces a declaration's signature is resolved in.

    A declaration ``Foo.bar`` inside ``namespace A`` sees ``A.Foo`` and ``A``.
    One written ``_root_.Foo.bar`` keeps the enclosing ``A`` only. A field or
    constructor is resolved where its structure or inductive type is.
    """
    parent = declaration.get("parent")
    if parent is not None:
        return lookup_namespaces(file_declarations[parent], scope, file_declarations)
    name = declaration["name"] or ""
    if not scope.namespace or name.startswith(scope.namespace + "."):
        return _prefixes(name)[1:]
    return _prefixes(scope.namespace)


def owner_context(
    declaration: dict[str, Any], file_declarations: list[dict[str, Any]]
) -> tuple[list[str], dict[str, str]]:
    """Return the binders and sibling fields a field or constructor can see."""
    parent = declaration.get("parent")
    if parent is None:
        return [], {}
    owner = file_declarations[parent]
    bound = signature_binder_names(
        owner.get("signature_text") or "", owner["name"] or ""
    )
    siblings = {
        other["name"].rsplit(".", 1)[-1]: other["name"]
        for other in file_declarations
        if other.get("parent") == parent and other["kind"] == "field" and other["name"]
    }
    return bound, siblings if declaration["kind"] == "field" else {}


def is_named(declaration: dict[str, Any], include_approximate: bool) -> bool:
    """Tell whether a declaration has a name that others can refer to."""
    return bool(declaration["name"]) and (
        include_approximate or not declaration["approximate"]
    )


def _visible_modules(imports: list[list[int]]) -> list[int]:
    """Compute, per module, a bit set of itself and everything it imports."""
    visible = [0] * len(imports)
    done = [False] * len(imports)
    started = [False] * len(imports)
    for root in range(len(imports)):
        stack = [root]
        while stack:
            module = stack[-1]
            started[module] = True
            waiting = [other for other in imports[module] if not started[other]]
            if waiting and not done[module]:
                stack.extend(waiting)
                continue
            mask = 1 << module
            for other in imports[module]:
                mask |= visible[other]
            visible[module] = mask
            done[module] = True
            stack.pop()
    return visible


def name_table(files: list[SourceFile], known_names: Collection[str]) -> NameTable:
    """Build the table of names the package's signatures can refer to."""
    indexes = {file.module: index for index, file in enumerate(files)}
    stored = set(known_names)
    declared: set[str] = set()
    private: set[str] = set()
    protected: set[str] = set()
    positions: dict[str, Position] = {}
    aliases: dict[str, OpenDeclaration] = {}
    for index, file in enumerate(files):
        aliases.update(file.aliases)
        for row in file.rows:
            name = row.parsed["name"]
            positions.setdefault(name, Position(index, row.parsed["range"][0]))
            declared.add(name)
            if row.declaration:
                stored.add(name)
            if "private" in row.parsed["modifiers"]:
                private.add(name)
            if "protected" in row.parsed["modifiers"]:
                protected.add(name)
    imports = [
        [indexes[name] for name in file.imports if name in indexes] for file in files
    ]
    return NameTable(
        declared | stored,
        private - stored,
        declared - stored,
        protected,
        positions,
        _visible_modules(imports),
        aliases,
    )


def _with_owner(
    dependencies: list[str] | None, owner: str | None, table: NameTable
) -> list[str] | None:
    """Add the inductive type to a constructor's references.

    A constructor written without a result type, such as ``| nil``, still
    has its inductive type as its type.
    """
    found = dependencies or []
    if owner and table.records(owner) and owner not in found:
        found = [*found, owner]
    return found or None


def resolve_rows(files: list[SourceFile], table: NameTable) -> list[Declaration]:
    """Fill in the references of every stored row, one per name."""
    stored: dict[str, Declaration] = {}
    for index, file in enumerate(files):
        for row in file.rows:
            declaration = row.declaration
            if declaration is None or declaration.name in stored:
                continue
            use = Use(
                declaration.name,
                row.scope.opened,
                Position(index, row.parsed["range"][0]),
                row.scope.mutual_start,
                row.namespaces,
                row.siblings,
            )
            declaration.dependencies = find_references(
                row.parsed.get("signature_text") or "",
                row.scope,
                table,
                use,
                row.parsed["kind"],
                row.bound,
                row.parsed.get("text") or "",
            )
            owner = row.parsed.get("parent")
            if row.parsed["kind"] == "constructor" and owner is not None:
                declaration.dependencies = _with_owner(
                    declaration.dependencies, file.owner_names[owner], table
                )
            stored[declaration.name] = declaration
    return list(stored.values())
