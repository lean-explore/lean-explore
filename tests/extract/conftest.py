"""Fixtures for extraction pipeline tests."""

from pathlib import Path

import pytest

from lean_explore.config import Config


@pytest.fixture
def previous_runs(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """Point Config's data and cache directories at empty temporary directories.

    Returns:
        Tuple of (data_directory, cache_directory) where tests can place
        lean_explore.db files from "previous extraction runs".
    """
    data_directory = tmp_path / "data"
    cache_directory = tmp_path / "cache"
    data_directory.mkdir()
    cache_directory.mkdir()
    monkeypatch.setattr(Config, "DATA_DIRECTORY", data_directory)
    monkeypatch.setattr(Config, "CACHE_DIRECTORY", cache_directory)
    return data_directory, cache_directory
