"""Read the published LeanExplore database that the benchmark is pinned to.

The benchmark compares descriptions of the same declarations, so every input
comes from one published extraction: the declarations, their dependencies and
the descriptions that production wrote for them. Mathlib rows of that
extraction were built from ``MATHLIB_COMMIT``.
"""

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

DATA_VERSION = "20260714_172516"
"""Version of the published data that the benchmark inputs come from."""

DATA_URL = (
    "https://pub-48b75babc4664808b15520033423c765.r2.dev/assets/"
    f"{DATA_VERSION}/lean_explore.db"
)
"""Download location of the published database."""

MATHLIB_REPOSITORY = "leanprover-community/mathlib4"
MATHLIB_COMMIT = "aae61ae084c21995ee248964a81e3750ad0db2db"
"""Mathlib commit of every Mathlib row in the published database."""

_SOURCE_LINK = re.compile(
    r"github\.com/(?P<repository>[^/]+/[^/]+)/blob/(?P<commit>[^/]+)/"
    r"(?P<path>[^#]+)#L(?P<start>\d+)-L(?P<end>\d+)"
)


@dataclass(frozen=True)
class SourceLocation:
    """Where a declaration is written, read from its source link."""

    repository: str
    commit: str
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class DeclarationRow:
    """One declaration of the published database."""

    name: str
    module: str
    docstring: str | None
    source_text: str
    dependencies: list[str]
    informalization: str | None
    location: SourceLocation | None


def parse_source_link(link: str | None) -> SourceLocation | None:
    """Read repository, commit, path and line range from a source link.

    Args:
        link: A GitHub blob link ending in ``#L<start>-L<end>``.

    Returns:
        The location, or None when the link has another form.
    """
    match = _SOURCE_LINK.search(link or "")
    if match is None:
        return None
    return SourceLocation(
        repository=match["repository"],
        commit=match["commit"],
        path=match["path"],
        start_line=int(match["start"]),
        end_line=int(match["end"]),
    )


def parse_dependencies(dependencies: str | None) -> list[str]:
    """Decode the JSON list stored in the ``dependencies`` column."""
    return json.loads(dependencies) if dependencies else []


def connect(database_path: Path) -> sqlite3.Connection:
    """Open the database read-only."""
    return sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)


def is_pinned_mathlib(location: SourceLocation | None) -> bool:
    """Return True for a declaration written in Mathlib at ``MATHLIB_COMMIT``."""
    return (
        location is not None
        and location.repository == MATHLIB_REPOSITORY
        and location.commit == MATHLIB_COMMIT
    )


def mathlib_modules(connection: sqlite3.Connection) -> dict[str, tuple[str, int]]:
    """Map each Mathlib module to its file path and number of declarations.

    Args:
        connection: Connection to the published database.

    Returns:
        Module name to (path in the repository, declaration count), for
        declarations written in Mathlib at the pinned commit.
    """
    modules: dict[str, tuple[str, int]] = {}
    rows = connection.execute("SELECT module, source_link FROM declarations")
    for module, link in rows:
        location = parse_source_link(link)
        if not is_pinned_mathlib(location):
            continue
        assert location is not None
        path, count = modules.get(module, (location.path, 0))
        modules[module] = (path, count + 1)
    return modules


def name_modules(connection: sqlite3.Connection) -> dict[str, str]:
    """Map every declaration name to its module."""
    return dict(connection.execute("SELECT name, module FROM declarations"))


def dependency_frequencies(connection: sqlite3.Connection) -> Counter[str]:
    """Count the declarations that list each name as a dependency."""
    frequencies: Counter[str] = Counter()
    for (dependencies,) in connection.execute("SELECT dependencies FROM declarations"):
        frequencies.update(set(parse_dependencies(dependencies)))
    return frequencies


def _row(values: tuple) -> DeclarationRow:
    """Build a row from the columns selected by ``_ROW_COLUMNS``."""
    name, module, docstring, source_text, dependencies, informal, link = values
    return DeclarationRow(
        name=name,
        module=module,
        docstring=docstring,
        source_text=source_text,
        dependencies=parse_dependencies(dependencies),
        informalization=informal,
        location=parse_source_link(link),
    )


_ROW_COLUMNS = (
    "name, module, docstring, source_text, dependencies, informalization, source_link"
)


def rows_for_modules(
    connection: sqlite3.Connection, modules: Iterable[str]
) -> Iterator[DeclarationRow]:
    """Yield the declarations of the given modules, ordered by module and id."""
    for module in sorted(set(modules)):
        cursor = connection.execute(
            f"SELECT {_ROW_COLUMNS} FROM declarations WHERE module = ? ORDER BY id",
            (module,),
        )
        for values in cursor:
            yield _row(values)


def informalizations(
    connection: sqlite3.Connection, names: Iterable[str]
) -> dict[str, str]:
    """Return the published description of each given name that has one."""
    wanted = sorted(set(names))
    found: dict[str, str] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        placeholders = ", ".join("?" * len(chunk))
        cursor = connection.execute(
            "SELECT name, informalization FROM declarations "
            f"WHERE name IN ({placeholders}) AND informalization IS NOT NULL",
            chunk,
        )
        found.update(cursor)
    return found
