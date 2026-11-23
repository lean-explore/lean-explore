"""Reuse results from previous extraction databases.

Informalization and embedding generation both backfill a column of the
``declarations`` table with the output of an expensive model. Before calling the
model they reuse values computed by earlier extraction runs: every
``lean_explore.db`` under the data and cache directories is scanned into an
in-memory cache, cache hits are applied, and only the misses are processed.
"""

import logging
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import TypeVar

from lean_explore.config import Config

logger = logging.getLogger(__name__)

DATABASE_FILENAME = "lean_explore.db"
"""File name of the extraction database searched for in data/cache directories."""

KeyT = TypeVar("KeyT", bound=Hashable)
ValueT = TypeVar("ValueT")
ItemT = TypeVar("ItemT")


def discover_database_files() -> list[Path]:
    """Discover all lean_explore.db files in data/ and cache/ directories.

    Returns:
        List of paths to discovered database files, data directory first.
    """
    database_files: list[Path] = []
    for directory in (Config.DATA_DIRECTORY, Config.CACHE_DIRECTORY):
        if directory.exists():
            database_files.extend(directory.rglob(DATABASE_FILENAME))

    logger.info("Discovered %d database files", len(database_files))
    return database_files


def load_cache_from_databases(
    database_files: Iterable[Path],
    read_entries: Callable[[Path], Iterator[tuple[KeyT, ValueT]]],
    description: str,
) -> dict[KeyT, ValueT]:
    """Build a cache from key/value entries read from several databases.

    The first database providing a key wins. A database that cannot be read is
    logged and skipped; entries read from it before the failure are kept.

    Args:
        database_files: Database file paths to scan, in priority order.
        read_entries: Function yielding (key, value) cache entries for one
            database file.
        description: Human-readable name of the cached values, for logging.

    Returns:
        Dictionary mapping each key to the first value found for it.
    """
    cache: dict[KeyT, ValueT] = {}

    for db_path in database_files:
        logger.info("Loading %s cache from %s", description, db_path)
        try:
            count = 0
            for key, value in read_entries(db_path):
                count += 1
                cache.setdefault(key, value)
            logger.info("Loaded %d %s from %s", count, description, db_path)
        except Exception as error:
            logger.warning(
                "Failed to load %s cache from %s: %s", description, db_path, error
            )

    logger.info("Total %s cache size: %d unique keys", description, len(cache))
    return cache


def split_cache_hits(
    items: Iterable[ItemT],
    cache: Mapping[KeyT, ValueT],
    key: Callable[[ItemT], KeyT | None],
) -> tuple[list[tuple[ItemT, ValueT]], list[ItemT]]:
    """Partition items into cache hits and misses.

    Args:
        items: Items to look up.
        cache: Cache to look items up in.
        key: Function computing an item's cache key. Items whose key is None are
            dropped from both results.

    Returns:
        Tuple of (hits, misses) where hits pairs each item with its cached value.
    """
    hits: list[tuple[ItemT, ValueT]] = []
    misses: list[ItemT] = []
    for item in items:
        item_key = key(item)
        if item_key is None:
            continue
        if item_key in cache:
            hits.append((item, cache[item_key]))
        else:
            misses.append(item)
    return hits, misses


def chunked(items: Sequence[ItemT], size: int) -> Iterator[Sequence[ItemT]]:
    """Yield consecutive slices of at most ``size`` items.

    Args:
        items: Sequence to split.
        size: Maximum slice length.

    Yields:
        Consecutive slices covering ``items`` in order.
    """
    for start in range(0, len(items), size):
        yield items[start : start + size]
