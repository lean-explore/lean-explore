"""Helpers that build small git repositories for source extraction tests."""

import subprocess
from pathlib import Path


def git(directory: Path, *arguments: str) -> str:
    """Run git in a directory with a fixed test identity and return its output."""
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "init.defaultBranch=main",
            *arguments,
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def make_repository(directory: Path, files: dict[str, str]) -> Path:
    """Create a git repository with one commit holding the given files."""
    directory.mkdir(parents=True, exist_ok=True)
    git(directory, "init", "-q")
    commit_files(directory, files, "Initial commit")
    return directory


def commit_files(directory: Path, files: dict[str, str], message: str) -> str:
    """Write files into a repository, commit them and return the commit hash."""
    for relative_path, text in files.items():
        path = directory / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(directory, "add", "-A")
    git(directory, "commit", "-q", "-m", message)
    return git(directory, "rev-parse", "HEAD")
