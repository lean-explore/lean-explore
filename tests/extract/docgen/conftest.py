"""Fixtures for the tests of extraction from doc-gen4 output."""

import pytest

from lean_explore.extract.packages.config import PackageConfig, VersionStrategy
from lean_explore.extract.packages.registry import PACKAGE_REGISTRY

DOCGEN_REGISTRY = {
    "mathlib": PackageConfig(
        name="mathlib",
        git_url="https://github.com/leanprover-community/mathlib4",
        module_prefixes=["Mathlib", "Batteries", "Init", "Lean", "Std"],
        version_strategy=VersionStrategy.LATEST,
    ),
    "physlean": PackageConfig(
        name="physlean",
        git_url="https://github.com/HEPLean/PhysLean",
        module_prefixes=["Physlib", "QuantumInfo"],
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
"""Packages that are built and read from doc-gen4 output."""


@pytest.fixture(autouse=True)
def docgen_registry():
    """Register built packages, since the real registry holds source packages."""
    original = dict(PACKAGE_REGISTRY)
    PACKAGE_REGISTRY.clear()
    PACKAGE_REGISTRY.update(DOCGEN_REGISTRY)
    yield PACKAGE_REGISTRY
    PACKAGE_REGISTRY.clear()
    PACKAGE_REGISTRY.update(original)
