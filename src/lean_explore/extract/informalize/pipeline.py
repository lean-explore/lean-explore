"""Generate informal natural language descriptions for Lean declarations.

Reads declarations from the database, generates informal descriptions using
an LLM via OpenRouter, and updates the informalization field.

Informalizations from previous extraction databases are reused first (keyed by
declaration name and source text). The remaining declarations are processed in
dependency order (see ``informalize_generation``), so each prompt can include
the informal descriptions of the declaration's dependencies.
"""

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session

from lean_explore.extract._cache import (
    chunked,
    discover_database_files,
    load_cache_from_databases,
    split_cache_hits,
)
from lean_explore.extract.informalize.dependency_layers import build_dependency_layers
from lean_explore.extract.informalize.generation import (
    DeclarationData,
    InformalizationCache,
    InformalizationResult,
    InformalizationRun,
    process_layers,
)
from lean_explore.models import Declaration
from lean_explore.util import OpenRouterClient

__all__ = [
    "DeclarationData",
    "InformalizationResult",
    "informalize_declarations",
]

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompt.txt"
"""Prompt template with {name}, {source_text}, {docstring}, {dependencies}."""


# --- Database Loading ---


async def _load_existing_informalizations(
    session: AsyncSession,
) -> list[InformalizationResult]:
    """Load all existing informalizations from the database."""
    logger.info("Loading existing informalizations...")
    stmt = select(Declaration).where(Declaration.informalization.isnot(None))
    result = await session.execute(stmt)
    informalizations = [
        InformalizationResult(
            declaration_id=declaration.id,
            declaration_name=declaration.name,
            informalization=declaration.informalization,
        )
        for declaration in result.scalars().all()
    ]
    logger.info("Loaded %d existing informalizations", len(informalizations))
    return informalizations


async def _get_declarations_to_process(
    session: AsyncSession, limit: int | None
) -> list[Declaration]:
    """Query and return declarations that need informalization."""
    stmt = select(Declaration).where(Declaration.informalization.is_(None))
    if limit:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# --- Cross-Database Cache Loading ---


def _read_cached_informalizations(
    db_path: Path,
) -> Iterator[tuple[tuple[str, str], str]]:
    """Yield informalization cache entries stored in one database.

    Args:
        db_path: Path to a lean_explore.db file.

    Yields:
        ((name, source_text), informalization) for each informalized declaration.
    """
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with Session(engine) as session:
            stmt = select(
                Declaration.name,
                Declaration.source_text,
                Declaration.informalization,
            ).where(Declaration.informalization.isnot(None))
            for name, source_text, informalization in session.execute(stmt):
                if informalization is not None:
                    yield (name, source_text), informalization
    finally:
        engine.dispose()


def _load_cache_from_databases(database_files: list[Path]) -> InformalizationCache:
    """Load informalizations from all discovered databases.

    Args:
        database_files: List of database file paths to scan

    Returns:
        Dictionary mapping (name, source_text) -> informalization
    """
    return load_cache_from_databases(
        database_files, _read_cached_informalizations, "informalizations"
    )


async def _apply_cache_to_declarations(
    session: AsyncSession,
    declarations: list[Declaration],
    cache: InformalizationCache,
    commit_batch_size: int = 1000,
) -> tuple[int, list[Declaration]]:
    """Apply cached informalizations to declarations.

    This is a fast first pass that applies all cache hits before making any
    API calls, allowing the user to see exactly how many API calls will be needed.

    Args:
        session: Async database session
        declarations: List of declarations to check against cache
        cache: Map of (name, source_text) to cached informalizations
        commit_batch_size: Number of updates to batch before committing

    Returns:
        Tuple of (cache_hits_count, list of declarations still needing API calls)
    """
    hits, remaining = split_cache_hits(
        declarations, cache, key=lambda d: (d.name, d.source_text)
    )
    logger.info(
        "Cache matching complete: %d hits, %d misses", len(hits), len(remaining)
    )
    if not hits:
        return 0, remaining

    total_batches = (len(hits) + commit_batch_size - 1) // commit_batch_size
    logger.info(
        "Applying %d cached informalizations in %d batches...",
        len(hits),
        total_batches,
    )
    # Raw SQL avoids ORM overhead for what can be hundreds of thousands of rows
    stmt = text("UPDATE declarations SET informalization = :inf WHERE id = :id")
    for batch_num, batch in enumerate(chunked(hits, commit_batch_size), 1):
        params = [{"id": decl.id, "inf": informal} for decl, informal in batch]
        connection = await session.connection()
        await connection.execute(stmt, params)
        await session.commit()
        if batch_num % 10 == 0 or batch_num == total_batches:
            logger.info("Committed batch %d/%d", batch_num, total_batches)

    return len(hits), remaining


# --- Pipeline Phases ---


async def _apply_cache_phase(
    session: AsyncSession,
    cache: InformalizationCache,
    limit: int | None,
    commit_batch_size: int,
) -> list[Declaration]:
    """Find declarations needing informalization and fill in cache hits.

    Args:
        session: Async database session for the search database.
        cache: Map of (name, source_text) to cached informalizations.
        limit: Maximum number of declarations to process (None for all).
        commit_batch_size: Number of updates to batch before committing.

    Returns:
        Declarations that still need an LLM call.
    """
    declarations = await _get_declarations_to_process(session, limit)
    logger.info("Found %d declarations needing informalization", len(declarations))
    if not declarations:
        logger.info("No declarations to process")
        return []

    logger.info("Phase 1: Applying cached informalizations...")
    cache_hits, remaining = await _apply_cache_to_declarations(
        session, declarations, cache, commit_batch_size
    )
    logger.info(
        "Applied %d informalizations from cache, %d remaining need API calls",
        cache_hits,
        len(remaining),
    )
    if not remaining:
        logger.info("All declarations served from cache, no API calls needed")
    return remaining


async def _generate_phase(
    session: AsyncSession,
    remaining: list[Declaration],
    cache: InformalizationCache,
    *,
    model: str,
    prompt_template: str,
    max_concurrent: int,
    commit_batch_size: int,
) -> None:
    """Generate informalizations for cache misses with the LLM.

    Args:
        session: Async database session for the search database.
        remaining: Declarations that still need informalization.
        cache: Map of (name, source_text) to cached informalizations.
        model: LLM model to use for generation.
        prompt_template: Prompt template string.
        max_concurrent: Maximum number of concurrent LLM API calls.
        commit_batch_size: Number of updates to batch before committing.
    """
    logger.info("Phase 2: Making API calls for remaining declarations...")
    run = InformalizationRun(
        session=session,
        client=OpenRouterClient(),
        model=model,
        prompt_template=prompt_template,
        # Includes the cache hits applied in phase 1
        informalizations_by_name={
            existing.declaration_name: existing.informalization
            for existing in await _load_existing_informalizations(session)
            if existing.informalization is not None
        },
        cache=cache,
        semaphore=asyncio.Semaphore(max_concurrent),
        commit_batch_size=commit_batch_size,
    )

    logger.info("Building dependency layers for remaining declarations...")
    layers = build_dependency_layers(remaining)
    logger.info("Built %d dependency layers", len(layers))

    processed = await process_layers(run, layers)
    logger.info(
        "Informalization complete. Processed %d/%d remaining declarations via API",
        processed,
        len(remaining),
    )


# --- Public API ---


async def informalize_declarations(
    search_db_engine: AsyncEngine,
    *,
    model: str = "google/gemini-3-flash-preview",
    commit_batch_size: int = 1000,
    max_concurrent: int = 10,
    limit: int | None = None,
) -> None:
    """Generate informalizations for declarations missing them.

    Args:
        search_db_engine: Async database engine for search database (Declaration table)
        model: LLM model to use for generation
        commit_batch_size: Number of updates to batch before committing to database
        max_concurrent: Maximum number of concurrent LLM API calls
        limit: Maximum number of declarations to process (None for all)
    """
    prompt_template = PROMPT_PATH.read_text()
    logger.info("Starting informalization process...")
    logger.info(
        "Model: %s, Max concurrent: %d, Commit batch size: %d",
        model,
        max_concurrent,
        commit_batch_size,
    )

    logger.info("Discovering existing databases for cache...")
    cache = _load_cache_from_databases(discover_database_files())

    async with AsyncSession(search_db_engine, expire_on_commit=False) as session:
        remaining = await _apply_cache_phase(session, cache, limit, commit_batch_size)
        if remaining:
            await _generate_phase(
                session,
                remaining,
                cache,
                model=model,
                prompt_template=prompt_template,
                max_concurrent=max_concurrent,
                commit_batch_size=commit_batch_size,
            )
