"""Type definitions for doc-gen4 data extraction."""

from pydantic import BaseModel


class Declaration(BaseModel):
    """A declaration for database storage - mirrors schemas.Declaration."""

    name: str
    """Fully qualified Lean name."""

    module: str
    """Module name."""

    docstring: str | None
    """Documentation string, if available."""

    source_text: str
    """The actual Lean source code."""

    source_link: str
    """GitHub URL to the source code."""

    dependencies: list[str] | None
    """List of declaration names this declaration depends on."""
