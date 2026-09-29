"""End-to-end tests for informalize_declarations.

Each test runs the real pipeline against SQLite database files with a fake LLM
client, and checks what ends up stored in the database.
"""

import pytest

from lean_explore.extract import informalize
from lean_explore.extract.informalize import informalize_declarations
from tests.extract.builders import (
    FakeLLMClient,
    create_database,
    describe,
    load_by_name,
    make_declaration,
    write_database,
)


@pytest.fixture
def llm(monkeypatch) -> FakeLLMClient:
    """Install a fake LLM client in place of OpenRouterClient."""
    client = FakeLLMClient()
    monkeypatch.setattr(informalize, "OpenRouterClient", lambda: client)
    return client


@pytest.fixture
def no_llm(monkeypatch):
    """Fail the test if an LLM client is created."""

    def fail():
        raise AssertionError("OpenRouterClient should not be created")

    monkeypatch.setattr(informalize, "OpenRouterClient", fail)


async def _informalize(tmp_path, declarations, **kwargs) -> dict:
    """Run informalize_declarations on a fresh database; return stored rows."""
    engine = await create_database(tmp_path / "run" / "lean_explore.db", declarations)
    kwargs.setdefault("model", "test-model")
    await informalize_declarations(engine, **kwargs)
    stored = await load_by_name(engine)
    await engine.dispose()
    return stored


def _chain() -> list:
    return [
        make_declaration("Top", dependencies=["Mid", "Base"]),
        make_declaration("Mid", dependencies=["Base"]),
        make_declaration("Base"),
    ]


class TestInformalizeDeclarations:
    """Tests for the informalization pipeline."""

    async def test_informalizes_in_dependency_order(self, tmp_path, previous_runs, llm):
        """Dependencies are described first and passed as prompt context."""
        stored = await _informalize(tmp_path, _chain())

        assert llm.order == ["Base", "Mid", "Top"]
        assert f"- Mid: {describe('Mid')}" in llm.prompts["Top"]
        assert f"- Base: {describe('Base')}" in llm.prompts["Top"]
        assert "**Declaration Name:** Top" in llm.prompts["Top"]
        assert {n: d.informalization for n, d in stored.items()} == {
            n: describe(n) for n in ["Top", "Mid", "Base"]
        }

    async def test_existing_informalizations_are_kept_and_used(
        self, tmp_path, previous_runs, llm
    ):
        """Already described declarations are not resent but give context."""
        declarations = [
            make_declaration("Base", informalization="Existing base."),
            make_declaration("Mid", dependencies=["Base"]),
        ]

        stored = await _informalize(tmp_path, declarations)

        assert llm.order == ["Mid"]
        assert "- Base: Existing base." in llm.prompts["Mid"]
        assert stored["Base"].informalization == "Existing base."

    async def test_reuses_matching_informalizations_from_previous_runs(
        self, tmp_path, previous_runs, llm
    ):
        """Previous results are reused only when name and source text match."""
        data_directory, cache_directory = previous_runs
        write_database(
            data_directory / "v1" / "lean_explore.db",
            [
                make_declaration("Base", informalization="Cached base."),
                make_declaration(
                    "Mid", source_text="def Mid := old", informalization="Stale."
                ),
            ],
        )
        write_database(
            cache_directory / "v0" / "lean_explore.db",
            [make_declaration("Base", informalization="Older cached base.")],
        )

        stored = await _informalize(tmp_path, _chain())

        assert llm.order == ["Mid", "Top"]
        assert stored["Base"].informalization == "Cached base."
        assert "- Base: Cached base." in llm.prompts["Mid"]
        assert stored["Mid"].informalization == describe("Mid")

    async def test_unreadable_previous_database_is_skipped(
        self, tmp_path, previous_runs, llm
    ):
        """A corrupt cache database does not stop the run."""
        data_directory, _ = previous_runs
        (data_directory / "lean_explore.db").write_text("not a database")

        stored = await _informalize(tmp_path, [make_declaration("Base")])

        assert stored["Base"].informalization == describe("Base")

    async def test_all_cached_makes_no_llm_calls(self, tmp_path, previous_runs, no_llm):
        """When every declaration is cached no LLM client is created."""
        data_directory, _ = previous_runs
        write_database(
            data_directory / "lean_explore.db",
            [make_declaration("Base", informalization="Cached base.")],
        )

        stored = await _informalize(
            tmp_path, [make_declaration("Base")], commit_batch_size=1
        )

        assert stored["Base"].informalization == "Cached base."

    async def test_nothing_to_do(self, tmp_path, previous_runs, no_llm):
        """A fully informalized database is left alone."""
        stored = await _informalize(
            tmp_path, [make_declaration("Base", informalization="Done.")]
        )

        assert stored["Base"].informalization == "Done."

    async def test_limit_caps_declarations_processed(
        self, tmp_path, previous_runs, llm
    ):
        """Only `limit` declarations are informalized."""
        declarations = [make_declaration(f"D{i}") for i in range(5)]

        stored = await _informalize(tmp_path, declarations, limit=2)

        assert len(llm.order) == 2
        assert sum(d.informalization is not None for d in stored.values()) == 2

    async def test_small_commit_batches_store_everything(
        self, tmp_path, previous_runs, llm
    ):
        """Results are stored whatever the commit batch size."""
        stored = await _informalize(tmp_path, _chain(), commit_batch_size=1)

        assert all(d.informalization == describe(n) for n, d in stored.items())

    async def test_empty_llm_responses_are_left_unset(
        self, tmp_path, previous_runs, llm
    ):
        """Declarations with an empty LLM answer stay without description."""
        llm.empty_for = {"Mid"}

        stored = await _informalize(tmp_path, _chain())

        assert stored["Mid"].informalization is None
        assert "- Mid:" not in llm.prompts["Top"]
        assert stored["Top"].informalization == describe("Top")

    async def test_llm_failure_aborts_run(self, tmp_path, previous_runs, monkeypatch):
        """One failing LLM call aborts the run (known issue, kept as is)."""

        class FailingClient:
            async def generate(self, **kwargs):
                raise RuntimeError("rate limited")

        monkeypatch.setattr(informalize, "OpenRouterClient", FailingClient)

        with pytest.raises(RuntimeError, match="rate limited"):
            await _informalize(tmp_path, [make_declaration("Base")])
