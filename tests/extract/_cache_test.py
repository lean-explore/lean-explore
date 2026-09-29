"""Tests for reusing results from previous extraction databases."""

import pytest

from lean_explore.extract._cache import (
    chunked,
    discover_database_files,
    load_cache_from_databases,
    split_cache_hits,
)


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


class TestDiscoverDatabaseFiles:
    """Tests for discover_database_files."""

    def test_finds_nested_databases_in_data_then_cache(self, previous_runs):
        """Databases at any depth are found, data directory first."""
        data_directory, cache_directory = previous_runs
        cached = _touch(cache_directory / "v4.23.0" / "lean_explore.db")
        data = _touch(data_directory / "v4.24.0" / "lean_explore.db")
        _touch(data_directory / "v4.24.0" / "other.db")

        assert discover_database_files() == [data, cached]

    def test_missing_directories_are_ignored(self, previous_runs, tmp_path):
        """Nonexistent data/cache directories yield no files."""
        for directory in previous_runs:
            directory.rmdir()

        assert discover_database_files() == []


class TestLoadCacheFromDatabases:
    """Tests for load_cache_from_databases."""

    def test_first_database_wins(self, tmp_path):
        """Keys found in an earlier database are not overwritten."""
        entries = {
            tmp_path / "a.db": [("x", 1), ("y", 2)],
            tmp_path / "b.db": [("x", 10), ("z", 30)],
        }

        cache = load_cache_from_databases(
            entries, lambda path: iter(entries[path]), "numbers"
        )

        assert cache == {"x": 1, "y": 2, "z": 30}

    def test_unreadable_database_is_skipped_keeping_partial_entries(self, tmp_path):
        """A failing reader keeps what it yielded and later databases still load."""

        def read_entries(path):
            if path.name == "bad.db":
                yield "partial", 0
                raise RuntimeError("corrupt database")
            yield "good", 1

        cache = load_cache_from_databases(
            [tmp_path / "bad.db", tmp_path / "good.db"], read_entries, "numbers"
        )

        assert cache == {"partial": 0, "good": 1}


class TestSplitCacheHits:
    """Tests for split_cache_hits."""

    def test_partitions_items_and_drops_none_keys(self):
        """Hits carry their cached value; items keyed None are dropped."""
        cache = {"a": 1, "c": 3}
        items = ["a", "b", "", "c"]

        hits, misses = split_cache_hits(items, cache, key=lambda s: s or None)

        assert hits == [("a", 1), ("c", 3)]
        assert misses == ["b"]


class TestChunked:
    """Tests for chunked."""

    @pytest.mark.parametrize(
        ("size", "expected"),
        [(2, [[0, 1], [2, 3], [4]]), (5, [[0, 1, 2, 3, 4]]), (10, [[0, 1, 2, 3, 4]])],
    )
    def test_slices_in_order(self, size, expected):
        """Slices cover the sequence in order with at most size items."""
        assert list(chunked([0, 1, 2, 3, 4], size)) == expected

    def test_empty_sequence(self):
        """An empty sequence yields nothing."""
        assert list(chunked([], 3)) == []
