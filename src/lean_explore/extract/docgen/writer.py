"""Write extracted declarations to the search database."""

import json
from typing import cast

from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from lean_explore.extract.docgen.common import new_progress
from lean_explore.extract.types import Declaration
from lean_explore.models import Declaration as DBDeclaration


def _insert_statement(declaration: Declaration) -> Insert:
    """Build an insert for one declaration that ignores duplicate names."""
    dependencies_json = (
        json.dumps(declaration.dependencies) if declaration.dependencies else None
    )
    return (
        insert(DBDeclaration)
        .values(
            name=declaration.name,
            module=declaration.module,
            docstring=declaration.docstring,
            source_text=declaration.source_text,
            source_link=declaration.source_link,
            dependencies=dependencies_json,
        )
        .on_conflict_do_nothing(index_elements=["name"])
    )


async def insert_declarations_batch(
    session: AsyncSession, declarations: list[Declaration], batch_size: int = 1000
) -> int:
    """Insert declarations into database in batches.

    Declarations whose name already exists are skipped. All batches run inside
    a single transaction.

    Args:
        session: Active database session.
        declarations: List of declarations to insert.
        batch_size: Number of declarations to insert per batch.

    Returns:
        Number of declarations successfully inserted.
    """
    inserted_count = 0

    with new_progress() as progress:
        task = progress.add_task(
            "[green]Inserting declarations into database...",
            total=len(declarations),
        )

        async with session.begin():
            for i in range(0, len(declarations), batch_size):
                for declaration in declarations[i : i + batch_size]:
                    result = await session.execute(_insert_statement(declaration))
                    inserted_count += cast(CursorResult, result).rowcount
                    progress.update(task, advance=1)

    return inserted_count
