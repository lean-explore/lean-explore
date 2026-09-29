"""Pipeline orchestration for Lean declaration extraction and enrichment.

This module provides functions to coordinate the complete data extraction pipeline:
1. Extract declarations from doc-gen4 output
2. Generate informal natural language descriptions
3. Generate vector embeddings for semantic search
4. Build FAISS indices for vector similarity search

Layers: CLI resolution (``resolve_*``), orchestration (``run_pipeline``), steps.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import click
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lean_explore.config import Config
from lean_explore.models import Base
from lean_explore.util.logging import setup_logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineSteps:
    """Which enrichment steps of the pipeline to run.

    Attributes:
        parse_docs: Parse doc-gen4 output into the database.
        informalize: Generate informal natural language descriptions.
        embeddings: Generate vector embeddings.
        index: Build FAISS and BM25 search indices.
    """

    parse_docs: bool
    informalize: bool
    embeddings: bool
    index: bool

    def enabled_names(self) -> list[str]:
        """Return the CLI names of the enabled steps, in execution order."""
        flags = {
            "parse-docs": self.parse_docs,
            "informalize": self.informalize,
            "embeddings": self.embeddings,
            "index": self.index,
        }
        return [name for name, enabled in flags.items() if enabled]


@dataclass(frozen=True)
class InformalizeSettings:
    """Settings for the informalization step.

    Attributes:
        model: LLM model used to generate informalizations.
        batch_size: Number of results committed per database batch.
        max_concurrent: Maximum concurrent LLM requests.
        limit: Maximum number of declarations to process, or None for all.
    """

    model: str
    batch_size: int
    max_concurrent: int
    limit: int | None


@dataclass(frozen=True)
class EmbeddingSettings:
    """Settings for the embeddings step.

    Attributes:
        model_name: Sentence transformer model name.
        batch_size: Batch size for embedding generation.
        limit: Maximum number of declarations to process, or None for all.
        max_seq_length: Maximum token sequence length.
        server_url: Running backend to delegate embedding generation to, if any.
    """

    model_name: str
    batch_size: int
    limit: int | None
    max_seq_length: int
    server_url: str | None


def resolve_steps(
    run_doc_gen4: bool,
    parse_docs: bool | None,
    informalize: bool | None,
    embeddings: bool | None,
    index: bool | None,
) -> PipelineSteps:
    """Resolve tri-state CLI step flags into a concrete step selection.

    If no step flag was given and ``--run-doc-gen4`` was not passed, every
    step runs. Otherwise only the explicitly enabled steps run, so a lone
    ``--no-<step>`` flag disables every step.

    Args:
        run_doc_gen4: Whether ``--run-doc-gen4`` was passed.
        parse_docs: ``--parse-docs`` flag value, or None when not given.
        informalize: ``--informalize`` flag value, or None when not given.
        embeddings: ``--embeddings`` flag value, or None when not given.
        index: ``--index`` flag value, or None when not given.

    Returns:
        The resolved step selection.
    """
    flags = (parse_docs, informalize, embeddings, index)
    if not run_doc_gen4 and all(flag is None for flag in flags):
        return PipelineSteps(True, True, True, True)
    return PipelineSteps(*(bool(flag) for flag in flags))


def resolve_extraction_path(create_new: bool) -> Path:
    """Choose the extraction directory for this run.

    Args:
        create_new: Create a new timestamped directory (used when parsing
            docs); otherwise reuse the latest existing extraction.

    Returns:
        Path to the extraction directory.

    Raises:
        click.ClickException: If reusing and no extraction exists yet.
    """
    if create_new:
        new_path = Config.create_timestamped_extraction_path()
        logger.info("Created new extraction directory: %s", new_path)
        return new_path

    extraction_path = Config.get_latest_extraction_path()
    if extraction_path is None:
        raise click.ClickException(
            "No existing extraction found. Run with --parse-docs first."
        )
    logger.info("Using existing extraction: %s", extraction_path)
    return extraction_path


def database_url_for(extraction_path: Path) -> str:
    """Return the async SQLite URL of ``lean_explore.db`` in an extraction."""
    return f"sqlite+aiosqlite:///{extraction_path / 'lean_explore.db'}"


def _require_openrouter_key() -> None:
    """Fail fast when the OpenRouter API key needed to informalize is missing.

    Raises:
        RuntimeError: If ``OPENROUTER_API_KEY`` is not set.
    """
    if not os.getenv("OPENROUTER_API_KEY"):
        logger.error(
            "OPENROUTER_API_KEY environment variable is required for informalization"
        )
        raise RuntimeError("OPENROUTER_API_KEY not set")


async def _create_database_schema(engine: AsyncEngine) -> None:
    """Create database tables if they don't exist.

    Args:
        engine: SQLAlchemy async engine instance.
    """
    logger.info("Creating database schema...")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    logger.info("Database schema created successfully")


async def _run_doc_gen4_step(fresh: bool = False) -> None:
    """Run doc-gen4 to generate documentation.

    Args:
        fresh: Clear cached dependencies to force fresh resolution.
    """
    from lean_explore.extract.doc_gen4 import run_doc_gen4

    logger.info("Running doc-gen4...")
    await run_doc_gen4(fresh=fresh)
    logger.info("doc-gen4 complete")


async def _run_extract_step(engine: AsyncEngine) -> None:
    """Extract declarations from doc-gen4 output.

    Args:
        engine: SQLAlchemy async engine instance.
    """
    from lean_explore.extract.doc_parser import extract_declarations

    logger.info("Step 1: Extracting declarations from doc-gen4...")
    await extract_declarations(engine)
    logger.info("Declaration extraction complete")


async def _run_informalize_step(
    engine: AsyncEngine, settings: InformalizeSettings
) -> None:
    """Generate informal descriptions for declarations.

    Args:
        engine: SQLAlchemy async engine instance.
        settings: Informalization settings.
    """
    from lean_explore.extract.informalize import informalize_declarations

    logger.info("Step 2: Generating informal descriptions...")
    await informalize_declarations(
        engine,
        model=settings.model,
        commit_batch_size=settings.batch_size,
        max_concurrent=settings.max_concurrent,
        limit=settings.limit,
    )
    logger.info("Informalization complete")


async def _run_embeddings_step(
    engine: AsyncEngine, settings: EmbeddingSettings
) -> None:
    """Generate embeddings for all declaration fields.

    Args:
        engine: SQLAlchemy async engine instance.
        settings: Embedding generation settings.
    """
    from lean_explore.extract.embeddings import generate_embeddings

    logger.info("Step 3: Generating embeddings...")
    await generate_embeddings(
        engine,
        model_name=settings.model_name,
        batch_size=settings.batch_size,
        limit=settings.limit,
        max_seq_length=settings.max_seq_length,
        embedding_server_url=settings.server_url,
    )
    logger.info("Embedding generation complete")


async def _run_index_step(engine: AsyncEngine, extraction_path: Path) -> None:
    """Build search indices (FAISS and BM25).

    Args:
        engine: SQLAlchemy async engine instance.
        extraction_path: Directory to save indices (same as database location).
    """
    from lean_explore.extract.index import build_bm25_indices, build_faiss_indices

    logger.info("Step 4: Building search indices...")
    await build_faiss_indices(engine, output_directory=extraction_path)
    await build_bm25_indices(engine, output_directory=extraction_path)
    logger.info("Index building complete")


async def _run_steps(
    engine: AsyncEngine,
    extraction_path: Path,
    steps: PipelineSteps,
    informalize_settings: InformalizeSettings,
    embedding_settings: EmbeddingSettings,
) -> None:
    """Run the enabled enrichment steps in pipeline order.

    Args:
        engine: SQLAlchemy async engine with the schema already created.
        extraction_path: Directory to save indices in.
        steps: Which steps to run.
        informalize_settings: Settings for the informalization step.
        embedding_settings: Settings for the embeddings step.
    """
    if steps.parse_docs:
        await _run_extract_step(engine)
    if steps.informalize:
        await _run_informalize_step(engine, informalize_settings)
    if steps.embeddings:
        await _run_embeddings_step(engine, embedding_settings)
    if steps.index:
        await _run_index_step(engine, extraction_path)


async def run_pipeline(
    database_url: str,
    extraction_path: Path,
    run_doc_gen4: bool = False,
    fresh: bool = False,
    parse_docs: bool = True,
    informalize: bool = True,
    embeddings: bool = True,
    index: bool = True,
    informalize_model: str = "google/gemini-3-flash-preview",
    informalize_batch_size: int = 1000,
    informalize_max_concurrent: int = 100,
    informalize_limit: int | None = None,
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B",
    embedding_batch_size: int = 250,
    embedding_limit: int | None = None,
    embedding_max_seq_length: int = 512,
    embedding_server_url: str | None = None,
    verbose: bool = False,
) -> None:
    """Run the Lean declaration extraction and enrichment pipeline.

    Args:
        database_url: SQLite database URL (e.g., sqlite+aiosqlite:///path/to/db)
        extraction_path: Directory containing the extraction (for saving indices).
        run_doc_gen4: Run doc-gen4 to generate documentation before parsing
        fresh: Clear cached dependencies to force fresh resolution (for nightly updates)
        parse_docs: Run doc-gen4 parsing step
        informalize: Run informalization step
        embeddings: Run embeddings generation step
        index: Run FAISS index building step
        informalize_model: LLM model for generating informalizations
        informalize_batch_size: Commit batch size for informalization
        informalize_max_concurrent: Maximum concurrent informalization requests
        informalize_limit: Limit number of declarations to informalize
        embedding_model: Sentence transformer model for embeddings
        embedding_batch_size: Batch size for embedding generation
        embedding_limit: Limit number of declarations for embeddings
        embedding_max_seq_length: Max sequence length for embeddings (lower=less mem)
        embedding_server_url: URL of a running backend to delegate embedding
            generation to, avoiding local GPU memory usage.
        verbose: Enable verbose logging

    Raises:
        RuntimeError: If informalization is enabled but ``OPENROUTER_API_KEY``
            is not set.
    """
    setup_logging(verbose)
    if informalize:
        _require_openrouter_key()

    steps = PipelineSteps(parse_docs, informalize, embeddings, index)
    logger.info("Starting Lean Explore extraction pipeline")
    logger.info("Database URL: %s", database_url)
    logger.info("Steps to run: %s", ", ".join(steps.enabled_names()))

    engine = create_async_engine(database_url, echo=verbose)
    try:
        await _create_database_schema(engine)
        if run_doc_gen4:
            await _run_doc_gen4_step(fresh=fresh)
        await _run_steps(
            engine,
            extraction_path,
            steps,
            InformalizeSettings(
                model=informalize_model,
                batch_size=informalize_batch_size,
                max_concurrent=informalize_max_concurrent,
                limit=informalize_limit,
            ),
            EmbeddingSettings(
                model_name=embedding_model,
                batch_size=embedding_batch_size,
                limit=embedding_limit,
                max_seq_length=embedding_max_seq_length,
                server_url=embedding_server_url,
            ),
        )
        logger.info("Pipeline completed successfully!")
    finally:
        await engine.dispose()


@click.command()
@click.option(
    "--run-doc-gen4",
    is_flag=True,
    help="Run doc-gen4 to generate documentation before parsing",
)
@click.option(
    "--fresh",
    is_flag=True,
    help="Clear cached dependencies to fetch latest versions (use for nightly updates)",
)
@click.option(
    "--parse-docs/--no-parse-docs",
    default=None,
    help="Run doc-gen4 parsing step (creates new timestamped directory)",
)
@click.option(
    "--informalize/--no-informalize",
    default=None,
    help="Run informalization step (uses latest extraction)",
)
@click.option(
    "--embeddings/--no-embeddings",
    default=None,
    help="Run embeddings generation step (uses latest extraction)",
)
@click.option(
    "--index/--no-index",
    default=None,
    help="Run FAISS index building step (uses latest extraction)",
)
@click.option(
    "--informalize-model",
    default="google/gemini-3-flash-preview",
    help="LLM model for generating informalizations",
)
@click.option(
    "--informalize-max-concurrent",
    type=int,
    default=10,
    help="Maximum concurrent informalization requests",
)
@click.option(
    "--informalize-limit",
    type=int,
    default=None,
    help="Limit number of declarations to informalize (for testing)",
)
@click.option(
    "--embedding-model",
    default="Qwen/Qwen3-Embedding-0.6B",
    help="Sentence transformer model for embeddings",
)
@click.option(
    "--embedding-batch-size",
    type=int,
    default=250,
    help="Batch size for embedding generation (lower = less memory, default 250)",
)
@click.option(
    "--embedding-limit",
    type=int,
    default=None,
    help="Limit number of declarations for embeddings (for testing)",
)
@click.option(
    "--embedding-max-seq-length",
    type=int,
    default=512,
    help="Max sequence length for embeddings (lower = less memory, default 512)",
)
@click.option(
    "--embedding-server-url",
    default=None,
    help="URL of a running backend server to use for embedding generation (e.g. http://localhost:5001)",
)
@click.option("--verbose", is_flag=True, help="Enable verbose logging")
def main(
    run_doc_gen4: bool,
    fresh: bool,
    parse_docs: bool | None,
    informalize: bool | None,
    embeddings: bool | None,
    index: bool | None,
    informalize_model: str,
    informalize_max_concurrent: int,
    informalize_limit: int | None,
    embedding_model: str,
    embedding_batch_size: int,
    embedding_limit: int | None,
    embedding_max_seq_length: int,
    embedding_server_url: str | None,
    verbose: bool,
) -> None:
    """Run the Lean declaration extraction and enrichment pipeline.

    Extraction creates timestamped directories (YYYYMMDD_HHMMSS format).
    Subsequent steps (informalize, embeddings, index) use the latest extraction.
    """
    steps = resolve_steps(run_doc_gen4, parse_docs, informalize, embeddings, index)
    extraction_path = resolve_extraction_path(create_new=steps.parse_docs)
    asyncio.run(
        run_pipeline(
            database_url=database_url_for(extraction_path),
            extraction_path=extraction_path,
            run_doc_gen4=run_doc_gen4,
            fresh=fresh,
            parse_docs=steps.parse_docs,
            informalize=steps.informalize,
            embeddings=steps.embeddings,
            index=steps.index,
            informalize_model=informalize_model,
            informalize_max_concurrent=informalize_max_concurrent,
            informalize_limit=informalize_limit,
            embedding_model=embedding_model,
            embedding_batch_size=embedding_batch_size,
            embedding_limit=embedding_limit,
            embedding_max_seq_length=embedding_max_seq_length,
            embedding_server_url=embedding_server_url,
            verbose=verbose,
        )
    )


if __name__ == "__main__":
    main()
