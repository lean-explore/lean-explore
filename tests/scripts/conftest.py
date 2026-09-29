"""Shared fixtures for documentation data generation tests."""

import pathlib
import textwrap
from collections.abc import Callable

import pytest
from griffe import Module

from scripts.docs_data.traversal import load_root_module

PackageFactory = Callable[..., Module]


def _write_package(
    root: pathlib.Path, name: str, files: dict[str, str]
) -> pathlib.Path:
    """Writes a package's source files beneath ``root/src``.

    Args:
        root: Directory that will contain the ``src`` directory.
        name: Package name.
        files: Mapping of package-relative file paths to (dedented) source code.

    Returns:
        The package directory, relative to root.
    """
    package_path = pathlib.Path("src") / name
    for relative_path, source in files.items():
        file_path = root / package_path / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(textwrap.dedent(source), encoding="utf-8")
    return package_path


@pytest.fixture
def load_package(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> PackageFactory:
    """Returns a factory that writes a temporary package and loads it with griffe.

    The working directory is changed to the temporary root so serialized file
    paths are relative to it, as in real runs from the repository root.
    """
    monkeypatch.chdir(tmp_path)

    def factory(files: dict[str, str], name: str = "pkg") -> Module:
        return load_root_module(_write_package(tmp_path, name, files))

    return factory
