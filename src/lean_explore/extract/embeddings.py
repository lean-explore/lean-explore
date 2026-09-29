"""Generate embeddings for Lean declarations.

Reads declarations from the database and generates informalization embeddings
for semantic search. Embeddings from previous extraction databases are reused
first (keyed by informalization text); only the misses are generated.
"""

import logging
import sqlite3
import struct
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lean_explore.extract._cache import (
    chunked,
    discover_database_files,
    load_cache_from_databases,
    split_cache_hits,
)
from lean_explore.extract.progress import RateColumn, create_progress
from lean_explore.models import Declaration
from lean_explore.util import EmbeddingClient

__all__ = ["EmbeddingCaches", "RateColumn", "generate_embeddings"]

logger = logging.getLogger(__name__)


# --- Data Classes ---


@dataclass
class EmbeddingCaches:
    """Container for embedding caches.

    Stores embeddings as raw bytes for efficiency. Use _deserialize_embedding()
    to convert to list[float] when actually needed.
    """

    by_informalization: dict[str, bytes]


def _deserialize_embedding(data: bytes) -> list[float]:
    """Convert raw binary embedding to list[float].

    Args:
        data: Binary embedding data (float32 packed)

    Returns:
        List of float values
    """
    num_floats = len(data) // 4
    return list(struct.unpack(f"{num_floats}f", data))


# --- Cross-Database Cache Loading ---


def _read_cached_embeddings(db_path: Path) -> Iterator[tuple[str, bytes]]:
    """Yield embedding cache entries stored in one database.

    Uses sync sqlite3 directly to avoid SQLAlchemy ORM overhead and TypeDecorator
    deserialization; embeddings stay raw bytes until actually used.

    Args:
        db_path: Path to a lean_explore.db file.

    Yields:
        (informalization, raw embedding bytes) for each embedded declaration.
    """
    with closing(sqlite3.connect(db_path)) as connection:
        cursor = connection.execute(
            """
            SELECT informalization, informalization_embedding
            FROM declarations
            WHERE informalization_embedding IS NOT NULL
            """
        )
        for informalization, informalization_embedding in cursor:
            if informalization is not None:
                yield informalization, informalization_embedding


def _load_embedding_caches(database_files: list[Path]) -> EmbeddingCaches:
    """Load embeddings from all discovered databases.

    Args:
        database_files: List of database file paths to scan

    Returns:
        EmbeddingCaches mapping informalization text to raw embedding bytes
    """
    return EmbeddingCaches(
        by_informalization=load_cache_from_databases(
            database_files, _read_cached_embeddings, "embeddings"
        )
    )


async def _get_declarations_needing_embeddings(
    session: AsyncSession, limit: int | None
) -> list[Declaration]:
    """Get declarations that need informalization embeddings.

    Only returns declarations that have an informalization but no embedding yet.

    Args:
        session: Async database session
        limit: Maximum number of declarations to retrieve (None for all)

    Returns:
        List of declarations needing embeddings
    """
    stmt = select(Declaration).where(
        Declaration.informalization.isnot(None),
        Declaration.informalization_embedding.is_(None),
    )
    if limit:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _apply_cache_to_declarations(
    session: AsyncSession,
    declarations: list[Declaration],
    caches: EmbeddingCaches,
    commit_batch_size: int = 1000,
) -> tuple[int, list[Declaration]]:
    """Apply cached embeddings to declarations.

    This is a fast first pass that applies all cache hits before generating
    new embeddings, allowing the user to see exactly how many need generation.
    Declarations with an empty informalization are skipped entirely.

    Args:
        session: Async database session
        declarations: List of declarations to check against cache
        caches: Embedding caches from cross-database loading
        commit_batch_size: Number of updates to batch before committing

    Returns:
        Tuple of (cache_hits_count, list of declarations still needing generation)
    """
    hits, remaining = split_cache_hits(
        declarations,
        caches.by_informalization,
        key=lambda declaration: declaration.informalization or None,
    )
    for batch in chunked(hits, commit_batch_size):
        for declaration, raw_embedding in batch:
            declaration.informalization_embedding = _deserialize_embedding(
                raw_embedding
            )
        await session.commit()

    return len(hits), remaining


# --- Generation ---


async def _process_batch(
    session: AsyncSession,
    declarations: Sequence[Declaration],
    client: EmbeddingClient,
) -> int:
    """Process a batch of declarations and generate informalization embeddings.

    Args:
        session: Async database session
        declarations: List of declarations to process (already filtered, no cache)
        client: Embedding client for generating embeddings

    Returns:
        Number of embeddings generated
    """
    declarations_to_embed = [
        declaration
        for declaration in declarations
        if declaration.informalization and declaration.informalization_embedding is None
    ]

    if declarations_to_embed:
        response = await client.embed(
            [declaration.informalization for declaration in declarations_to_embed]
        )
        for declaration, embedding in zip(declarations_to_embed, response.embeddings):
            declaration.informalization_embedding = embedding

    await session.commit()

    return len(declarations_to_embed)


def _create_embedding_client(
    model_name: str, max_seq_length: int, embedding_server_url: str | None
) -> EmbeddingClient:
    """Create a local or remote embedding client.

    Args:
        model_name: Name of the sentence transformer model for local generation.
        max_seq_length: Maximum sequence length for local tokenization.
        embedding_server_url: URL of a backend server to delegate to, if any.

    Returns:
        RemoteEmbeddingClient when a server URL is given, else EmbeddingClient.
    """
    if embedding_server_url:
        from lean_explore.util.remote_embedding_client import RemoteEmbeddingClient

        return RemoteEmbeddingClient(server_url=embedding_server_url)
    return EmbeddingClient(model_name=model_name, max_length=max_seq_length)


async def _generate_in_batches(
    session: AsyncSession,
    declarations: list[Declaration],
    client: EmbeddingClient,
    batch_size: int,
) -> int:
    """Generate embeddings batch by batch with a progress bar.

    Args:
        session: Async database session
        declarations: Declarations still needing embeddings
        client: Embedding client for generating embeddings
        batch_size: Number of declarations per embedding request

    Returns:
        Number of embeddings generated
    """
    total_embeddings = 0
    rate_column = RateColumn(window_seconds=60)
    with create_progress(rate_column) as progress:
        task = progress.add_task("Generating embeddings", total=len(declarations))
        for batch in chunked(declarations, batch_size):
            count = await _process_batch(session, batch, client)
            total_embeddings += count
            rate_column.add_count(count)
            progress.update(task, advance=len(batch))
    return total_embeddings


async def generate_embeddings(
    engine: AsyncEngine,
    model_name: str,
    batch_size: int = 128,
    limit: int | None = None,
    max_seq_length: int = 512,
    embedding_server_url: str | None = None,
) -> None:
    """Generate embeddings for all declarations.

    Args:
        engine: Async database engine
        model_name: Name of the sentence transformer model to use
        batch_size: Number of declarations to process in each batch (default 250)
        limit: Maximum number of declarations to process (None for all)
        max_seq_length: Maximum sequence length for tokenization (default 512).
            Lower values reduce memory usage but may truncate long texts.
        embedding_server_url: URL of a running backend server to delegate
            embedding generation to. When set, uses RemoteEmbeddingClient
            instead of loading the model locally, avoiding GPU memory
            conflicts with a co-located backend process.
    """
    logger.info("Discovering existing databases for embedding cache...")
    caches = _load_embedding_caches(discover_database_files())

    async with AsyncSession(engine, expire_on_commit=False) as session:
        declarations = await _get_declarations_needing_embeddings(session, limit)
        logger.info("Found %d declarations needing embeddings", len(declarations))
        if not declarations:
            logger.info("No declarations to process")
            return

        logger.info("Phase 1: Applying cached embeddings...")
        cache_hits, remaining = await _apply_cache_to_declarations(
            session, declarations, caches
        )
        logger.info(
            "Applied %d embeddings from cache, %d remaining need generation",
            cache_hits,
            len(remaining),
        )
        if not remaining:
            logger.info("All embeddings served from cache, no generation needed")
            return

        logger.info("Phase 2: Generating embeddings for remaining declarations...")
        client = _create_embedding_client(
            model_name, max_seq_length, embedding_server_url
        )
        logger.info("Using %s", client.model_name)
        total_embeddings = await _generate_in_batches(
            session, remaining, client, batch_size
        )

        logger.info(
            "Generated %d new embeddings (%d from cache, %d generated)",
            total_embeddings,
            cache_hits,
            total_embeddings,
        )
