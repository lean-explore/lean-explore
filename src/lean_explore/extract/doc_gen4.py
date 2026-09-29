"""Documentation generation using doc-gen4 for each package.

This module provides functionality to run doc-gen4 on each package workspace
to generate Lean documentation data for the extraction pipeline.
"""

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from lean_explore.extract.github import extract_lean_version
from lean_explore.extract.package_config import PackageConfig
from lean_explore.extract.package_registry import PACKAGE_REGISTRY
from lean_explore.extract.package_utils import (
    get_extraction_order,
    get_package_toolchain,
    update_lakefile_docgen_version,
)

logger = logging.getLogger(__name__)

WORKSPACES_ROOT = Path("lean")
"""Directory holding one Lake workspace per package, relative to the CWD."""


def _uses_sqlite_docgen(lean_version: str) -> bool:
    """Return whether the matching doc-gen4 release writes api-docs.db.

    Doc-gen4's SQLite output landed after `v4.29.0-rc1` and is present starting
    with `v4.29.0-rc2`.
    """
    match = extract_lean_version(lean_version)
    version_match = re.match(r"^v(\d+)\.(\d+)\.(\d+)(?:-rc(\d+))?$", match)
    if version_match is None:
        return False

    major, minor, patch, rc = version_match.groups()
    version = (int(major), int(minor), int(patch))
    if version > (4, 29, 0):
        return True
    if version < (4, 29, 0):
        return False

    if rc is None:
        return True
    return int(rc) >= 2


def _clear_workspace_cache(workspace_path: Path) -> None:
    """Clear entire Lake cache to force complete rebuild.

    Removes the .lake/ directory and lake-manifest.json to ensure:
    1. Fresh dependency resolution (latest compatible versions)
    2. Fresh doc-gen4 output (regenerated BMP files)
    3. No stale build artifacts

    Use this for nightly updates to get a clean build from scratch.

    Args:
        workspace_path: Path to the package workspace.
    """
    manifest = workspace_path / "lake-manifest.json"
    if manifest.exists():
        logger.info("Removing %s", manifest)
        manifest.unlink()

    lake_dir = workspace_path / ".lake"
    if lake_dir.exists():
        logger.info("Removing %s to force complete rebuild", lake_dir)
        shutil.rmtree(lake_dir)


def _get_library_names(package_name: str) -> list[str]:
    """Get the library names to build and run doc-gen4 on for a package.

    Some packages have custom extract wrappers, others use upstream libraries directly.
    """
    lib_names: dict[str, list[str]] = {
        "mathlib": ["MathExtract"],
        "physlean": ["PhysExtract"],
        "flt": ["FLTExtract"],
        "formal-conjectures": ["FormalConjectures", "FormalConjecturesForMathlib"],
        "cslib": ["CslibExtract"],
    }
    return lib_names.get(package_name, [f"{package_name.title()}Extract"])


def _run_lake_build_target(
    workspace_path: Path,
    package_name: str,
    target: str,
    env: dict[str, str],
    allow_failure: bool = False,
) -> bool:
    """Run ``lake build <target>`` streaming output to the logger.

    Args:
        workspace_path: Path to the Lake workspace directory.
        package_name: Name of the package for log messages.
        target: The Lake build target to run.
        env: Environment variables to pass to the subprocess.
        allow_failure: Whether to continue when the target fails.

    Returns:
        ``True`` if the target built successfully, otherwise ``False``.

    Raises:
        RuntimeError: If the target fails and ``allow_failure`` is ``False``.
    """
    logger.info("[%s] Running lake build %s...", package_name, target)
    process = subprocess.Popen(
        ["lake", "build", target],
        cwd=workspace_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    if process.stdout:
        for line in process.stdout:
            logger.info(line.rstrip())

    returncode = process.wait()
    if returncode == 0:
        return True

    if allow_failure:
        logger.warning(
            "[%s] lake build %s failed (continuing with generated docs)",
            package_name,
            target,
        )
        return False

    raise RuntimeError(f"lake build failed for {package_name} target {target}")


def _workspace_path(package_name: str) -> Path:
    """Return the Lake workspace directory for a package."""
    return WORKSPACES_ROOT / package_name


def _setup_workspace(package_config: PackageConfig) -> tuple[str, str]:
    """Fetch toolchain from GitHub and update lakefile.

    Args:
        package_config: Configuration of the package to set up.

    Returns:
        Tuple of (lean_toolchain, git_ref).
    """
    workspace_path = _workspace_path(package_config.name)
    lakefile_path = workspace_path / "lakefile.lean"
    toolchain_file = workspace_path / "lean-toolchain"

    # NOTE: git_ref is only reported; it is not written into the lakefile, so
    # TAGGED packages still build whatever revision the lakefile requires.
    lean_toolchain, git_ref = get_package_toolchain(package_config)
    lean_version = extract_lean_version(lean_toolchain)

    update_lakefile_docgen_version(lakefile_path, lean_version)
    toolchain_file.write_text(lean_toolchain + "\n")

    return lean_toolchain, git_ref


def _run_lake_update_with_retry(
    workspace_path: Path,
    package_name: str,
    env: dict[str, str],
    verbose: bool = False,
    max_retries: int = 3,
    base_delay: float = 30.0,
) -> None:
    """Run ``lake update`` with retries for transient network failures.

    Large repositories like mathlib4 require cloning several gigabytes of git
    data. Transient network issues (DNS blips, connection resets, GitHub
    throttling) can cause the clone to fail with git exit code 128. Retrying
    with exponential backoff handles these cases.

    Args:
        workspace_path: Path to the Lake workspace directory.
        package_name: Name of the package (for log messages).
        env: Environment variables to pass to the subprocess.
        verbose: Log stdout from ``lake update``.
        max_retries: Maximum number of retry attempts after the initial try.
        base_delay: Seconds to wait before the first retry. Doubles each retry.
    """
    for attempt in range(1, max_retries + 2):
        logger.info("[%s] Running lake update (attempt %d)...", package_name, attempt)
        result = subprocess.run(
            ["lake", "update"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            env=env,
        )
        if verbose and result.stdout:
            logger.info(result.stdout)
        if result.returncode == 0:
            return

        if attempt <= max_retries:
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "[%s] lake update failed (attempt %d), retrying in %.0fs...",
                package_name,
                attempt,
                delay,
            )
            logger.warning("[%s] stderr: %s", package_name, result.stderr.strip())
            time.sleep(delay)
        else:
            logger.error(result.stderr)
            raise RuntimeError(f"lake update failed for {package_name}")


def _lake_env() -> dict[str, str]:
    """Return the environment for Lake subprocesses.

    Mathlib's post-update cache hook is disabled because the cache is fetched
    explicitly by ``_fetch_mathlib_cache``.
    """
    env = os.environ.copy()
    env["MATHLIB_NO_CACHE_ON_UPDATE"] = "1"
    return env


def _fetch_mathlib_cache(
    workspace_path: Path, package_name: str, env: dict[str, str], verbose: bool
) -> None:
    """Run ``lake exe cache get``, logging (not raising) on failure.

    Args:
        workspace_path: Path to the Lake workspace directory.
        package_name: Name of the package for log messages.
        env: Environment variables to pass to the subprocess.
        verbose: Log stdout from the cache command.
    """
    logger.info("[%s] Fetching mathlib cache...", package_name)
    result = subprocess.run(
        ["lake", "exe", "cache", "get"],
        cwd=workspace_path,
        capture_output=True,
        text=True,
        env=env,
    )
    if verbose and result.stdout:
        logger.info(result.stdout)
    if result.returncode != 0:
        logger.warning("[%s] Cache fetch failed (non-fatal)", package_name)


def _docgen_facet(workspace_path: Path) -> str:
    """Choose the doc-gen4 Lake facet to build for a workspace.

    For SQLite-format doc-gen4 we only need ``:docInfo``, which populates
    api-docs.db and stops there. The ``:docs`` facet additionally runs
    ``fromDb`` to generate all HTML and the search index, which we don't
    consume. Older BMP-format doc-gen4 has no ``:docInfo`` facet, so ``:docs``
    is the only option there (and the fallback when no toolchain is pinned).

    Args:
        workspace_path: Path to the Lake workspace directory.

    Returns:
        ``"docInfo"`` or ``"docs"``.
    """
    toolchain_file = workspace_path / "lean-toolchain"
    if toolchain_file.is_file():
        toolchain = toolchain_file.read_text().strip()
        if toolchain and _uses_sqlite_docgen(toolchain):
            return "docInfo"
    return "docs"


def _run_lake_for_package(package_name: str, verbose: bool = False) -> None:
    """Run lake update, cache get, and doc-gen4 for a package.

    Args:
        package_name: Registry name of the package.
        verbose: Log stdout from Lake commands.
    """
    workspace_path = _workspace_path(package_name)
    package_config = PACKAGE_REGISTRY[package_name]
    env = _lake_env()

    _run_lake_update_with_retry(workspace_path, package_name, env, verbose)

    if "mathlib" in package_config.depends_on or package_name == "mathlib":
        _fetch_mathlib_cache(workspace_path, package_name, env, verbose)

    facet = _docgen_facet(workspace_path)
    for lib_name in _get_library_names(package_name):
        # NOTE: build failures are tolerated, so a failed build silently
        # reuses whatever api-docs.db / doc-data a previous run left behind.
        _run_lake_build_target(
            workspace_path,
            package_name,
            f"{lib_name}:{facet}",
            env,
            allow_failure=True,
        )


def _prepare_package(config: PackageConfig, setup: bool, fresh: bool) -> None:
    """Set up a package workspace and, if fresh, clear stale Lake state.

    Doc-gen4 switched from BMP files to api-docs.db in v4.29.0-rc2. The
    SQLite format handles incremental updates, while legacy BMP output
    requires a cache clear to avoid stale files. Without ``setup`` the
    toolchain is unknown, so a fresh run always clears the cache.

    Args:
        config: Configuration of the package to prepare.
        setup: Fetch the toolchain and update the lakefile.
        fresh: Clear cached dependencies for legacy doc-gen4 workspaces.
    """
    toolchain = None
    if setup:
        toolchain, ref = _setup_workspace(config)
        logger.info("Toolchain: %s, ref: %s", toolchain, ref)

    if not fresh:
        return
    if toolchain and _uses_sqlite_docgen(toolchain):
        logger.info(
            "[%s] Skipping cache clear (api-docs.db handles incremental updates)",
            config.name,
        )
    else:
        _clear_workspace_cache(_workspace_path(config.name))


async def run_doc_gen4(
    packages: list[str] | None = None,
    setup: bool = True,
    fresh: bool = False,
    verbose: bool = False,
) -> None:
    """Run doc-gen4 for each package to generate documentation data.

    Args:
        packages: List of package names to process. If None, processes all packages
            in dependency order.
        setup: Whether to fetch toolchain and update lakefile before building.
        fresh: Clear cached dependencies to force fresh resolution. Use this for
            nightly updates to get the latest compatible versions of all packages.
        verbose: Enable verbose logging.

    Raises:
        ValueError: If a package is not in the registry.
        RuntimeError: If any build step fails.
    """
    if packages is None:
        packages = get_extraction_order()

    logger.info("Running doc-gen4 for packages: %s", ", ".join(packages))

    for package_name in packages:
        if package_name not in PACKAGE_REGISTRY:
            raise ValueError(f"Unknown package: {package_name}")

        logger.info("\n%s\nPackage: %s\n%s", "=" * 50, package_name, "=" * 50)
        _prepare_package(PACKAGE_REGISTRY[package_name], setup, fresh)
        _run_lake_for_package(package_name, verbose)

    logger.info("doc-gen4 generation complete for all packages")
