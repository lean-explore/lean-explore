"""Tests for loading and querying the on-disk search indices."""

import shutil
from pathlib import Path

import pytest

from lean_explore.search.indexes import FAISS_NPROBE, SearchIndexes
from tests.search.conftest import DECLARATIONS, SearchData, fake_embed


def _id_of(name: str) -> int:
    """Declaration id assigned to a name by the search_data fixture."""
    return [n for n, *_ in DECLARATIONS].index(name) + 1


class TestValidation:
    """Tests for index file validation."""

    @pytest.mark.parametrize(
        "missing",
        [
            "informalization_faiss.index",
            "informalization_faiss_ids_map.json",
            "bm25_name_spaced",
            "bm25_name_raw",
            "bm25_ids_map.json",
        ],
    )
    def test_missing_file_is_reported(
        self, search_data: SearchData, tmp_path: Path, missing: str
    ):
        """Every required file is checked and the error explains how to fix it."""
        copy = tmp_path / "data"
        shutil.copytree(search_data.directory, copy)
        target = copy / missing
        shutil.rmtree(target) if target.is_dir() else target.unlink()

        with pytest.raises(FileNotFoundError, match="lean-explore data fetch"):
            SearchIndexes(copy)

    def test_faiss_path_overrides(self, search_data: SearchData, tmp_path: Path):
        """Explicit FAISS paths replace the defaults inside the data directory."""
        moved = tmp_path / "custom.index"
        shutil.copy(search_data.directory / "informalization_faiss.index", moved)

        indexes = SearchIndexes(search_data.directory, faiss_index_path=moved)

        assert indexes.faiss_index_path == moved
        assert indexes.faiss_index.ntotal > 0


class TestLazyLoading:
    """Tests that indices load only when first used."""

    def test_nothing_is_loaded_at_construction(self, search_data: SearchData):
        """Constructing the object only checks paths."""
        indexes = SearchIndexes(search_data.directory)

        assert indexes._faiss_index is None
        assert indexes._bm25_spaced is None

    def test_faiss_id_map_loads_index(self, search_data: SearchData):
        """Reading the id map loads the FAISS index alongside it."""
        indexes = SearchIndexes(search_data.directory)

        id_map = indexes.faiss_id_map

        assert len(id_map) == indexes.faiss_index.ntotal
        assert _id_of("Finset.sum_range") not in id_map


class TestNameCandidates:
    """Tests for BM25 retrieval over declaration names."""

    def test_exact_name_ranks_first(self, search_data: SearchData):
        """The raw index makes an exact full name the top candidate."""
        scores = SearchIndexes(search_data.directory).name_candidates("Nat.add", 5)

        assert max(scores, key=scores.get) == _id_of("Nat.add")

    def test_name_parts_match(self, search_data: SearchData):
        """Spaced tokenization matches individual name components."""
        scores = SearchIndexes(search_data.directory).name_candidates("hash map", 5)
        top_two = sorted(scores, key=scores.get, reverse=True)[:2]

        assert set(top_two) == {_id_of("Std.HashMap"), _id_of("Std.HashMap.insert")}


class TestSemanticCandidates:
    """Tests for FAISS retrieval over informalization embeddings."""

    def test_nearest_informalization_ranks_first(self, search_data: SearchData):
        """The query closest to an informalization retrieves that declaration."""
        indexes = SearchIndexes(search_data.directory)
        query = "Keeps the elements of a list satisfying a predicate."

        scores = indexes.semantic_candidates(fake_embed(query).tolist(), 3)

        assert max(scores, key=scores.get) == _id_of("List.filter")
        assert len(scores) == 3

    def test_unnormalized_query_is_normalized(self, search_data: SearchData):
        """Query vectors are L2-normalized, so similarities stay cosine."""
        indexes = SearchIndexes(search_data.directory)
        vector = (fake_embed("prime number") * 10).tolist()

        scores = indexes.semantic_candidates(vector, 3)

        assert max(scores.values()) <= 1.0 + 1e-6

    def test_invalid_positions_are_skipped(self, search_data: SearchData):
        """Asking for more neighbours than vectors skips FAISS's -1 padding."""
        indexes = SearchIndexes(search_data.directory)

        scores = indexes.semantic_candidates(fake_embed("prime").tolist(), 1000)

        assert len(scores) == indexes.faiss_index.ntotal

    def test_ivf_index_gets_nprobe(self, search_data: SearchData):
        """IVF indices are searched with the configured number of probes."""
        import numpy as np

        class FakeIvfIndex:
            nprobe = 1

            def search(self, vectors, k):
                self.nprobe_at_search = self.nprobe
                return np.array([[0.5]]), np.array([[0]])

        indexes = SearchIndexes(search_data.directory)
        ivf = FakeIvfIndex()
        indexes._faiss_index = ivf
        indexes._faiss_id_map = [7]

        scores = indexes.semantic_candidates(fake_embed("prime").tolist(), 1)

        assert ivf.nprobe_at_search == FAISS_NPROBE
        assert scores == {7: 0.5}
