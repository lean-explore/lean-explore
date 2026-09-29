"""Tests for prompt building and layer-by-layer LLM informalization."""

import asyncio
import json

import pytest
from rich.progress import Progress
from sqlalchemy.ext.asyncio import AsyncSession

from lean_explore.extract.informalize_generation import (
    DeclarationData,
    InformalizationRun,
    LayerProgress,
    build_prompt,
    format_dependencies,
    process_layer,
    process_one_declaration,
)
from tests.extract.builders import (
    FakeLLMClient,
    create_database,
    describe,
    load_by_name,
    make_declaration,
)

TEMPLATE = "Name: {name}\nSource: {source_text}\nDoc: {docstring}\n{dependencies}"


def _data(name="Foo", *, dependencies=None, informalization=None, docstring=None):
    return DeclarationData(
        id=1,
        name=name,
        source_text=f"def {name} := 0",
        docstring=docstring,
        dependencies=dependencies,
        informalization=informalization,
    )


async def _process(data, client, *, cache=None, informalizations_by_name=None):
    return await process_one_declaration(
        declaration_data=data,
        client=client,
        model="test-model",
        prompt_template=TEMPLATE,
        informalizations_by_name=informalizations_by_name or {},
        cache=cache or {},
        semaphore=asyncio.Semaphore(1),
    )


class TestPromptBuilding:
    """Tests for format_dependencies and build_prompt."""

    def test_lists_only_described_dependencies(self):
        """Dependencies without a description are left out."""
        text = format_dependencies('["A", "Unknown", "B"]', {"A": "a.", "B": "b."})

        assert text == "Dependencies:\n- A: a.\n- B: b."

    def test_no_described_dependencies_gives_empty_text(self):
        """Without any known description there is no dependency section."""
        assert format_dependencies('["Unknown"]', {}) == ""
        assert format_dependencies(None, {"A": "a."}) == ""

    def test_long_descriptions_are_truncated_to_256_characters(self):
        """Descriptions over 256 characters end in an ellipsis."""
        text = format_dependencies(["A"], {"A": "x" * 300})

        assert text == "Dependencies:\n- A: " + "x" * 253 + "..."

    def test_only_first_twenty_dependencies_are_considered(self):
        """Dependencies after the twentieth are ignored."""
        names = [f"D{i}" for i in range(25)]

        text = format_dependencies(json.dumps(names), {n: "d." for n in names})

        assert text.count("\n- ") == 20
        assert "D19" in text and "D20" not in text

    def test_build_prompt_fills_template(self):
        """The template is filled with a docstring placeholder when missing."""
        prompt = build_prompt(_data(dependencies='["A"]'), TEMPLATE, {"A": "a."})

        assert prompt == (
            "Name: Foo\nSource: def Foo := 0\nDoc: No docstring available\n"
            "Dependencies:\n- A: a."
        )


class TestProcessOneDeclaration:
    """Tests for process_one_declaration."""

    async def test_generates_stripped_description(self):
        """The LLM response is stripped and returned."""
        result = await _process(_data(), FakeLLMClient())

        assert result.informalization == describe("Foo")
        assert (result.declaration_id, result.declaration_name) == (1, "Foo")

    async def test_prompt_includes_dependency_descriptions(self):
        """Known dependency descriptions are passed to the LLM."""
        client = FakeLLMClient()

        await _process(
            _data(dependencies='["A"]'), client, informalizations_by_name={"A": "a."}
        )

        assert "- A: a." in client.prompts["Foo"]

    async def test_already_informalized_is_skipped(self):
        """Declarations that already have a description are not sent."""
        client = FakeLLMClient()

        result = await _process(_data(informalization="done"), client)

        assert result.informalization is None
        assert client.order == []

    async def test_cache_hit_skips_llm(self):
        """A (name, source_text) cache hit is returned without an LLM call."""
        client = FakeLLMClient()
        cache = {("Foo", "def Foo := 0"): "cached"}

        result = await _process(_data(), client, cache=cache)

        assert result.informalization == "cached"
        assert client.order == []

    async def test_empty_response_gives_none(self):
        """An empty LLM answer produces no informalization."""
        result = await _process(_data(), FakeLLMClient(empty_for={"Foo"}))

        assert result.informalization is None

    async def test_llm_errors_propagate(self):
        """LLM exceptions are not caught (known issue: this aborts the run)."""

        class FailingClient:
            async def generate(self, **kwargs):
                raise RuntimeError("rate limited")

        with pytest.raises(RuntimeError, match="rate limited"):
            await _process(_data(), FailingClient())


async def _run_layer(tmp_path, declarations, client, *, max_concurrent=10, batch=2):
    engine = await create_database(tmp_path / "search.db", declarations)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        run = InformalizationRun(
            session=session,
            client=client,
            model="test-model",
            prompt_template=TEMPLATE,
            informalizations_by_name={},
            cache={},
            semaphore=asyncio.Semaphore(max_concurrent),
            commit_batch_size=batch,
        )
        with Progress(disable=True) as progress:
            layer_progress = LayerProgress(
                progress, progress.add_task("total"), progress.add_task("batch")
            )
            layer = list((await load_by_name(engine)).values())
            processed = await process_layer(run, layer, layer_progress)
    stored = await load_by_name(engine)
    await engine.dispose()
    return processed, run, stored


class TestProcessLayer:
    """Tests for process_layer against a real SQLite database."""

    async def test_commits_all_generated_descriptions(self, tmp_path):
        """Every non-empty description is stored, across several commit batches."""
        names = ["A", "B", "C", "D", "E"]
        client = FakeLLMClient(empty_for={"C"})

        processed, run, stored = await _run_layer(
            tmp_path, [make_declaration(n) for n in names], client
        )

        assert processed == 4
        assert stored["C"].informalization is None
        for name in ["A", "B", "D", "E"]:
            assert stored[name].informalization == describe(name)
            assert run.informalizations_by_name[name] == describe(name)

    async def test_concurrency_is_bounded_by_semaphore(self, tmp_path):
        """No more than max_concurrent LLM calls run at once."""
        client = FakeLLMClient(delay=0.01)
        declarations = [make_declaration(f"D{i}") for i in range(6)]

        await _run_layer(tmp_path, declarations, client, max_concurrent=2)

        assert client.max_active == 2
