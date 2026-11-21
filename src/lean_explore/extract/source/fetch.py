"""Fetch the sources of packages that are extracted without a build.

A source package is a plain shallow git clone at ``lean/<package>/source``. It
is checked out at the ref chosen by the package's version strategy and is never
built, so neither Lake nor a Lean toolchain is needed.
"""

import logging
import subprocess
from pathlib import Path

from lean_explore.extract.packages.config import PackageConfig
from lean_explore.extract.packages.workspace import get_package_toolchain

logger = logging.getLogger(__name__)


def source_directory(lean_root: Path, package_name: str) -> Path:
    """Return the directory that holds a source package's clone.

    Args:
        lean_root: Root directory containing the package workspaces.
        package_name: Registry name of the package.

    Returns:
        The clone directory, ``<lean_root>/<package_name>/source``.
    """
    return lean_root / package_name / "source"


def _run_git(arguments: list[str], cwd: Path | None = None) -> str:
    """Run a git command and return its standard output.

    Args:
        arguments: Arguments after ``git``.
        cwd: Directory to run the command in.

    Returns:
        The command's standard output, stripped.

    Raises:
        RuntimeError: If git exits with a non-zero status.
    """
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def checkout_sources(git_url: str, git_ref: str, directory: Path) -> None:
    """Clone a repository at a ref, or move an existing clone to that ref.

    Args:
        git_url: Repository URL or path.
        git_ref: Branch or tag to check out.
        directory: Clone directory.
    """
    if (directory / ".git").exists():
        _run_git(["fetch", "--depth", "1", "origin", git_ref], cwd=directory)
        _run_git(["checkout", "--force", "--detach", "FETCH_HEAD"], cwd=directory)
        return
    directory.parent.mkdir(parents=True, exist_ok=True)
    _run_git(["clone", "--depth", "1", "--branch", git_ref, git_url, str(directory)])


def read_commit(directory: Path) -> str:
    """Return the commit that a source clone has checked out.

    Args:
        directory: Clone directory.

    Returns:
        The full commit hash of ``HEAD``.
    """
    return _run_git(["rev-parse", "HEAD"], cwd=directory)


def fetch_package_sources(package_config: PackageConfig, lean_root: Path) -> Path:
    """Fetch a source package at the ref its version strategy selects.

    Args:
        package_config: Configuration of the package.
        lean_root: Root directory containing the package workspaces.

    Returns:
        The clone directory.
    """
    _, git_ref = get_package_toolchain(package_config)
    directory = source_directory(lean_root, package_config.name)
    logger.info("[%s] Fetching sources at %s", package_config.name, git_ref)
    checkout_sources(package_config.git_url, git_ref, directory)
    logger.info(
        "[%s] Sources at commit %s", package_config.name, read_commit(directory)
    )
    return directory
