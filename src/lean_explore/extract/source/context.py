"""Find the source files that give a source package its context.

The build-free parser resolves a notation (``+``, ``ℝ``, ``⥤``) to the
constants it stands for by reading the command that declares it, and it follows
a field access through the declaration of the field. For names and notations
that come from outside the package it needs the source files that declare
them: those of Lean itself and of the package's dependencies. Nothing is built
for this; the files are read where they already are.
"""

import os
from pathlib import Path

from lean_explore.extract.packages.config import PackageConfig
from lean_explore.extract.source.fetch import source_directory

_TOOLCHAIN_FILE = "lean-toolchain"


def toolchain_source_directory(package_directory: Path) -> Path | None:
    """Return the directory with the sources of a package's Lean toolchain.

    elan keeps the sources of an installed toolchain under
    ``<elan home>/toolchains/<toolchain>/src/lean``.

    Args:
        package_directory: Directory with the package's ``lean-toolchain`` file.

    Returns:
        The source directory, or None if the package names no toolchain or
        the toolchain is not installed.
    """
    toolchain_file = package_directory / _TOOLCHAIN_FILE
    if not toolchain_file.is_file():
        return None
    toolchain = toolchain_file.read_text(encoding="utf-8").strip()
    if not toolchain:
        return None
    elan_home = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan"))
    # elan writes `leanprover/lean4:v4.36.0` as `leanprover--lean4---v4.36.0`.
    directory_name = toolchain.replace("/", "--").replace(":", "---")
    directory = elan_home / "toolchains" / directory_name / "src" / "lean"
    return directory if directory.is_dir() else None


def _dependency_directories(package_config: PackageConfig) -> list[tuple[str, str]]:
    """List the packages a package depends on, directly or through another.

    Returns:
        Each dependency's name with the subdirectory that holds its modules.
        A dependency that is not registered has no further dependencies.
    """
    from lean_explore.extract.packages.registry import PACKAGE_REGISTRY

    found: dict[str, str] = {}
    waiting = list(package_config.depends_on)
    while waiting:
        name = waiting.pop(0)
        if name in found:
            continue
        dependency = PACKAGE_REGISTRY.get(name)
        found[name] = dependency.source_subdirectory.strip("/") if dependency else ""
        waiting.extend(dependency.depends_on if dependency else [])
    return list(found.items())


def context_source_files(lean_root: Path, package_config: PackageConfig) -> list[Path]:
    """List the Lean files outside a source package that it can refer to.

    These are the sources of the package's Lean toolchain, if elan has it
    installed, and of every package it depends on, directly or through another
    package: the Lake packages of a dependency that was built for doc-gen4
    (``lean/<dependency>/.lake/packages``) and the modules of a dependency
    that is itself a source package.

    Args:
        lean_root: Root directory containing the package workspaces.
        package_config: Configuration of the source package.

    Returns:
        The files that exist, sorted within each directory. The list is empty
        if neither the toolchain nor a dependency's sources are present.
    """
    directories = [
        toolchain_source_directory(source_directory(lean_root, package_config.name))
    ]
    for name, subdirectory in _dependency_directories(package_config):
        directories.append(lean_root / name / ".lake" / "packages")
        directories.append(source_directory(lean_root, name) / subdirectory)
    files: list[Path] = []
    for directory in directories:
        if directory is not None and directory.is_dir():
            files.extend(sorted(directory.rglob("*.lean")))
    return files
