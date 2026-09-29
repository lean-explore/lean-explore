"""Centralized configuration for lean_explore.

This module provides all configuration settings including paths, URLs,
and other constants used throughout the application.
"""

import os
import pathlib
import re
from datetime import datetime


def _get_active_cache_version() -> str:
    """Get the active cache version from the version file or environment.

    The version is determined by (in order of priority):
    1. LEAN_EXPLORE_VERSION environment variable
    2. Contents of ~/.lean_explore/active_version file (set by data fetch)
    3. Default fallback version

    Returns:
        The active version string (e.g., "2025.01.27" or "v4.24.0").
    """
    env_version = os.getenv("LEAN_EXPLORE_VERSION")
    if env_version:
        return env_version

    version_file = pathlib.Path.home() / ".lean_explore" / "active_version"
    if version_file.exists():
        return version_file.read_text().strip()

    return "v4.24.0"


def _get_data_directory() -> pathlib.Path:
    """Get the data directory path."""
    return pathlib.Path(
        os.getenv(
            "LEAN_EXPLORE_DATA_DIR",
            pathlib.Path(__file__).parent.parent.parent / "data",
        )
    )


def _get_timestamped_directories(data_directory: pathlib.Path) -> list[pathlib.Path]:
    """Get all timestamped extraction directories sorted by name descending."""
    if not data_directory.exists():
        return []

    timestamp_pattern = re.compile(r"^\d{8}_\d{6}$")
    timestamped_directories = [
        directory
        for directory in data_directory.iterdir()
        if directory.is_dir() and timestamp_pattern.match(directory.name)
    ]

    timestamped_directories.sort(key=lambda d: d.name, reverse=True)
    return timestamped_directories


_REQUIRED_EXTRACTION_FILES = [
    "lean_explore.db",
    "informalization_faiss.index",
    "informalization_faiss_ids_map.json",
    "bm25_ids_map.json",
    "bm25_name_raw",
    "bm25_name_spaced",
]


def _is_complete_extraction(directory: pathlib.Path) -> bool:
    """Check whether an extraction directory contains all required files.

    Args:
        directory: Path to a timestamped extraction directory.

    Returns:
        True if all required files and directories are present.
    """
    return all((directory / name).exists() for name in _REQUIRED_EXTRACTION_FILES)


def _resolve_active_data_path(
    data_directory: pathlib.Path, active_version: str
) -> pathlib.Path:
    """Resolve the active data path using the best available source.

    Priority:
    1. DATA_DIRECTORY if it contains lean_explore.db directly
    2. Most recent complete timestamped extraction directory (YYYYMMDD_HHMMSS)
    3. DATA_DIRECTORY / ACTIVE_VERSION as fallback

    Only directories that contain all required extraction files are considered
    complete. Incomplete extractions (e.g. from a failed pipeline run) are
    skipped.
    """
    if (data_directory / "lean_explore.db").exists():
        return data_directory

    timestamped_dirs = _get_timestamped_directories(data_directory)
    for directory in timestamped_dirs:
        if _is_complete_extraction(directory):
            return directory

    return data_directory / active_version


class Config:
    """Application-wide configuration settings."""

    CACHE_DIRECTORY: pathlib.Path = pathlib.Path(
        os.getenv(
            "LEAN_EXPLORE_CACHE_DIR",
            pathlib.Path.home() / ".lean_explore" / "cache",
        )
    )
    """Cache directory for downloaded data (used by search engine and MCP server).

    Can be overridden with LEAN_EXPLORE_CACHE_DIR environment variable.
    Default: ~/.lean_explore/cache
    """

    DATA_DIRECTORY: pathlib.Path = _get_data_directory()
    """Local data directory for extraction pipeline output.

    Can be overridden with LEAN_EXPLORE_DATA_DIR environment variable.
    Default: <repo-root>/data
    """

    ACTIVE_VERSION: str = _get_active_cache_version()
    """Active version identifier for cached data (e.g., "2025.01.27").

    Determined by LEAN_EXPLORE_VERSION env var, ~/.lean_explore/active_version
    file, or defaults to v4.24.0.
    """

    ACTIVE_CACHE_PATH: pathlib.Path = CACHE_DIRECTORY / ACTIVE_VERSION
    """Directory for the active version's cached data files."""

    ACTIVE_DATA_PATH: pathlib.Path = _resolve_active_data_path(
        DATA_DIRECTORY, ACTIVE_VERSION
    )
    """Directory for the active version's local data files.

    Resolved using (in priority order):
    1. DATA_DIRECTORY if it contains lean_explore.db directly
    2. Most recent timestamped extraction directory (YYYYMMDD_HHMMSS)
    3. DATA_DIRECTORY / ACTIVE_VERSION as fallback
    """

    # =========================================================================
    # Timestamped Extraction Directory Methods
    # =========================================================================

    @staticmethod
    def get_latest_extraction_path() -> pathlib.Path | None:
        """Get the most recent timestamped extraction directory.

        Looks for directories matching YYYYMMDD_HHMMSS pattern in DATA_DIRECTORY.

        Returns:
            Path to most recent extraction directory, or None if none exist.
        """
        timestamped_directories = _get_timestamped_directories(Config.DATA_DIRECTORY)
        return timestamped_directories[0] if timestamped_directories else None

    DATABASE_PATH: pathlib.Path = ACTIVE_CACHE_PATH / "lean_explore.db"
    """Path to SQLite database file in cache (used by search engine)."""

    DATABASE_URL: str = f"sqlite+aiosqlite:///{DATABASE_PATH}"
    """Async SQLAlchemy database URL for SQLite (used by search engine)."""

    EXTRACTION_DATABASE_URL: str = (
        f"sqlite+aiosqlite:///{ACTIVE_DATA_PATH / 'lean_explore.db'}"
    )
    """Async SQLAlchemy database URL for extraction pipeline."""

    @staticmethod
    def create_timestamped_extraction_path() -> pathlib.Path:
        """Create a new timestamped extraction directory.

        Returns:
            Path to the newly created directory (YYYYMMDD_HHMMSS format).
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        extraction_path = Config.DATA_DIRECTORY / timestamp
        extraction_path.mkdir(parents=True, exist_ok=True)
        return extraction_path

    # =========================================================================
    # Package Workspace Paths
    # =========================================================================

    PACKAGES_ROOT: pathlib.Path = pathlib.Path(
        os.getenv(
            "LEAN_EXPLORE_PACKAGES_ROOT",
            pathlib.Path(__file__).parent.parent.parent / "lean",
        )
    )
    """Root directory for per-package Lean workspaces.

    Can be overridden with LEAN_EXPLORE_PACKAGES_ROOT environment variable.
    Default: <repo-root>/lean
    """

    R2_ASSETS_BASE_URL: str = "https://pub-48b75babc4664808b15520033423c765.r2.dev"
    """Base URL for Cloudflare R2 asset storage."""

    API_BASE_URL: str = "https://www.leanexplore.com/api/v2"
    """Base URL for the LeanExplore remote API service."""
