"""Package registry for Lean extraction.

This module contains the registry of Lean packages available for extraction.
"""

from lean_explore.extract.packages.config import PackageConfig, VersionStrategy

PACKAGE_REGISTRY: dict[str, PackageConfig] = {
    "mathlib": PackageConfig(
        name="mathlib",
        git_url="https://github.com/leanprover-community/mathlib4",
        module_prefixes=["Mathlib", "Batteries", "Init", "Lean", "Std"],
        version_strategy=VersionStrategy.LATEST,
        depends_on=[],
    ),
    "physlean": PackageConfig(
        name="physlean",
        git_url="https://github.com/HEPLean/PhysLean",
        module_prefixes=["Physlib", "QuantumInfo"],
        version_strategy=VersionStrategy.TAGGED,
        depends_on=["mathlib"],
    ),
    "flt": PackageConfig(
        name="flt",
        git_url="https://github.com/ImperialCollegeLondon/FLT",
        module_prefixes=["FLT"],
        version_strategy=VersionStrategy.LATEST,
        depends_on=["mathlib"],
    ),
    "formal-conjectures": PackageConfig(
        name="formal-conjectures",
        git_url="https://github.com/google-deepmind/formal-conjectures",
        module_prefixes=["FormalConjectures", "FormalConjecturesForMathlib"],
        version_strategy=VersionStrategy.LATEST,
        depends_on=["mathlib"],
    ),
    "cslib": PackageConfig(
        name="cslib",
        git_url="https://github.com/leanprover/cslib",
        module_prefixes=["Cslib"],
        version_strategy=VersionStrategy.LATEST,
        depends_on=["mathlib"],
    ),
}
