"""Tests for building the FAISS and BM25 search indices.

FAISS k-means training crashes on macOS when torch's OpenMP runtime is already
loaded (other test modules import torch), so FAISS builds run in a fresh
interpreter that only imports the index module.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import bm25s
import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import lean_explore
from lean_explore.config import Config
from lean_explore.extract import index
from lean_explore.extract.index import (
    _load_embeddings_from_database,
    build_bm25_indices,
    build_faiss_indices,
)
from lean_explore.search.tokenization import tokenize_raw, tokenize_spaced
from tests.extract.builders import (
    create_database,
    make_declaration,
    sqlite_url,
    write_database,
)

NAMES = ["Nat.add", "Nat.add_comm", "List.map", "List.foldl", "Real.sqrt"]

FAISS_SCRIPT = """
import asyncio, json, sys
from pathlib import Path
import faiss, numpy as np
from sqlalchemy.ext.asyncio import create_async_engine
from lean_explore.extract.index import build_faiss_indices

faiss.get_num_gpus = lambda: 0  # Some macOS wheels report a GPU but lack GpuIndex
url, out, query = sys.argv[1], Path(sys.argv[2]), json.loads(sys.argv[3])
asyncio.run(build_faiss_indices(create_async_engine(url), output_directory=out))
built = faiss.read_index(str(out / "informalization_faiss.index"))
built.nprobe = 256
_, positions = built.search(np.array([query], dtype=np.float32), 1)
print(json.dumps({"ntotal": built.ntotal, "d": built.d, "top": int(positions[0][0])}))
"""


async def _bm25_output(tmp_path: Path, names: list[str]) -> Path:
    engine = await create_database(
        tmp_path / "search.db", [make_declaration(n) for n in names]
    )
    output = tmp_path / "out"
    await build_bm25_indices(engine, output_directory=output)
    await engine.dispose()
    return output


def _bm25_ranking(output: Path, index_name: str, tokens: list[str]) -> list[str]:
    """Rank all NAMES with a saved BM25 index (declaration IDs are 1-based)."""
    ids = json.loads((output / "bm25_ids_map.json").read_text())
    loaded = bm25s.BM25.load(str(output / index_name))
    positions, _ = loaded.retrieve([tokens], k=len(ids))
    return [NAMES[ids[position] - 1] for position in positions[0]]


def _embedded_declarations(count: int, dimension: int = 8) -> list:
    vectors = np.random.default_rng(0).random((count, dimension), dtype=np.float32)
    # Unit vectors: under inner product each vector is its own nearest neighbor
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    return [
        make_declaration(f"D{i}", embedding=vectors[i].tolist()) for i in range(count)
    ]


class TestBuildBm25Indices:
    """Tests for build_bm25_indices."""

    async def test_spaced_index_round_trip(self, tmp_path):
        """Saved spaced index finds names by their dotted/underscored parts."""
        output = await _bm25_output(tmp_path, NAMES)

        ranking = _bm25_ranking(output, "bm25_name_spaced", tokenize_spaced("add comm"))

        assert ranking[:2] == ["Nat.add_comm", "Nat.add"]

    async def test_raw_index_matches_full_names_only(self, tmp_path):
        """Saved raw index ranks the exact full name first."""
        output = await _bm25_output(tmp_path, NAMES)

        ranking = _bm25_ranking(output, "bm25_name_raw", tokenize_raw("List.map"))

        assert ranking[0] == "List.map"

    async def test_ids_map_covers_every_declaration(self, tmp_path):
        """The ID map lists each declaration ID once."""
        output = await _bm25_output(tmp_path, NAMES)

        ids = json.loads((output / "bm25_ids_map.json").read_text())

        assert sorted(ids) == list(range(1, len(NAMES) + 1))

    async def test_empty_database_writes_nothing(self, tmp_path):
        """No declarations means no index files."""
        output = await _bm25_output(tmp_path, [])

        assert list(output.iterdir()) == []

    async def test_defaults_to_active_data_path(self, tmp_path, monkeypatch):
        """Without an output directory, indices go to Config.ACTIVE_DATA_PATH."""
        monkeypatch.setattr(Config, "ACTIVE_DATA_PATH", tmp_path / "active")
        engine = await create_database(tmp_path / "search.db", [make_declaration("A")])

        await build_bm25_indices(engine)
        await engine.dispose()

        assert (tmp_path / "active" / "bm25_ids_map.json").exists()


class TestLoadEmbeddings:
    """Tests for _load_embeddings_from_database."""

    def _load(self, tmp_path, declarations):
        path = write_database(tmp_path / "search.db", declarations)
        engine = create_engine(sqlite_url(path, async_driver=False))
        with Session(engine) as session:
            loaded = _load_embeddings_from_database(
                session, "informalization_embedding"
            )
        engine.dispose()
        return loaded

    def test_loads_only_rows_with_embeddings(self, tmp_path):
        """IDs and a float32 matrix are returned for embedded rows."""
        declarations = [
            make_declaration("A", embedding=[1.0, 2.0]),
            make_declaration("B"),
            make_declaration("C", embedding=[3.0, 4.0]),
        ]

        ids, matrix = self._load(tmp_path, declarations)

        assert ids == [1, 3]
        assert matrix.dtype == np.float32
        assert matrix.tolist() == [[1.0, 2.0], [3.0, 4.0]]

    def test_no_embeddings(self, tmp_path):
        """Without embeddings an empty result is returned."""
        ids, matrix = self._load(tmp_path, [make_declaration("A")])

        assert ids == []
        assert matrix.shape == (0,)


class TestBuildFaissIndices:
    """Tests for build_faiss_indices."""

    @pytest.mark.parametrize(("gpus", "device"), [(0, "cpu"), (1, "cuda")])
    def test_get_device(self, monkeypatch, gpus, device):
        """CUDA is used only when FAISS sees a GPU."""
        monkeypatch.setattr(index.faiss, "get_num_gpus", lambda: gpus)

        assert index._get_device() == device

    async def test_empty_database_writes_nothing(self, tmp_path):
        """Without embeddings no FAISS files are written."""
        engine = await create_database(tmp_path / "search.db", [make_declaration("A")])
        output = tmp_path / "out"

        await build_faiss_indices(engine, output_directory=output)
        await engine.dispose()

        assert list(output.iterdir()) == []

    def test_build_and_search_round_trip(self, tmp_path):
        """A saved IVF index over 300 vectors finds a stored vector as its top hit.

        Runs in a subprocess; see the module docstring.
        """
        declarations = _embedded_declarations(300)
        query = declarations[42].informalization_embedding
        path = write_database(tmp_path / "search.db", declarations)
        output = tmp_path / "out"
        output.mkdir()

        env = {**os.environ, "PYTHONPATH": str(Path(lean_explore.__file__).parents[1])}
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                FAISS_SCRIPT,
                sqlite_url(path),
                str(output),
                json.dumps(query),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        ids = json.loads((output / "informalization_faiss_ids_map.json").read_text())
        assert (result["ntotal"], result["d"]) == (300, 8)
        assert ids == list(range(1, 301))
        assert ids[result["top"]] == 43
