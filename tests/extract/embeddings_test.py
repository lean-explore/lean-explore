"""Tests for embedding generation.

The pipeline tests run generate_embeddings against SQLite database files with a
fake embedding client and check what ends up stored in the database.
"""

import struct

import pytest

from lean_explore.extract import embeddings
from lean_explore.extract.embeddings import (
    _deserialize_embedding,
    _load_embedding_caches,
    generate_embeddings,
)
from tests.extract.builders import (
    FakeEmbeddingClient,
    create_database,
    fake_vector,
    load_by_name,
    make_declaration,
    write_database,
)

CACHED_VECTOR = [0.5, -0.25, 0.125, 2.0]


@pytest.fixture
def embedder(monkeypatch) -> FakeEmbeddingClient:
    """Install a fake local embedding client and record its constructor args."""
    client = FakeEmbeddingClient()

    def create(**kwargs):
        client.init_kwargs = kwargs
        return client

    monkeypatch.setattr(embeddings, "EmbeddingClient", create)
    return client


@pytest.fixture
def no_embedder(monkeypatch):
    """Fail the test if an embedding client is created."""

    def fail(**kwargs):
        raise AssertionError("EmbeddingClient should not be created")

    monkeypatch.setattr(embeddings, "EmbeddingClient", fail)


async def _embed(tmp_path, declarations, **kwargs) -> dict:
    """Run generate_embeddings on a fresh database; return stored rows."""
    engine = await create_database(tmp_path / "run" / "lean_explore.db", declarations)
    await generate_embeddings(engine, model_name="test-model", **kwargs)
    stored = await load_by_name(engine)
    await engine.dispose()
    return stored


def _informalized(*names: str) -> list:
    return [make_declaration(n, informalization=f"About {n}.") for n in names]


class TestEmbeddingCacheLoading:
    """Tests for loading embeddings from previous databases."""

    def test_deserialize_embedding(self):
        """Packed float32 bytes decode to floats."""
        data = struct.pack("3f", 1.0, -2.5, 0.25)

        assert _deserialize_embedding(data) == [1.0, -2.5, 0.25]

    def test_first_database_wins_and_rows_without_embedding_are_ignored(self, tmp_path):
        """The cache maps informalization text to the first stored embedding."""
        first = write_database(
            tmp_path / "a.db",
            [
                make_declaration("A", informalization="Text A.", embedding=[1.0]),
                make_declaration("B", informalization="Text B."),
            ],
        )
        second = write_database(
            tmp_path / "b.db",
            [make_declaration("A2", informalization="Text A.", embedding=[9.0])],
        )

        caches = _load_embedding_caches([first, second])

        assert list(caches.by_informalization) == ["Text A."]
        assert _deserialize_embedding(caches.by_informalization["Text A."]) == [1.0]

    def test_corrupt_database_is_skipped(self, tmp_path):
        """Unreadable databases are skipped."""
        corrupt = tmp_path / "corrupt.db"
        corrupt.write_text("not a database")

        assert _load_embedding_caches([corrupt]).by_informalization == {}


class TestGenerateEmbeddings:
    """Tests for the embedding pipeline."""

    async def test_embeds_in_batches(self, tmp_path, previous_runs, embedder):
        """Informalizations are embedded batch_size at a time and stored."""
        stored = await _embed(
            tmp_path, _informalized("A", "B", "C"), batch_size=2, max_seq_length=64
        )

        assert [len(batch) for batch in embedder.batches] == [2, 1]
        assert embedder.init_kwargs == {"model_name": "test-model", "max_length": 64}
        for name, declaration in stored.items():
            assert declaration.informalization_embedding == pytest.approx(
                fake_vector(f"About {name}.")
            )

    async def test_skips_declarations_without_informalization_or_with_embedding(
        self, tmp_path, previous_runs, embedder
    ):
        """Only informalized declarations lacking an embedding are embedded."""
        declarations = [
            make_declaration("Raw"),
            make_declaration("Done", informalization="Done.", embedding=[7.0]),
            *_informalized("New"),
        ]

        stored = await _embed(tmp_path, declarations)

        assert embedder.batches == [["About New."]]
        assert stored["Raw"].informalization_embedding is None
        assert stored["Done"].informalization_embedding == [7.0]

    async def test_reuses_embeddings_by_informalization_text(
        self, tmp_path, previous_runs, embedder
    ):
        """Embeddings of identical text in previous runs are copied, not recomputed."""
        data_directory, _ = previous_runs
        write_database(
            data_directory / "lean_explore.db",
            [
                make_declaration(
                    "Old", informalization="About A.", embedding=CACHED_VECTOR
                )
            ],
        )

        stored = await _embed(tmp_path, _informalized("A", "B"))

        assert embedder.batches == [["About B."]]
        assert stored["A"].informalization_embedding == pytest.approx(CACHED_VECTOR)

    async def test_all_cached_creates_no_client(
        self, tmp_path, previous_runs, no_embedder
    ):
        """When every embedding is cached, no model is loaded."""
        data_directory, _ = previous_runs
        write_database(
            data_directory / "lean_explore.db",
            [make_declaration("A", informalization="About A.", embedding=[1.0])],
        )

        stored = await _embed(tmp_path, _informalized("A"))

        assert stored["A"].informalization_embedding == [1.0]

    async def test_nothing_to_do(self, tmp_path, previous_runs, no_embedder):
        """A database without pending declarations is left alone."""
        stored = await _embed(tmp_path, [make_declaration("Raw")])

        assert stored["Raw"].informalization_embedding is None

    async def test_limit_caps_declarations_embedded(
        self, tmp_path, previous_runs, embedder
    ):
        """Only `limit` declarations are embedded."""
        stored = await _embed(tmp_path, _informalized("A", "B", "C"), limit=2)

        embedded = [d for d in stored.values() if d.informalization_embedding]
        assert len(embedded) == 2

    async def test_embedding_server_url_uses_remote_client(
        self, tmp_path, previous_runs, no_embedder, monkeypatch
    ):
        """With a server URL, embeddings are delegated to RemoteEmbeddingClient."""
        from lean_explore.util import remote_embedding_client

        client = FakeEmbeddingClient()
        server_urls = []

        def create_remote(server_url):
            server_urls.append(server_url)
            return client

        monkeypatch.setattr(
            remote_embedding_client, "RemoteEmbeddingClient", create_remote
        )

        stored = await _embed(
            tmp_path, _informalized("A"), embedding_server_url="http://gpu:8001"
        )

        assert server_urls == ["http://gpu:8001"]
        assert stored["A"].informalization_embedding == pytest.approx(
            fake_vector("About A.")
        )
