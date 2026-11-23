"""Package registry for Lean extraction.

This module contains the registry of Lean packages available for extraction.
Every package is extracted from its sources: its repository is cloned and
read by the build-free parser, so no package is built.
"""

from lean_explore.extract.packages.config import (
    ExtractionMethod,
    PackageConfig,
    VersionStrategy,
)

PACKAGE_REGISTRY: dict[str, PackageConfig] = {
    "lean": PackageConfig(
        name="lean",
        git_url="https://github.com/leanprover/lean4",
        module_prefixes=["Init", "Std", "Lean"],
        version_strategy=VersionStrategy.TAGGED,
        depends_on=[],
        extraction=ExtractionMethod.SOURCE,
        source_subdirectory="src",
    ),
    "batteries": PackageConfig(
        name="batteries",
        git_url="https://github.com/leanprover-community/batteries",
        module_prefixes=["Batteries"],
        version_strategy=VersionStrategy.LATEST,
        depends_on=["lean"],
        extraction=ExtractionMethod.SOURCE,
    ),
    "mathlib": PackageConfig(
        name="mathlib",
        git_url="https://github.com/leanprover-community/mathlib4",
        module_prefixes=["Mathlib"],
        version_strategy=VersionStrategy.LATEST,
        depends_on=["lean", "batteries"],
        extraction=ExtractionMethod.SOURCE,
    ),
    "physlean": PackageConfig(
        name="physlean",
        git_url="https://github.com/HEPLean/PhysLean",
        module_prefixes=["Physlib", "PhyslibAlpha", "QuantumInfo"],
        version_strategy=VersionStrategy.LATEST,
        depends_on=["mathlib"],
        extraction=ExtractionMethod.SOURCE,
    ),
    "flt": PackageConfig(
        name="flt",
        git_url="https://github.com/ImperialCollegeLondon/FLT",
        module_prefixes=["FLT"],
        version_strategy=VersionStrategy.LATEST,
        depends_on=["mathlib"],
        extraction=ExtractionMethod.SOURCE,
    ),
    "formal-conjectures": PackageConfig(
        name="formal-conjectures",
        git_url="https://github.com/google-deepmind/formal-conjectures",
        module_prefixes=["FormalConjectures", "FormalConjecturesForMathlib"],
        version_strategy=VersionStrategy.LATEST,
        depends_on=["mathlib"],
        extraction=ExtractionMethod.SOURCE,
    ),
    "cslib": PackageConfig(
        name="cslib",
        git_url="https://github.com/leanprover/cslib",
        module_prefixes=["Cslib"],
        version_strategy=VersionStrategy.LATEST,
        depends_on=["mathlib"],
        extraction=ExtractionMethod.SOURCE,
    ),
}
