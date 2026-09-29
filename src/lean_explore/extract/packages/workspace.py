"""Utility functions for package configuration.

This module provides helper functions for working with the package registry,
including dependency ordering, toolchain resolution, and lakefile manipulation.
"""

import logging
import re
from pathlib import Path

from lean_explore.extract.packages.config import PackageConfig, VersionStrategy
from lean_explore.extract.packages.github import fetch_latest_tag, fetch_lean_toolchain
from lean_explore.extract.packages.registry import PACKAGE_REGISTRY

logger = logging.getLogger(__name__)

_DEFAULT_BRANCHES = ("main", "master")
"""Branches tried, in order, for packages using ``VersionStrategy.LATEST``."""

_DOCGEN_REQUIRE_PATTERN = re.compile(
    r"require «doc-gen4» from git\s+"
    r'"https://github\.com/leanprover/doc-gen4"(?:\s+@\s+"[^"]*")?'
)


def get_extraction_order() -> list[str]:
    """Get packages in dependency order for extraction.

    Returns:
        Registry package names ordered so dependencies come before dependents.
        Dependencies missing from the registry are skipped.
    """
    result: list[str] = []
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        configuration = PACKAGE_REGISTRY.get(name)
        if configuration:
            for dep in configuration.depends_on:
                visit(dep)
            result.append(name)

    for name in PACKAGE_REGISTRY:
        visit(name)

    return result


def _fetch_default_branch_toolchain(
    package_configuration: PackageConfig,
) -> tuple[str, str]:
    """Fetch the toolchain from the first default branch that has one.

    Args:
        package_configuration: Package configuration.

    Returns:
        Tuple of (lean_toolchain, branch).

    Raises:
        RuntimeError: If neither ``main`` nor ``master`` has a toolchain file.
    """
    for branch in _DEFAULT_BRANCHES:
        try:
            toolchain = fetch_lean_toolchain(package_configuration.git_url, branch)
        except RuntimeError:
            continue
        return toolchain, branch
    raise RuntimeError(
        f"Could not fetch toolchain from main or master for "
        f"{package_configuration.name}"
    )


def get_package_toolchain(package_configuration: PackageConfig) -> tuple[str, str]:
    """Get the toolchain and ref for a package based on its version strategy.

    Args:
        package_configuration: Package configuration

    Returns:
        Tuple of (lean_toolchain, git_ref) where git_ref is the branch/tag to use.

    Raises:
        RuntimeError: If the toolchain or tags cannot be fetched.
    """
    if package_configuration.version_strategy == VersionStrategy.LATEST:
        return _fetch_default_branch_toolchain(package_configuration)

    latest_tag = fetch_latest_tag(package_configuration.git_url)
    toolchain = fetch_lean_toolchain(package_configuration.git_url, latest_tag)
    return toolchain, latest_tag


def update_lakefile_docgen_version(lakefile_path: Path, lean_version: str) -> None:
    """Update the doc-gen4 version in a lakefile to match the Lean toolchain.

    Doc-gen4 releases are tagged to match Lean toolchain versions, so pinning
    to the same version ensures compatibility. The doc-gen4 ``require`` must
    appear before the main package ``require`` so that the main package's
    transitive dependency versions take precedence during resolution.

    The file is rewritten only when the content changes; a lakefile without a
    doc-gen4 ``require`` is left untouched.

    Args:
        lakefile_path: Path to lakefile.lean
        lean_version: Lean version like 'v4.27.0'
    """
    content = lakefile_path.read_text()
    replacement = (
        f"require «doc-gen4» from git\n"
        f'  "https://github.com/leanprover/doc-gen4" @ "{lean_version}"'
    )
    new_content = _DOCGEN_REQUIRE_PATTERN.sub(replacement, content)

    if new_content != content:
        lakefile_path.write_text(new_content)
        logger.info("Updated doc-gen4 version to %s in %s", lean_version, lakefile_path)
