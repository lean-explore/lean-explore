"""SQLAlchemy ORM models for Lean declaration database.

Simple schema for a Lean declaration search engine.
Uses SQLAlchemy 2.0 syntax with SQLite for storage and FAISS for vector search.
"""

import struct

from sqlalchemy import Integer, LargeBinary, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class BinaryEmbedding(TypeDecorator):
    """Custom type for storing embeddings as binary blobs.

    Converts between Python list[float] and compact binary representation.
    Uses float32 (4 bytes per dimension) for ~5x space savings over JSON.
    """

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: list[float] | None, dialect) -> bytes | None:
        """Convert list[float] to binary for storage."""
        if value is None:
            return None
        return struct.pack(f"{len(value)}f", *value)

    def process_result_value(self, value: bytes | None, dialect) -> list[float] | None:
        """Convert binary back to list[float] on retrieval."""
        if value is None:
            return None
        num_floats = len(value) // 4
        return list(struct.unpack(f"{num_floats}f", value))


class Base(DeclarativeBase):
    """Base class for SQLAlchemy declarative models."""

    pass


class Declaration(Base):
    """Represents a Lean declaration for search."""

    __tablename__ = "declarations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    """Primary key identifier."""

    name: Mapped[str] = mapped_column(Text, unique=True, index=True, nullable=False)
    """Fully qualified Lean name (e.g., 'Nat.add')."""

    module: Mapped[str] = mapped_column(Text, index=True, nullable=False)
    """Module name (e.g., 'Mathlib.Data.List.Basic')."""

    docstring: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Documentation string from the source code, if available."""

    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    """The actual Lean source code for this declaration."""

    source_link: Mapped[str] = mapped_column(Text, nullable=False)
    """GitHub URL to the declaration source code."""

    dependencies: Mapped[str | None] = mapped_column(Text, nullable=True)
    """JSON array of declaration names this declaration depends on."""

    informalization: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Natural language description of the declaration."""

    informalization_embedding: Mapped[list[float] | None] = mapped_column(
        BinaryEmbedding, nullable=True
    )
    """1024-dimensional embedding of the informalization text (binary float32)."""
