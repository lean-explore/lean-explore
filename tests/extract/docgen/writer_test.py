"""Tests for inserting extracted declarations into the database."""

import json

from sqlalchemy import select

from lean_explore.extract.docgen.writer import insert_declarations_batch
from lean_explore.extract.types import Declaration
from lean_explore.models import Declaration as DBDeclaration


def _declaration(index: int, dependencies: list[str] | None = None) -> Declaration:
    """Build a distinct extracted declaration."""
    return Declaration(
        name=f"Test.Declaration{index}",
        module="Test.Module",
        docstring=f"Test declaration {index}",
        source_text=f"def test{index} := {index}",
        source_link=f"https://example.com/test{index}.lean#L1-L1",
        dependencies=dependencies,
    )


async def _stored(session) -> list[DBDeclaration]:
    """Return all stored declarations ordered by id."""
    result = await session.execute(select(DBDeclaration).order_by(DBDeclaration.id))
    return list(result.scalars().all())


class TestInsertDeclarationsBatch:
    """Tests for database insertion."""

    async def test_stores_all_fields(self, async_db_session):
        """Test inserting a declaration stores every field."""
        inserted = await insert_declarations_batch(
            async_db_session, [_declaration(0, ["Nat", "Bool"])], batch_size=100
        )

        [stored] = await _stored(async_db_session)
        assert inserted == 1
        assert (stored.name, stored.module, stored.docstring) == (
            "Test.Declaration0",
            "Test.Module",
            "Test declaration 0",
        )
        assert stored.source_text == "def test0 := 0"
        assert stored.source_link == "https://example.com/test0.lean#L1-L1"
        assert json.loads(stored.dependencies) == ["Nat", "Bool"]
        assert stored.informalization is None

    async def test_empty_dependencies_stored_as_null(self, async_db_session):
        """Test that None and empty dependency lists are stored as NULL."""
        await insert_declarations_batch(
            async_db_session, [_declaration(0, None), _declaration(1, [])]
        )

        assert [d.dependencies for d in await _stored(async_db_session)] == [
            None,
            None,
        ]

    async def test_skips_duplicates(self, async_db_session):
        """Test that duplicate names are skipped and keep the first version."""
        first = _declaration(0)
        duplicate = first.model_copy(update={"docstring": "changed"})

        inserted = await insert_declarations_batch(
            async_db_session, [first, duplicate], batch_size=100
        )

        stored = await _stored(async_db_session)
        assert inserted == 1
        assert [d.docstring for d in stored] == ["Test declaration 0"]

    async def test_multiple_batches(self, async_db_session):
        """Test inserting more declarations than one batch holds."""
        declarations = [_declaration(i) for i in range(10)]

        inserted = await insert_declarations_batch(
            async_db_session, declarations, batch_size=3
        )

        stored = await _stored(async_db_session)
        assert inserted == 10
        assert [d.name for d in stored] == [d.name for d in declarations]

    async def test_empty_input(self, async_db_session):
        """Test inserting nothing returns zero."""
        assert await insert_declarations_batch(async_db_session, []) == 0
