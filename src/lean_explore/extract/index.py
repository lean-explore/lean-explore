"""Build search indices from declaration data.

This module creates:
1. FAISS IVF index for semantic search from embeddings
2. BM25 indices for lexical search on declaration names

IVF (Inverted File) uses k-means clustering for efficient approximate
nearest neighbor search with controllable recall.
"""

import json
import logging
from pathlib import Path

import bm25s
import faiss
import numpy as np
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import Session

from lean_explore.config import Config
from lean_explore.models import Declaration
from lean_explore.search.tokenization import tokenize_raw, tokenize_spaced

logger = logging.getLogger(__name__)

EMBEDDING_FIELDS = ["informalization_embedding"]
"""Declaration columns to build FAISS indices for."""


def _get_device() -> str:
    """Detect if CUDA GPU is available for FAISS.

    Returns:
        Device string: 'cuda' if CUDA GPU available, otherwise 'cpu'.
        Note: FAISS doesn't support MPS, so Apple Silicon uses CPU.
    """
    if faiss.get_num_gpus() > 0:
        device = "cuda"
        logger.info("Using CUDA GPU for FAISS")
    else:
        device = "cpu"
        logger.info("Using CPU for FAISS")
    return device


def _create_sync_engine(engine: AsyncEngine) -> Engine:
    """Create a sync engine for the same SQLite database as an async engine.

    Sync access avoids aiosqlite issues with binary data.

    Args:
        engine: Async engine using the sqlite+aiosqlite driver.

    Returns:
        Sync engine using the default sqlite driver.
    """
    return create_engine(str(engine.url).replace("sqlite+aiosqlite", "sqlite"))


def _resolve_output_directory(output_directory: Path | None) -> Path:
    """Return the output directory (default: active data path), creating it.

    Args:
        output_directory: Requested directory, or None for the default.

    Returns:
        Existing output directory.
    """
    if output_directory is None:
        output_directory = Config.ACTIVE_DATA_PATH
    output_directory.mkdir(parents=True, exist_ok=True)
    return output_directory


def _write_ids_map(path: Path, declaration_ids: list[int]) -> None:
    """Write the index-position to declaration-ID mapping as JSON.

    Args:
        path: Output file path.
        declaration_ids: Declaration IDs in index order.
    """
    with open(path, "w") as file:
        json.dump(declaration_ids, file)


# --- FAISS ---


def _load_embeddings_from_database(
    session: Session, embedding_field: str
) -> tuple[list[int], np.ndarray]:
    """Load embeddings and IDs from the database.

    Args:
        session: Sync database session.
        embedding_field: Name of the embedding field to load
            (e.g., 'informalization_embedding').

    Returns:
        Tuple of (declaration_ids, embeddings_array) where embeddings_array
        is a numpy array of shape (num_declarations, embedding_dimension).
    """
    column = getattr(Declaration, embedding_field)
    stmt = select(Declaration.id, column).where(column.isnot(None))
    rows = list(session.execute(stmt).all())

    if not rows:
        logger.warning("No declarations found with %s", embedding_field)
        return [], np.array([])

    declaration_ids = [row.id for row in rows]
    embeddings_array = np.array([row[1] for row in rows], dtype=np.float32)

    logger.info(
        "Loaded %d embeddings with dimension %d",
        len(declaration_ids),
        embeddings_array.shape[1],
    )

    return declaration_ids, embeddings_array


def _build_faiss_index(embeddings: np.ndarray, device: str) -> faiss.Index:
    """Build a FAISS IVF index from embeddings.

    Args:
        embeddings: Numpy array of embeddings, shape (num_vectors, dimension).
        device: Device to use ('cuda', 'mps', or 'cpu').

    Returns:
        FAISS IVF index for fast approximate nearest neighbor search.
    """
    num_vectors = embeddings.shape[0]
    dimension = embeddings.shape[1]

    # Number of clusters: sqrt(n) is a good heuristic, minimum 256
    nlist = max(256, int(np.sqrt(num_vectors)))

    logger.info(
        "Building FAISS IVF index for %d vectors with %d clusters...",
        num_vectors,
        nlist,
    )

    # Use inner product (cosine similarity on normalized vectors)
    quantizer = faiss.IndexFlatIP(dimension)
    index = faiss.IndexIVFFlat(quantizer, dimension, nlist, faiss.METRIC_INNER_PRODUCT)

    if device == "cuda" and faiss.get_num_gpus() > 0:
        logger.info("Training IVF index on GPU")
        resource = faiss.StandardGpuResources()
        gpu_index = faiss.index_cpu_to_gpu(resource, 0, index)
        gpu_index.train(embeddings)
        gpu_index.add(embeddings)
        index = faiss.index_gpu_to_cpu(gpu_index)
    else:
        logger.info("Training IVF index on CPU")
        index.train(embeddings)
        index.add(embeddings)

    logger.info("FAISS IVF index built successfully")
    return index


def _build_and_save_faiss_index(
    session: Session, embedding_field: str, device: str, output_directory: Path
) -> None:
    """Build the FAISS index for one embedding column and save it with its ID map.

    Writes ``<field>_faiss.index`` and ``<field>_faiss_ids_map.json`` (with the
    ``_embedding`` suffix dropped from the field name). Skips empty columns.

    Args:
        session: Sync database session.
        embedding_field: Declaration column holding the embeddings.
        device: Device to build on ('cuda' or 'cpu').
        output_directory: Directory to write files to.
    """
    declaration_ids, embeddings = _load_embeddings_from_database(
        session, embedding_field
    )
    if len(declaration_ids) == 0:
        logger.warning("Skipping %s (no data)", embedding_field)
        return

    index = _build_faiss_index(embeddings, device)

    # Move GPU index back to CPU for serialization
    if device == "cuda" and isinstance(index, faiss.GpuIndex):
        index = faiss.index_gpu_to_cpu(index)

    index_path = output_directory / embedding_field.replace(
        "_embedding", "_faiss.index"
    )
    faiss.write_index(index, str(index_path))
    logger.info("Saved FAISS index to %s", index_path)

    ids_map_path = output_directory / embedding_field.replace(
        "_embedding", "_faiss_ids_map.json"
    )
    _write_ids_map(ids_map_path, declaration_ids)
    logger.info("Saved ID mapping to %s", ids_map_path)


async def build_faiss_indices(
    engine: AsyncEngine,
    output_directory: Path | None = None,
) -> None:
    """Build FAISS index for informalization embeddings.

    This function creates a FAISS IVF index for informalization embeddings
    and saves it to disk along with ID mappings.

    Args:
        engine: Async database engine (URL extracted for sync access).
        output_directory: Directory to save indices. Defaults to active data path.
    """
    output_directory = _resolve_output_directory(output_directory)
    logger.info("Saving indices to %s", output_directory)

    device = _get_device()
    sync_engine = _create_sync_engine(engine)

    with Session(sync_engine) as session:
        for i, embedding_field in enumerate(EMBEDDING_FIELDS, 1):
            logger.info(
                "Processing %s (%d/%d)...", embedding_field, i, len(EMBEDDING_FIELDS)
            )
            _build_and_save_faiss_index(
                session, embedding_field, device, output_directory
            )

    sync_engine.dispose()
    logger.info("All FAISS indices built successfully")


# --- BM25 ---


def _load_declaration_names(session: Session) -> tuple[list[int], list[str]]:
    """Load all declaration IDs and names from the database.

    Args:
        session: Sync database session.

    Returns:
        Tuple of (declaration_ids, declaration_names).
    """
    rows = list(session.execute(select(Declaration.id, Declaration.name)).all())

    declaration_ids = [row.id for row in rows]
    declaration_names = [row.name or "" for row in rows]

    logger.info("Loaded %d declarations for BM25 indexing", len(declaration_ids))
    return declaration_ids, declaration_names


def _build_bm25_indices(
    declaration_names: list[str],
) -> tuple[bm25s.BM25, bm25s.BM25]:
    """Build BM25 indices over declaration names.

    Creates two indices:
    1. Spaced tokenization (splits on dots, underscores, camelCase)
    2. Raw tokenization (full name as single token)

    Args:
        declaration_names: List of declaration names.

    Returns:
        Tuple of (bm25_spaced, bm25_raw) indices.
    """
    logger.info("Building BM25 indices over declaration names...")

    corpus_spaced = [list(set(tokenize_spaced(n))) for n in declaration_names]
    corpus_raw = [list(set(tokenize_raw(n))) for n in declaration_names]

    bm25_spaced = bm25s.BM25(method="bm25+")
    bm25_spaced.index(corpus_spaced)
    logger.info("Built BM25 spaced index")

    bm25_raw = bm25s.BM25(method="bm25+")
    bm25_raw.index(corpus_raw)
    logger.info("Built BM25 raw index")

    return bm25_spaced, bm25_raw


async def build_bm25_indices(
    engine: AsyncEngine,
    output_directory: Path | None = None,
) -> None:
    """Build BM25 indices for declaration name search.

    This function creates BM25 indices for lexical search on declaration
    names and saves them to disk along with ID mappings.

    Args:
        engine: Async database engine (URL extracted for sync access).
        output_directory: Directory to save indices. Defaults to active data path.
    """
    output_directory = _resolve_output_directory(output_directory)
    logger.info("Saving BM25 indices to %s", output_directory)

    sync_engine = _create_sync_engine(engine)
    with Session(sync_engine) as session:
        declaration_ids, declaration_names = _load_declaration_names(session)
    sync_engine.dispose()

    if not declaration_ids:
        logger.warning("No declarations found for BM25 indexing")
        return

    bm25_spaced, bm25_raw = _build_bm25_indices(declaration_names)

    bm25_spaced_path = output_directory / "bm25_name_spaced"
    bm25_spaced.save(str(bm25_spaced_path))
    logger.info("Saved BM25 spaced index to %s", bm25_spaced_path)

    bm25_raw_path = output_directory / "bm25_name_raw"
    bm25_raw.save(str(bm25_raw_path))
    logger.info("Saved BM25 raw index to %s", bm25_raw_path)

    # Shared by both indices
    ids_map_path = output_directory / "bm25_ids_map.json"
    _write_ids_map(ids_map_path, declaration_ids)
    logger.info("Saved BM25 ID mapping to %s", ids_map_path)
    logger.info("All BM25 indices built successfully")
