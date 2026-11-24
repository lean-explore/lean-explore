"""Package configuration for Lean extraction.

This module defines the configuration dataclass and the version strategy and
extraction method enums for Lean packages to extract.
"""

from dataclasses import dataclass, field
from enum import Enum


class VersionStrategy(Enum):
    """Strategy for selecting which version of a package to extract."""

    LATEST = "latest"
    """Use HEAD/main branch - for packages with CI that ensures main compiles."""

    TAGGED = "tagged"
    """Use the latest git tag - safer for downstream packages."""


class ExtractionMethod(Enum):
    """How a package's declarations are extracted."""

    DOCGEN = "docgen"
    """Build the package and read doc-gen4 output."""

    SOURCE = "source"
    """Parse the package's Lean sources without building them."""


@dataclass
class PackageConfig:
    """Configuration for a Lean package extraction."""

    name: str
    """Package name (e.g., 'mathlib', 'physlean')."""

    git_url: str
    """GitHub repository URL."""

    module_prefixes: list[str]
    """Module name prefixes that belong to this package (e.g., ['Mathlib'])."""

    version_strategy: VersionStrategy = VersionStrategy.TAGGED
    """Strategy for selecting the version to extract."""

    depends_on: list[str] = field(default_factory=list)
    """List of package names this package depends on (for extraction ordering)."""

    extraction: ExtractionMethod = ExtractionMethod.DOCGEN
    """Whether to read doc-gen4 output or parse the unbuilt sources."""

    source_subdirectory: str = ""
    """Directory of the repository that holds its modules, for source extraction.

    Empty when the modules are at the repository root. Lean itself keeps
    ``Init``, ``Std`` and ``Lean`` under ``src``.
    """

    def should_include_module(self, module_name: str) -> bool:
        """Check if a module belongs to this package based on prefixes.

        Uses exact match or prefix + "." to avoid "Lean" matching "LeanSearchClient".

        Args:
            module_name: Fully qualified module name (e.g., 'Mathlib.Data.List').

        Returns:
            True if the module is one of this package's prefixes or below one.
        """
        return any(
            module_name == prefix or module_name.startswith(prefix + ".")
            for prefix in self.module_prefixes
        )
