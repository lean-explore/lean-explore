"""Extract Lean declarations from doc-gen4 output into the search database.

This module orchestrates extraction: for every package workspace it detects
the doc-gen4 output format, parses declarations (reading their Lean source
text), filters auto-generated projections, and inserts the result.

Supports two doc-gen4 output formats:
- SQLite database (api-docs.db): Used by doc-gen4 >= v4.29.0-rc2
  (see :mod:`lean_explore.extract.docgen_sqlite`)
- BMP JSON files (.bmp): Used by doc-gen4 < v4.29.0-rc2
  (see :mod:`lean_explore.extract.docgen_bmp`)
"""

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lean_explore.extract.declaration_writer import insert_declarations_batch
from lean_explore.extract.docgen_bmp import parse_declarations_from_files
from lean_explore.extract.docgen_sqlite import (
    parse_declarations_from_sqlite,
    validate_docgen_sqlite,
)
from lean_explore.extract.lean_source import (
    build_package_cache,
    read_lean_toolchain_version,
)
from lean_explore.extract.projection_filter import filter_auto_generated_projections
from lean_explore.extract.types import Declaration

logger = logging.getLogger(__name__)


def _api_docs_path(workspace_path: Path) -> Path:
    """Return the location of a workspace's doc-gen4 SQLite database."""
    return workspace_path / ".lake" / "build" / "api-docs.db"


def _doc_data_dir(workspace_path: Path) -> Path:
    """Return the directory holding a workspace's legacy BMP files."""
    return workspace_path / ".lake" / "build" / "doc-data"


def _detect_docgen_format(workspace_path: Path) -> str:
    """Detect which doc-gen4 output format a workspace uses.

    Doc-gen4 >= v4.29.0-rc2 writes to a SQLite database (api-docs.db).
    Earlier versions write individual BMP JSON files to doc-data/.

    The SQLite file is validated before returning "sqlite" to guard against
    zero-byte, corrupt, or incompatible databases left by crashed builds.

    Args:
        workspace_path: Path to the package workspace (e.g., lean/mathlib).

    Returns:
        "sqlite" if a valid api-docs.db exists, "bmp" if BMP files exist,
        "none" otherwise.
    """
    api_docs_db = _api_docs_path(workspace_path)
    if api_docs_db.exists():
        if validate_docgen_sqlite(api_docs_db):
            return "sqlite"
        logger.warning(
            "Invalid api-docs.db at %s, checking for BMP fallback",
            api_docs_db,
        )

    doc_data_dir = _doc_data_dir(workspace_path)
    if doc_data_dir.exists():
        bmp_files = list(doc_data_dir.glob("**/*.bmp"))
        if bmp_files:
            return "bmp"

    return "none"


def _extract_package(lean_root: Path, package_name: str) -> list[Declaration]:
    """Extract the declarations of one package workspace.

    Args:
        lean_root: Root directory containing the package workspaces.
        package_name: Registry name of the package (and its workspace).

    Returns:
        The package's declarations, or an empty list if the workspace has no
        doc-gen4 output.
    """
    from lean_explore.extract.package_registry import PACKAGE_REGISTRY

    package_config = PACKAGE_REGISTRY[package_name]
    workspace_path = lean_root / package_name
    docgen_format = _detect_docgen_format(workspace_path)

    if docgen_format == "none":
        logger.warning("No doc-gen4 output found for %s", package_name)
        return []

    # Build workspace-specific package cache to avoid version mismatches
    package_cache = build_package_cache(lean_root, package_name)
    logger.info(
        "Built package cache for %s with %d packages",
        package_name,
        len(package_cache),
    )

    if docgen_format == "sqlite":
        lean_version = read_lean_toolchain_version(workspace_path)
        logger.info("[%s] Using SQLite format (api-docs.db)", package_name)
        declarations = parse_declarations_from_sqlite(
            _api_docs_path(workspace_path),
            lean_root,
            package_cache,
            package_config.should_include_module,
            lean_version=lean_version,
        )
    else:
        bmp_files = sorted(_doc_data_dir(workspace_path).glob("**/*.bmp"))
        logger.info("[%s] Using BMP format (%d files)", package_name, len(bmp_files))
        declarations = parse_declarations_from_files(
            bmp_files, lean_root, package_cache, package_config.should_include_module
        )

    logger.info(
        "Extracted %d declarations from %s (prefixes: %s)",
        len(declarations),
        package_name,
        package_config.module_prefixes,
    )
    return declarations


def _extract_all_packages(lean_root: Path) -> list[Declaration]:
    """Extract declarations from every package workspace in dependency order.

    Args:
        lean_root: Root directory containing the package workspaces.

    Returns:
        All extracted declarations, with auto-generated projections removed.

    Raises:
        FileNotFoundError: If no declarations were extracted from any package.
    """
    from lean_explore.extract.package_utils import get_extraction_order

    all_declarations = []
    for package_name in get_extraction_order():
        all_declarations.extend(_extract_package(lean_root, package_name))

    if not all_declarations:
        raise FileNotFoundError("No declarations extracted from any package workspace")

    logger.info("Total declarations extracted: %d", len(all_declarations))

    # Filter out auto-generated 'to*' projections that share source with parent
    all_declarations, projection_count = filter_auto_generated_projections(
        all_declarations
    )
    if projection_count > 0:
        logger.info("Filtered %d auto-generated 'to*' projections", projection_count)
    return all_declarations


async def extract_declarations(engine: AsyncEngine, batch_size: int = 1000) -> None:
    """Extract all declarations from doc-gen4 data and load into database.

    Automatically detects whether each package uses the newer SQLite format
    (api-docs.db from doc-gen4 >= v4.29.0-rc2) or the legacy BMP JSON format.
    Package workspaces are read from ``lean/`` relative to the current
    working directory.

    Args:
        engine: SQLAlchemy async engine for database connection.
        batch_size: Number of declarations to insert per database transaction.

    Raises:
        FileNotFoundError: If no declarations were extracted from any package.
    """
    all_declarations = _extract_all_packages(Path("lean"))

    async with AsyncSession(engine) as session:
        inserted_count = await insert_declarations_batch(
            session, all_declarations, batch_size
        )

    skipped = len(all_declarations) - inserted_count
    logger.info(
        "Inserted %d new declarations into database (skipped %d duplicates)",
        inserted_count,
        skipped,
    )
