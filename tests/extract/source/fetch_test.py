"""Tests for fetching the sources of build-free packages."""

import pytest

from lean_explore.extract.packages.config import PackageConfig
from lean_explore.extract.source import fetch
from lean_explore.extract.source.fetch import (
    checkout_sources,
    fetch_package_sources,
    read_commit,
    source_directory,
)
from tests.extract.source.fixtures import commit_files, git, make_repository


@pytest.fixture
def origin(tmp_path):
    """A local repository standing in for a package's GitHub remote."""
    repository = make_repository(tmp_path / "origin", {"Pkg/A.lean": "def a := 1\n"})
    git(repository, "tag", "v1")
    return repository


def test_source_directory(tmp_path):
    """Sources live next to the package's Lake workspace files."""
    assert source_directory(tmp_path, "flt") == tmp_path / "flt" / "source"


def test_clone_then_update_branch(origin, tmp_path):
    """A first call clones the branch; a later call moves to its new head."""
    clone = tmp_path / "lean" / "pkg" / "source"
    checkout_sources(origin.as_uri(), "main", clone)
    assert (clone / "Pkg" / "A.lean").read_text() == "def a := 1\n"

    head = commit_files(origin, {"Pkg/B.lean": "def b := 2\n"}, "Add B")
    checkout_sources(origin.as_uri(), "main", clone)

    assert read_commit(clone) == head
    assert (clone / "Pkg" / "B.lean").exists()


def test_clone_tag(origin, tmp_path):
    """A tag checks out the tagged commit, not the branch head."""
    tagged = git(origin, "rev-parse", "v1")
    commit_files(origin, {"Pkg/B.lean": "def b := 2\n"}, "Add B")
    clone = tmp_path / "clone"

    checkout_sources(origin.as_uri(), "v1", clone)

    assert read_commit(clone) == tagged
    assert not (clone / "Pkg" / "B.lean").exists()


def test_git_failure_raises(tmp_path):
    """A failed git command raises with git's message."""
    missing = (tmp_path / "missing").as_uri()
    with pytest.raises(RuntimeError, match="git clone"):
        checkout_sources(missing, "main", tmp_path / "clone")


def test_fetch_uses_version_strategy_ref(origin, tmp_path, monkeypatch):
    """The ref comes from the package's version strategy."""
    monkeypatch.setattr(
        fetch, "get_package_toolchain", lambda config: ("toolchain", "v1")
    )
    config = PackageConfig(name="pkg", git_url=origin.as_uri(), module_prefixes=["Pkg"])

    directory = fetch_package_sources(config, tmp_path / "lean")

    assert directory == tmp_path / "lean" / "pkg" / "source"
    assert read_commit(directory) == git(origin, "rev-parse", "v1")
