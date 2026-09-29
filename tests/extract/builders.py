"""Builders and fake clients shared by the extraction pipeline tests.

The tests use real SQLite database files (the pipeline opens extra sync and
async connections to them) plus small in-process fakes for the LLM and the
embedding model.
"""

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from lean_explore.models import Base, Declaration


def make_declaration(
    name: str,
    *,
    dependencies: list[str] | str | None = None,
    informalization: str | None = None,
    embedding: list[float] | None = None,
    source_text: str | None = None,
    docstring: str | None = None,
) -> Declaration:
    """Create a Declaration with sensible defaults for the required columns."""
    if isinstance(dependencies, list):
        dependencies = "[" + ", ".join(f'"{d}"' for d in dependencies) + "]"
    return Declaration(
        name=name,
        module="Test",
        docstring=docstring,
        source_text=source_text or f"def {name} := 0",
        source_link=f"https://example.com/{name}",
        dependencies=dependencies,
        informalization=informalization,
        informalization_embedding=embedding,
    )


def sqlite_url(path: Path, *, async_driver: bool = True) -> str:
    """Return the SQLAlchemy URL of a SQLite database file."""
    driver = "sqlite+aiosqlite" if async_driver else "sqlite"
    return f"{driver}:///{path}"


def write_database(path: Path, declarations: list[Declaration]) -> Path:
    """Create a SQLite database file containing the given declarations."""
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(sqlite_url(path, async_driver=False))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(declarations)
        session.commit()
    engine.dispose()
    return path


async def create_database(
    path: Path, declarations: list[Declaration] = ()
) -> AsyncEngine:
    """Create a SQLite database file and return an async engine for it."""
    write_database(path, list(declarations))
    return create_async_engine(sqlite_url(path))


async def load_by_name(engine: AsyncEngine) -> dict[str, Declaration]:
    """Load all declarations keyed by name."""
    async with AsyncSession(engine) as session:
        result = await session.execute(select(Declaration))
        return {d.name: d for d in result.scalars().all()}


def llm_response(content: str | None) -> SimpleNamespace:
    """Build an OpenAI-style chat completion response."""
    message = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeLLMClient:
    """Fake OpenRouter client describing each declaration by its name.

    The declaration name is read from the "Name:" line of the prompt, which is
    present both in the real prompt template and in the small test templates.
    """

    NAME_PATTERN = re.compile(r"Name:\**\s*(\S+)")

    def __init__(self, *, empty_for: set[str] = frozenset(), delay: float = 0.0):
        """Initialize the fake.

        Args:
            empty_for: Declaration names that get an empty response.
            delay: Seconds to sleep per call, to observe concurrency.
        """
        self.empty_for = empty_for
        self.delay = delay
        self.prompts: dict[str, str] = {}
        self.order: list[str] = []
        self.active = 0
        self.max_active = 0

    async def generate(self, *, model, messages, temperature):
        """Return "  Description of <name>.  " (untrimmed) for the prompt."""
        prompt = messages[0]["content"]
        name = self.NAME_PATTERN.search(prompt).group(1)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        self.prompts[name] = prompt
        self.order.append(name)
        return llm_response(
            None if name in self.empty_for else describe(name, pad=True)
        )


def describe(name: str, *, pad: bool = False) -> str:
    """Return the description FakeLLMClient generates for a name."""
    text = f"Description of {name}."
    return f"  {text}  " if pad else text


def fake_vector(text: str) -> list[float]:
    """Deterministic 4-dimensional embedding of a text."""
    return [float(len(text)), 1.0, 2.0, 3.0]


class FakeEmbeddingClient:
    """Fake embedding client recording each embed() batch."""

    model_name = "fake-embedding-model"

    def __init__(self):
        """Initialize with no recorded batches."""
        self.batches: list[list[str]] = []

    async def embed(self, texts: list[str]) -> SimpleNamespace:
        """Return fake_vector() for each text."""
        self.batches.append(list(texts))
        return SimpleNamespace(embeddings=[fake_vector(t) for t in texts])
