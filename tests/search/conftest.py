"""A small but real search dataset for SearchEngine tests.

The fixture writes a SQLite database, BM25 name indices, and a FAISS index to a
temporary directory, exactly in the layout produced by the extraction pipeline.
Embedding and reranking use deterministic word-overlap models so rankings are
reproducible without downloading neural models.
"""

import json
import re
import zlib
from dataclasses import dataclass
from pathlib import Path

import bm25s
import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from lean_explore.models import Base, Declaration
from lean_explore.search.tokenization import tokenize_raw, tokenize_spaced
from lean_explore.util.embedding_client import EmbeddingResponse
from lean_explore.util.reranker_client import RerankerResponse

EMBEDDING_DIMENSION = 64

# (name, module, informalization, dependencies)
DECLARATIONS: list[tuple[str, str, str | None, list[str] | None]] = [
    ("Nat.add", "Init.Prelude", "**Addition.** Adds two natural numbers.", None),
    (
        "Nat.add_comm",
        "Init.Data.Nat.Basic",
        "**Commutativity of addition.** Adding natural numbers is commutative.",
        ["Nat.add"],
    ),
    (
        "Nat.add_assoc",
        "Init.Data.Nat.Basic",
        "**Associativity of addition.** Addition of natural numbers is associative.",
        ["Nat.add"],
    ),
    (
        "Nat.mul_comm",
        "Init.Data.Nat.Basic",
        "**Commutativity of multiplication.** Multiplying natural numbers is "
        "commutative.",
        ["Nat.mul"],
    ),
    ("Nat.mul", "Init.Prelude", "**Multiplication.** Multiplies natural numbers.", []),
    (
        "Nat.Prime",
        "Mathlib.Data.Nat.Prime.Defs",
        "**Prime number.** A natural number greater than one with no divisors "
        "other than one and itself.",
        ["Nat.mul"],
    ),
    (
        "Nat.Prime.two_le",
        "Mathlib.Data.Nat.Prime.Basic",
        "**Primes are at least two.** Every prime number is at least two.",
        ["Nat.Prime"],
    ),
    (
        "Nat.Prime.dvd_mul",
        "Mathlib.Data.Nat.Prime.Basic",
        "**Prime divides a product.** A prime dividing a product of natural "
        "numbers divides one of the factors.",
        ["Nat.Prime", "Nat.mul"],
    ),
    (
        "Nat.Prime.mk",
        "Mathlib.Data.Nat.Prime.Defs",
        "**Prime constructor.** Builds a proof that a number is prime.",
        ["Nat.Prime"],
    ),
    (
        "List.map",
        "Init.Data.List.Basic",
        "**Map over a list.** Applies a function to every element of a list.",
        None,
    ),
    (
        "List.length_map",
        "Init.Data.List.Lemmas",
        "**Length of a mapped list.** Mapping a function over a list preserves "
        "its length.",
        ["List.map", "List.length"],
    ),
    ("List.length", "Init.Data.List.Basic", "**List length.** Counts elements.", []),
    (
        "List.filter",
        "Init.Data.List.Basic",
        "**Filter a list.** Keeps the elements of a list satisfying a predicate.",
        None,
    ),
    (
        "Std.HashMap",
        "Std.Data.HashMap.Basic",
        "**Hash map.** A finite map from keys to values backed by a hash table.",
        None,
    ),
    (
        "Std.HashMap.insert",
        "Std.Data.HashMap.Basic",
        "**Insert into a hash map.** Adds a key and value to a hash map.",
        ["Std.HashMap"],
    ),
    (
        "ContinuousOn.isCompact_image",
        "Mathlib.Topology.Compactness.Compact",
        "**Continuous image of a compact set is compact.** A function continuous "
        "on a compact set maps it to a compact set.",
        ["IsCompact"],
    ),
    (
        "IsCompact",
        "Mathlib.Topology.Defs.Filter",
        "**Compact set.** A set is compact when every open cover has a finite "
        "subcover.",
        [],
    ),
    (
        "FLT.frey_curve",
        "FLT.Basic.FreyPackage",
        "**Frey curve.** The elliptic curve attached to a putative counterexample "
        "to Fermat's Last Theorem.",
        ["Nat.Prime"],
    ),
    (
        "Physlib.SpaceTime",
        "Physlib.Relativity.SpaceTime.Basic",
        "**Space-time.** Four dimensional Minkowski space-time.",
        None,
    ),
    ("Finset.sum_range", "Mathlib.Algebra.BigOperators.Basic", None, ["Nat.add"]),
    (
        "geom_sum_eq",
        "Mathlib.Algebra.GeomSum",
        "**Geometric series sum.** Closed form for the sum of a geometric series.",
        ["Finset.sum_range", "Nat.add", "Nat.mul"],
    ),
    (
        "Broken.dependencies",
        "Mathlib.Broken",
        "**Broken dependencies.** A row whose dependencies are not valid JSON.",
        None,
    ),
]

BROKEN_DEPENDENCIES_JSON = "not json ["


def _words(text: str) -> list[str]:
    """Lowercase alphanumeric words of a text."""
    return re.findall(r"[a-z0-9]+", text.lower())


def fake_embed(text: str) -> np.ndarray:
    """Deterministic bag-of-words embedding, L2-normalized.

    Args:
        text: Text to embed.

    Returns:
        A float32 vector of length EMBEDDING_DIMENSION.
    """
    vector = np.zeros(EMBEDDING_DIMENSION, dtype=np.float32)
    for word in _words(text):
        vector[zlib.crc32(word.encode()) % EMBEDDING_DIMENSION] += 1.0
    norm = np.linalg.norm(vector)
    return vector / norm if norm else vector


class FakeEmbeddingClient:
    """Embedding client returning deterministic bag-of-words vectors."""

    def __init__(self):
        """Initialize the call log."""
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str], is_query: bool = False):
        """Embed each text with fake_embed."""
        self.calls.append(texts)
        return EmbeddingResponse(
            texts=texts,
            embeddings=[fake_embed(text).tolist() for text in texts],
            model="fake-embedding",
        )


class FakeRerankerClient:
    """Reranker scoring documents by the fraction of query words they contain."""

    def __init__(self):
        """Initialize the call log."""
        self.calls: list[tuple[str, list[str]]] = []

    async def rerank(self, query: str, documents: list[str]):
        """Score each document by query word overlap."""
        self.calls.append((query, documents))
        query_words = set(_words(query))
        scores = [
            len(query_words & set(_words(document))) / max(len(query_words), 1)
            for document in documents
        ]
        return RerankerResponse(query=query, scores=scores, model="fake-reranker")


@dataclass
class SearchData:
    """Paths of a generated search dataset."""

    directory: Path
    db_url: str


def _write_database(path: Path) -> list[int]:
    """Write DECLARATIONS to a SQLite database and return their ids."""
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for index, (name, module, informal, dependencies) in enumerate(DECLARATIONS):
            if name == "Broken.dependencies":
                dependencies_json = BROKEN_DEPENDENCIES_JSON
            else:
                dependencies_json = json.dumps(dependencies) if dependencies else None
            session.add(
                Declaration(
                    id=index + 1,
                    name=name,
                    module=module,
                    docstring=f"Docstring for {name}.",
                    source_text=f"theorem {name} := sorry",
                    source_link=f"https://example.com/{name}",
                    dependencies=dependencies_json,
                    informalization=informal,
                )
            )
        session.commit()
    engine.dispose()
    return list(range(1, len(DECLARATIONS) + 1))


def _write_bm25_indices(directory: Path, ids: list[int]) -> None:
    """Build and save the spaced and raw BM25 name indices."""
    names = [name for name, *_ in DECLARATIONS]
    for suffix, tokenize in (("spaced", tokenize_spaced), ("raw", tokenize_raw)):
        index = bm25s.BM25(method="bm25+")
        index.index([list(set(tokenize(name))) for name in names])
        index.save(str(directory / f"bm25_name_{suffix}"))
    (directory / "bm25_ids_map.json").write_text(json.dumps(ids))


def _write_faiss_index(directory: Path, ids: list[int]) -> None:
    """Build and save an inner-product FAISS index over informalizations."""
    import faiss

    embedded = [
        (declaration_id, informal)
        for declaration_id, (_, _, informal, _) in zip(ids, DECLARATIONS)
        if informal
    ]
    vectors = np.stack([fake_embed(informal) for _, informal in embedded])
    index = faiss.IndexFlatIP(EMBEDDING_DIMENSION)
    index.add(vectors)
    faiss.write_index(index, str(directory / "informalization_faiss.index"))
    (directory / "informalization_faiss_ids_map.json").write_text(
        json.dumps([declaration_id for declaration_id, _ in embedded])
    )


@pytest.fixture(scope="session")
def search_data(tmp_path_factory: pytest.TempPathFactory) -> SearchData:
    """Generate the search dataset once per test session."""
    directory = tmp_path_factory.mktemp("search_data")
    ids = _write_database(directory / "lean_explore.db")
    _write_bm25_indices(directory, ids)
    _write_faiss_index(directory, ids)
    return SearchData(
        directory=directory,
        db_url=f"sqlite+aiosqlite:///{directory / 'lean_explore.db'}",
    )
