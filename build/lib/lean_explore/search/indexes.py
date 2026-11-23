"""On-disk retrieval indices used by the search engine.

Two independent candidate generators are loaded lazily from a data directory:

- BM25+ over declaration names, with a spaced tokenization (split on dots,
  underscores, and camelCase) and a raw tokenization (whole name as one token).
- A FAISS index over embeddings of the natural-language informalizations.

Note: On macOS, torch and FAISS have OpenMP library conflicts. FAISS is therefore
imported lazily, only after the caller has computed the query embedding (which
loads torch first).
"""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import bm25s
import numpy as np

from lean_explore.search.tokenization import tokenize_raw, tokenize_spaced

if TYPE_CHECKING:
    import faiss

logger = logging.getLogger(__name__)

FAISS_INDEX_FILENAME = "informalization_faiss.index"
FAISS_IDS_MAP_FILENAME = "informalization_faiss_ids_map.json"
BM25_SPACED_DIRNAME = "bm25_name_spaced"
BM25_RAW_DIRNAME = "bm25_name_raw"
BM25_IDS_MAP_FILENAME = "bm25_ids_map.json"

FAISS_NPROBE = 64
"""Number of inverted lists probed per query when the index is an IVF index."""


class SearchIndexes:
    """Lazily loaded BM25 name indices and FAISS informalization index."""

    def __init__(
        self,
        data_directory: Path,
        faiss_index_path: Path | None = None,
        faiss_ids_map_path: Path | None = None,
    ):
        """Locate the index files and check that they exist.

        Args:
            data_directory: Directory holding the index files.
            faiss_index_path: Override for the FAISS index file.
            faiss_ids_map_path: Override for the FAISS id mapping file.

        Raises:
            FileNotFoundError: If any required index file is missing.
        """
        self.faiss_index_path = faiss_index_path or (
            data_directory / FAISS_INDEX_FILENAME
        )
        self.faiss_ids_map_path = faiss_ids_map_path or (
            data_directory / FAISS_IDS_MAP_FILENAME
        )
        self.bm25_spaced_path = data_directory / BM25_SPACED_DIRNAME
        self.bm25_raw_path = data_directory / BM25_RAW_DIRNAME
        self.bm25_ids_map_path = data_directory / BM25_IDS_MAP_FILENAME

        self._faiss_index: faiss.Index | None = None
        self._faiss_id_map: list[int] = []
        self._bm25_spaced: bm25s.BM25 | None = None
        self._bm25_raw: bm25s.BM25 | None = None
        self._bm25_id_map: list[int] = []

        self._validate_paths()

    def _validate_paths(self) -> None:
        """Raise FileNotFoundError for the first missing index file."""
        for path in (
            self.faiss_index_path,
            self.faiss_ids_map_path,
            self.bm25_spaced_path,
            self.bm25_raw_path,
            self.bm25_ids_map_path,
        ):
            if not path.exists():
                raise FileNotFoundError(
                    f"Required file not found at {path}. "
                    "Please run 'lean-explore data fetch' to download the data."
                )

    @property
    def faiss_index(self) -> "faiss.Index":
        """The FAISS informalization index, loaded on first use."""
        if self._faiss_index is None:
            import faiss

            logger.info("Loading FAISS index from %s", self.faiss_index_path)
            self._faiss_index = faiss.read_index(str(self.faiss_index_path))
            self._faiss_id_map = json.loads(self.faiss_ids_map_path.read_text())
        return self._faiss_index

    @property
    def faiss_id_map(self) -> list[int]:
        """Declaration id for each FAISS vector position."""
        _ = self.faiss_index
        return self._faiss_id_map

    def _load_bm25(self) -> tuple[bm25s.BM25, bm25s.BM25]:
        """Return the spaced and raw BM25 indices, loading them on first use."""
        if self._bm25_spaced is None or self._bm25_raw is None:
            logger.info("Loading BM25 indices from %s", self.bm25_spaced_path.parent)
            self._bm25_spaced = bm25s.BM25.load(str(self.bm25_spaced_path))
            self._bm25_raw = bm25s.BM25.load(str(self.bm25_raw_path))
            self._bm25_id_map = json.loads(self.bm25_ids_map_path.read_text())
            logger.info("BM25 indices loaded (%d declarations)", len(self._bm25_id_map))
        return self._bm25_spaced, self._bm25_raw

    def name_candidates(self, query: str, k: int) -> dict[int, float]:
        """Retrieve candidates by BM25 on declaration names.

        Both tokenizations are queried and each declaration keeps its best score.

        Args:
            query: Search query string.
            k: Number of candidates to retrieve from each index.

        Returns:
            Map of declaration id to BM25 score.
        """
        bm25_spaced, bm25_raw = self._load_bm25()
        scores_by_id: dict[int, float] = {}
        for index, tokens in (
            (bm25_spaced, tokenize_spaced(query)),
            (bm25_raw, tokenize_raw(query)),
        ):
            positions, scores = index.retrieve([tokens], k=k)
            for position, score in zip(positions[0], scores[0]):
                declaration_id = self._bm25_id_map[position]
                scores_by_id[declaration_id] = max(
                    scores_by_id.get(declaration_id, 0.0), float(score)
                )

        logger.info("BM25 name: %d candidates", len(scores_by_id))
        return scores_by_id

    def semantic_candidates(
        self, query_embedding: list[float], k: int
    ) -> dict[int, float]:
        """Retrieve candidates by cosine similarity to informalization embeddings.

        Args:
            query_embedding: Embedding of the search query.
            k: Number of nearest neighbours to retrieve.

        Returns:
            Map of declaration id to similarity score.
        """
        import faiss

        query_vector = np.array([query_embedding], dtype=np.float32)
        faiss.normalize_L2(query_vector)

        index = self.faiss_index
        if hasattr(index, "nprobe"):
            index.nprobe = FAISS_NPROBE
        similarities, positions = index.search(query_vector, k)

        id_map = self.faiss_id_map
        scores_by_id: dict[int, float] = {}
        for position, similarity in zip(positions[0], similarities[0]):
            if position == -1 or position >= len(id_map):
                continue
            declaration_id = id_map[position]
            scores_by_id[declaration_id] = max(
                scores_by_id.get(declaration_id, 0.0), float(similarity)
            )

        logger.info("FAISS informal: %d candidates", len(scores_by_id))
        return scores_by_id
