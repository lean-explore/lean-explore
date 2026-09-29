"""Reader for the doc-gen4 SQLite database (``api-docs.db``).

Doc-gen4 >= v4.29.0-rc2 writes declaration data to a SQLite database instead
of individual BMP JSON files. This module validates that database and turns
its rows into the same Declaration objects the BMP reader produces.
"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lean_explore.extract.docgen_common import drop_self_references, new_progress
from lean_explore.extract.lean_source import SourceTextReader, construct_source_link
from lean_explore.extract.rendered_code import extract_names_from_rendered_code
from lean_explore.extract.types import Declaration

logger = logging.getLogger(__name__)

REQUIRED_DOCGEN_TABLES = {"name_info", "declaration_ranges", "modules"}

# Query declarations with source ranges and both docstring types.
# Doc-gen4 stores docstrings as either markdown text or Verso binary
# BLOBs (never both). We prefer markdown; Verso BLOBs require a
# complex deserializer so we detect but cannot extract them yet.
_DECLARATIONS_QUERY = """
    SELECT
        n.module_name,
        n.position,
        n.kind,
        n.name,
        n.type,
        r.start_line,
        r.end_line,
        d.text AS docstring,
        v.content AS verso_docstring,
        m.source_url
    FROM name_info n
    JOIN declaration_ranges r
        ON n.module_name = r.module_name AND n.position = r.position
    LEFT JOIN declaration_markdown_docstrings d
        ON n.module_name = d.module_name AND n.position = d.position
    LEFT JOIN declaration_verso_docstrings v
        ON n.module_name = v.module_name AND n.position = v.position
    JOIN modules m
        ON n.module_name = m.name
    WHERE n.render = 1
    ORDER BY n.module_name, n.position
"""


def validate_docgen_sqlite(database_path: Path) -> bool:
    """Check that a doc-gen4 api-docs.db is a valid, usable SQLite database.

    Verifies the file is non-empty, opens as SQLite, and contains the tables
    that the extraction pipeline requires.

    Args:
        database_path: Path to the api-docs.db file.

    Returns:
        True if the database is valid and contains the required tables.
    """
    if database_path.stat().st_size == 0:
        logger.warning("api-docs.db exists but is empty: %s", database_path)
        return False

    try:
        connection = sqlite3.connect(str(database_path))
        try:
            cursor = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = {row[0] for row in cursor.fetchall()}
        finally:
            connection.close()
    except sqlite3.DatabaseError as error:
        logger.warning("api-docs.db is not a valid SQLite file: %s", error)
        return False

    missing = REQUIRED_DOCGEN_TABLES - tables
    if missing:
        logger.warning(
            "api-docs.db is missing required tables %s: %s", missing, database_path
        )
        return False

    return True


@dataclass
class _SqliteParseStats:
    """Counts of rows skipped or degraded while parsing api-docs.db."""

    skipped_prefix: int = 0
    skipped_constructor: int = 0
    skipped_no_source: int = 0
    verso_only_docstrings: int = 0

    def log_summary(self) -> None:
        """Log every non-zero counter."""
        if self.skipped_prefix > 0:
            logger.info(
                "Skipped %d declarations outside allowed prefixes",
                self.skipped_prefix,
            )
        if self.skipped_constructor > 0:
            logger.info("Skipped %d .mk constructors", self.skipped_constructor)
        if self.skipped_no_source > 0:
            logger.info(
                "Skipped %d declarations without source URL", self.skipped_no_source
            )
        if self.verso_only_docstrings > 0:
            logger.warning(
                "%d declarations have Verso-only docstrings "
                "(not yet supported, stored as docstring=None)",
                self.verso_only_docstrings,
            )


@dataclass
class _RowConverter:
    """Converts api-docs.db rows to Declarations, recording skip statistics."""

    include_module: Callable[[str], bool]
    source_reader: SourceTextReader
    lean_version: str | None
    stats: _SqliteParseStats

    def convert(self, row: sqlite3.Row) -> Declaration | None:
        """Build a Declaration from a row, or return None if it is skipped."""
        module_name = row["module_name"]
        declaration_name = row["name"]

        if not self.include_module(module_name):
            self.stats.skipped_prefix += 1
            return None

        # Skip auto-generated .mk constructors
        if declaration_name.endswith(".mk"):
            self.stats.skipped_constructor += 1
            return None

        source_link = construct_source_link(
            module_name,
            row["source_url"],
            row["start_line"],
            row["end_line"],
            lean_version=self.lean_version,
        )
        if not source_link:
            self.stats.skipped_no_source += 1
            return None

        source_text = self.source_reader.read(source_link, declaration_name)
        if source_text is None:
            return None

        # Use markdown docstring; detect Verso-only cases
        if not row["docstring"] and row["verso_docstring"]:
            self.stats.verso_only_docstrings += 1

        return Declaration(
            name=declaration_name,
            module=module_name,
            docstring=row["docstring"],
            source_text=source_text,
            source_link=source_link,
            dependencies=_dependencies_from_type(row["type"], declaration_name),
        )


def _dependencies_from_type(
    type_blob: bytes | None, declaration_name: str
) -> list[str] | None:
    """Extract dependency names from a RenderedCode type signature BLOB."""
    if not type_blob:
        return None
    names = extract_names_from_rendered_code(bytes(type_blob))
    return drop_self_references(names, declaration_name)


def _fetch_rendered_rows(database_path: Path) -> list[sqlite3.Row]:
    """Fetch every rendered declaration row from api-docs.db."""
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(_DECLARATIONS_QUERY).fetchall()
    finally:
        connection.close()
    logger.info("Found %d declarations in api-docs.db", len(rows))
    return rows


def parse_declarations_from_sqlite(
    database_path: Path,
    lean_root: Path,
    package_cache: dict[str, Path],
    include_module: Callable[[str], bool],
    lean_version: str | None = None,
) -> list[Declaration]:
    """Parse declarations from a doc-gen4 SQLite database (api-docs.db).

    Args:
        database_path: Path to the api-docs.db SQLite database.
        lean_root: Root directory of the Lean project.
        package_cache: Dictionary mapping package names to their directories.
        include_module: Predicate selecting the modules to extract, typically
            ``PackageConfig.should_include_module``.
        lean_version: Lean toolchain version for core module source links.

    Returns:
        List of parsed Declaration objects.
    """
    rows = _fetch_rendered_rows(database_path)
    source_reader = SourceTextReader(lean_root, package_cache)
    converter = _RowConverter(
        include_module, source_reader, lean_version, _SqliteParseStats()
    )
    declarations = []

    with new_progress() as progress:
        task = progress.add_task("[cyan]Parsing api-docs.db...", total=len(rows))
        for row in rows:
            declaration = converter.convert(row)
            if declaration is not None:
                declarations.append(declaration)
            progress.update(task, advance=1)

    converter.stats.log_summary()
    source_reader.log_summary()
    return declarations
