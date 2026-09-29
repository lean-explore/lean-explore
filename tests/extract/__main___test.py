"""Tests for the extraction pipeline CLI and orchestration.

The pipeline steps themselves (doc-gen4, parsing, informalization, embeddings,
indexing) are replaced with recorders so these tests exercise argument
resolution, step selection, path resolution, and orchestration order.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from sqlalchemy import inspect

from lean_explore.config import Config
from lean_explore.extract import __main__ as pipeline
from lean_explore.extract.__main__ import (
    EmbeddingSettings,
    InformalizeSettings,
    PipelineSteps,
    database_url_for,
    main,
    resolve_steps,
    run_pipeline,
)

NIGHTLY_ARGS = [
    "--run-doc-gen4",
    "--fresh",
    "--parse-docs",
    "--informalize",
    "--embeddings",
    "--index",
    "--embedding-server-url",
    "http://localhost:5001",
]
"""Exact invocation used by lean-explore-app's nightly update."""


def _recorder(calls: list, name: str):
    """Return an async function that records its call under ``name``."""

    async def record(*args, **kwargs):
        calls.append((name, args, kwargs))

    return record


@pytest.fixture
def data_directory(tmp_path, monkeypatch) -> Path:
    """Point extraction directories at a temporary data directory."""
    monkeypatch.setattr(Config, "DATA_DIRECTORY", tmp_path)
    return tmp_path


@pytest.fixture
def captured_pipeline(monkeypatch) -> dict:
    """Replace run_pipeline with a recorder and return the captured kwargs."""
    captured: dict = {}

    async def fake_run_pipeline(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)
    return captured


def _invoke(args: list[str]):
    return CliRunner().invoke(main, args, catch_exceptions=False)


class TestResolveSteps:
    """Tests for resolving tri-state step flags."""

    def test_no_flags_runs_every_step(self):
        """Without flags every step is enabled."""
        assert resolve_steps(False, None, None, None, None) == PipelineSteps(
            True, True, True, True
        )

    def test_explicit_flag_disables_unmentioned_steps(self):
        """Once any step flag is given, only explicitly enabled steps run."""
        assert resolve_steps(False, None, True, None, True) == PipelineSteps(
            False, True, False, True
        )

    def test_run_doc_gen4_alone_disables_all_steps(self):
        """--run-doc-gen4 counts as an explicit selection."""
        assert resolve_steps(True, None, None, None, None) == PipelineSteps(
            False, False, False, False
        )

    def test_lone_negative_flag_disables_all_steps(self):
        """A lone --no-<step> leaves nothing enabled (current behavior)."""
        assert resolve_steps(False, None, None, None, False) == PipelineSteps(
            False, False, False, False
        )

    def test_enabled_names_in_pipeline_order(self):
        """Enabled step names are reported in execution order."""
        steps = PipelineSteps(True, False, True, True)
        assert steps.enabled_names() == ["parse-docs", "embeddings", "index"]


class TestCli:
    """Tests for CLI argument handling and path resolution."""

    def test_nightly_invocation(self, data_directory, captured_pipeline):
        """The nightly flags run everything in a new timestamped directory."""
        result = _invoke(NIGHTLY_ARGS)

        assert result.exit_code == 0
        (extraction_path,) = list(data_directory.iterdir())
        assert captured_pipeline["extraction_path"] == extraction_path
        assert captured_pipeline["database_url"] == database_url_for(extraction_path)
        assert captured_pipeline["run_doc_gen4"] and captured_pipeline["fresh"]
        assert all(
            captured_pipeline[step]
            for step in ("parse_docs", "informalize", "embeddings", "index")
        )
        assert captured_pipeline["embedding_server_url"] == "http://localhost:5001"

    def test_cli_defaults(self, data_directory, captured_pipeline):
        """CLI defaults are forwarded unchanged."""
        _invoke([])

        assert captured_pipeline["run_doc_gen4"] is False
        assert captured_pipeline["informalize_model"] == (
            "google/gemini-3-flash-preview"
        )
        # Known discrepancy: the CLI default is 10 while run_pipeline's is 100.
        assert captured_pipeline["informalize_max_concurrent"] == 10
        assert captured_pipeline["embedding_batch_size"] == 250
        assert captured_pipeline["embedding_max_seq_length"] == 512
        assert captured_pipeline["embedding_server_url"] is None
        assert "informalize_batch_size" not in captured_pipeline

    def test_options_forwarded(self, data_directory, captured_pipeline):
        """Model, limit, and batch options reach run_pipeline."""
        _invoke(
            [
                "--parse-docs",
                "--informalize-model=m",
                "--informalize-limit=5",
                "--embedding-model=e",
                "--embedding-limit=7",
                "--embedding-batch-size=3",
                "--verbose",
            ]
        )

        assert captured_pipeline["informalize_model"] == "m"
        assert captured_pipeline["informalize_limit"] == 5
        assert captured_pipeline["embedding_model"] == "e"
        assert captured_pipeline["embedding_limit"] == 7
        assert captured_pipeline["embedding_batch_size"] == 3
        assert captured_pipeline["verbose"] is True

    def test_later_steps_reuse_latest_extraction(
        self, data_directory, captured_pipeline
    ):
        """Without --parse-docs the newest existing extraction is reused."""
        (data_directory / "20240101_000000").mkdir()
        latest = data_directory / "20250101_000000"
        latest.mkdir()

        _invoke(["--informalize"])

        assert captured_pipeline["extraction_path"] == latest
        assert captured_pipeline["informalize"] is True
        assert captured_pipeline["parse_docs"] is False
        assert len(list(data_directory.iterdir())) == 2

    def test_missing_extraction_is_an_error(self, data_directory, captured_pipeline):
        """Reusing an extraction when none exists fails with a clear message."""
        result = _invoke(["--index"])

        assert result.exit_code == 1
        assert "No existing extraction found" in result.output
        assert captured_pipeline == {}


@pytest.fixture
def recorded_steps(monkeypatch) -> list:
    """Replace every pipeline step with a recorder."""
    calls: list = []
    for name in (
        "_run_doc_gen4_step",
        "_run_extract_step",
        "_run_informalize_step",
        "_run_embeddings_step",
        "_run_index_step",
    ):
        monkeypatch.setattr(pipeline, name, _recorder(calls, name))
    monkeypatch.setattr(pipeline, "setup_logging", lambda verbose: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return calls


class TestRunPipeline:
    """Tests for pipeline orchestration."""

    async def test_runs_all_steps_in_order(self, tmp_path, recorded_steps):
        """doc-gen4 runs first, then the enrichment steps in pipeline order."""
        await run_pipeline(
            database_url=database_url_for(tmp_path),
            extraction_path=tmp_path,
            run_doc_gen4=True,
            fresh=True,
        )

        assert [name for name, _, _ in recorded_steps] == [
            "_run_doc_gen4_step",
            "_run_extract_step",
            "_run_informalize_step",
            "_run_embeddings_step",
            "_run_index_step",
        ]
        assert recorded_steps[0][2] == {"fresh": True}
        assert recorded_steps[-1][1][1] == tmp_path

    async def test_settings_passed_to_steps(self, tmp_path, recorded_steps):
        """Step settings are assembled from run_pipeline arguments."""
        await run_pipeline(
            database_url=database_url_for(tmp_path),
            extraction_path=tmp_path,
            parse_docs=False,
            index=False,
            informalize_model="m",
            informalize_limit=3,
            embedding_server_url="http://x",
        )

        informalize_args = recorded_steps[0][1]
        embeddings_args = recorded_steps[1][1]
        assert informalize_args[1] == InformalizeSettings("m", 1000, 100, 3)
        assert embeddings_args[1] == EmbeddingSettings(
            "Qwen/Qwen3-Embedding-0.6B", 250, None, 512, "http://x"
        )

    async def test_creates_database_schema(self, tmp_path, recorded_steps):
        """The database file and tables exist even when no steps run."""
        await run_pipeline(
            database_url=database_url_for(tmp_path),
            extraction_path=tmp_path,
            parse_docs=False,
            informalize=False,
            embeddings=False,
            index=False,
        )

        assert recorded_steps == []
        assert (tmp_path / "lean_explore.db").exists()

    async def test_informalize_requires_api_key(
        self, tmp_path, recorded_steps, monkeypatch
    ):
        """Informalization without an API key fails before touching the DB."""
        monkeypatch.delenv("OPENROUTER_API_KEY")

        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY not set"):
            await run_pipeline(
                database_url=database_url_for(tmp_path), extraction_path=tmp_path
            )

        assert not (tmp_path / "lean_explore.db").exists()

    async def test_engine_disposed_on_error(
        self, tmp_path, recorded_steps, monkeypatch
    ):
        """The engine is disposed even when a step fails."""
        engine = AsyncMock()
        monkeypatch.setattr(pipeline, "create_async_engine", lambda *a, **k: engine)
        monkeypatch.setattr(
            pipeline, "_create_database_schema", AsyncMock(side_effect=OSError("x"))
        )

        with pytest.raises(OSError):
            await run_pipeline(database_url="unused", extraction_path=tmp_path)

        engine.dispose.assert_awaited_once()


class TestSteps:
    """Tests that each step forwards its settings to the implementation."""

    async def test_create_database_schema_idempotent(self, async_db_engine):
        """Creating the schema twice leaves the declarations table in place."""
        await pipeline._create_database_schema(async_db_engine)
        await pipeline._create_database_schema(async_db_engine)

        async with async_db_engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync: inspect(sync).get_table_names()
            )
        assert "declarations" in tables

    async def test_doc_gen4_step(self, monkeypatch):
        """The doc-gen4 step forwards the fresh flag."""
        from lean_explore.extract import doc_gen4

        calls: list = []
        monkeypatch.setattr(doc_gen4, "run_doc_gen4", _recorder(calls, "docgen"))
        await pipeline._run_doc_gen4_step(fresh=True)
        assert calls == [("docgen", (), {"fresh": True})]

    async def test_extract_step(self, monkeypatch):
        """The extract step passes the engine through."""
        from lean_explore.extract import doc_parser

        calls: list = []
        monkeypatch.setattr(
            doc_parser, "extract_declarations", _recorder(calls, "extract")
        )
        await pipeline._run_extract_step("engine")
        assert calls == [("extract", ("engine",), {})]

    async def test_informalize_step(self, monkeypatch):
        """Informalization settings map onto informalize_declarations kwargs."""
        from lean_explore.extract import informalize

        calls: list = []
        monkeypatch.setattr(
            informalize, "informalize_declarations", _recorder(calls, "inf")
        )
        settings = InformalizeSettings("m", 10, 2, None)
        await pipeline._run_informalize_step("engine", settings)
        assert calls[0][2] == {
            "model": "m",
            "commit_batch_size": 10,
            "max_concurrent": 2,
            "limit": None,
        }

    async def test_embeddings_step(self, monkeypatch):
        """Embedding settings map onto generate_embeddings kwargs."""
        from lean_explore.extract import embeddings

        calls: list = []
        monkeypatch.setattr(embeddings, "generate_embeddings", _recorder(calls, "e"))
        settings = EmbeddingSettings("model", 8, 4, 128, "http://s")
        await pipeline._run_embeddings_step("engine", settings)
        assert calls[0][2] == {
            "model_name": "model",
            "batch_size": 8,
            "limit": 4,
            "max_seq_length": 128,
            "embedding_server_url": "http://s",
        }

    async def test_index_step_builds_faiss_then_bm25(self, monkeypatch, tmp_path):
        """Both indices are written into the extraction directory."""
        from lean_explore.extract import index

        calls: list = []
        monkeypatch.setattr(index, "build_faiss_indices", _recorder(calls, "faiss"))
        monkeypatch.setattr(index, "build_bm25_indices", _recorder(calls, "bm25"))
        await pipeline._run_index_step("engine", tmp_path)
        assert calls == [
            ("faiss", ("engine",), {"output_directory": tmp_path}),
            ("bm25", ("engine",), {"output_directory": tmp_path}),
        ]
