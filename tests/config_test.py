"""Tests for configuration defaults and data directory resolution."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from lean_explore import config

REQUIRED_FILES = (
    "lean_explore.db",
    "informalization_faiss.index",
    "informalization_faiss_ids_map.json",
    "bm25_ids_map.json",
    "bm25_name_raw",
    "bm25_name_spaced",
)


def load_fresh_config() -> ModuleType:
    """Execute config.py as a new module so settings reflect the current env."""
    spec = importlib.util.spec_from_file_location("fresh_config", config.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_extraction(directory: Path, complete: bool = True) -> Path:
    """Create an extraction directory, optionally with every required file."""
    directory.mkdir(parents=True)
    for name in REQUIRED_FILES if complete else REQUIRED_FILES[:1]:
        (directory / name).touch()
    return directory


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the home directory at a temp dir and clear config env vars."""
    for variable in (
        "LEAN_EXPLORE_VERSION",
        "LEAN_EXPLORE_CACHE_DIR",
        "LEAN_EXPLORE_DATA_DIR",
        "LEAN_EXPLORE_PACKAGES_ROOT",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


class TestActiveVersion:
    """Tests for choosing the active data version."""

    def test_environment_variable_wins(self, home: Path, monkeypatch):
        """LEAN_EXPLORE_VERSION overrides the version file."""
        (home / ".lean_explore").mkdir()
        (home / ".lean_explore" / "active_version").write_text("from-file")
        monkeypatch.setenv("LEAN_EXPLORE_VERSION", "from-env")

        assert config._get_active_cache_version() == "from-env"

    def test_version_file(self, home: Path):
        """The version written by `data fetch` is used, stripped of whitespace."""
        (home / ".lean_explore").mkdir()
        (home / ".lean_explore" / "active_version").write_text("20260127_103630\n")

        assert config._get_active_cache_version() == "20260127_103630"

    def test_fallback(self, home: Path):
        """Without env var or file, the built-in default is used."""
        assert config._get_active_cache_version() == "v4.24.0"


class TestDataDirectoryResolution:
    """Tests for locating locally extracted data."""

    def test_timestamped_directories_sorted_newest_first(self, tmp_path: Path):
        """Only YYYYMMDD_HHMMSS directories are listed, newest first."""
        for name in ("20250101_000000", "20260101_000000", "notes", "2026"):
            (tmp_path / name).mkdir()
        (tmp_path / "20270101_000000").touch()

        found = config._get_timestamped_directories(tmp_path)

        assert [d.name for d in found] == ["20260101_000000", "20250101_000000"]

    def test_missing_data_directory(self, tmp_path: Path):
        """A data directory that does not exist has no extractions."""
        assert config._get_timestamped_directories(tmp_path / "missing") == []

    def test_database_in_data_directory_wins(self, tmp_path: Path):
        """A database directly in the data directory takes priority."""
        (tmp_path / "lean_explore.db").touch()
        make_extraction(tmp_path / "20260101_000000")

        assert config._resolve_active_data_path(tmp_path, "v1") == tmp_path

    def test_newest_complete_extraction(self, tmp_path: Path):
        """Incomplete newer extractions are skipped."""
        older = make_extraction(tmp_path / "20250101_000000")
        make_extraction(tmp_path / "20260101_000000", complete=False)

        assert config._resolve_active_data_path(tmp_path, "v1") == older

    def test_version_fallback(self, tmp_path: Path):
        """Without any extraction, the version subdirectory is used."""
        assert config._resolve_active_data_path(tmp_path, "v1") == tmp_path / "v1"


class TestConfigClass:
    """Tests for the Config settings computed at import time."""

    def test_defaults(self, home: Path):
        """Cache paths live under ~/.lean_explore/cache/<version>."""
        fresh = load_fresh_config().Config

        assert fresh.CACHE_DIRECTORY == home / ".lean_explore" / "cache"
        assert fresh.ACTIVE_CACHE_PATH == fresh.CACHE_DIRECTORY / "v4.24.0"
        assert fresh.DATABASE_URL == (
            f"sqlite+aiosqlite:///{fresh.ACTIVE_CACHE_PATH / 'lean_explore.db'}"
        )

    def test_environment_overrides(self, home: Path, monkeypatch):
        """Cache, data, and package directories can be set by env vars."""
        monkeypatch.setenv("LEAN_EXPLORE_CACHE_DIR", str(home / "cache"))
        monkeypatch.setenv("LEAN_EXPLORE_DATA_DIR", str(home / "data"))
        monkeypatch.setenv("LEAN_EXPLORE_PACKAGES_ROOT", str(home / "lean"))
        make_extraction(home / "data" / "20260101_000000")

        fresh = load_fresh_config().Config

        assert fresh.CACHE_DIRECTORY == home / "cache"
        assert fresh.ACTIVE_DATA_PATH == home / "data" / "20260101_000000"
        assert fresh.EXTRACTION_DATABASE_URL.endswith(
            "data/20260101_000000/lean_explore.db"
        )
        assert fresh.PACKAGES_ROOT == home / "lean"

    def test_latest_and_new_extraction_paths(self, home: Path, monkeypatch):
        """New extraction directories are created and become the latest."""
        fresh = load_fresh_config().Config
        monkeypatch.setattr(fresh, "DATA_DIRECTORY", home / "data")

        assert fresh.get_latest_extraction_path() is None
        created = fresh.create_timestamped_extraction_path()

        assert created.is_dir()
        assert fresh.get_latest_extraction_path() == created
