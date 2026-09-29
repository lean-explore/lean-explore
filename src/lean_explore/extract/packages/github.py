"""GitHub utilities for fetching package metadata.

This module provides functions to interact with GitHub repositories
for fetching toolchain versions and release tags.
"""

import json
import logging
import re
import urllib.request

logger = logging.getLogger(__name__)

_GITHUB_REPO_PATTERN = re.compile(r"github\.com/([^/]+)/([^/]+?)(?:\.git)?$")
_SEMVER_TAG_PATTERN = re.compile(r"^v?\d+\.\d+\.\d+")
_REQUEST_TIMEOUT_SECONDS = 30


def _parse_github_repo(git_url: str) -> tuple[str, str]:
    """Split a GitHub repository URL into owner and repository name.

    Args:
        git_url: GitHub repository URL, optionally ending in ``.git``.

    Returns:
        Tuple of (owner, repo).

    Raises:
        ValueError: If the URL is not a GitHub repository URL.
    """
    match = _GITHUB_REPO_PATTERN.search(git_url)
    if not match:
        raise ValueError(f"Could not parse GitHub URL: {git_url}")
    owner, repo = match.groups()
    return owner, repo


def _read_url(request: str | urllib.request.Request) -> str:
    """Perform an HTTP GET and return the decoded response body.

    Args:
        request: URL or prepared request.

    Returns:
        Response body decoded as UTF-8.
    """
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8")


def github_url_to_raw(git_url: str, branch: str, file_path: str) -> str:
    """Convert GitHub repo URL to raw file URL.

    Args:
        git_url: GitHub repository URL (e.g., https://github.com/owner/repo)
        branch: Branch or tag name
        file_path: Path to file in repo

    Returns:
        Raw GitHub URL for the file.

    Raises:
        ValueError: If the URL is not a GitHub repository URL.
    """
    owner, repo = _parse_github_repo(git_url)
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"


def fetch_lean_toolchain(git_url: str, ref: str = "main") -> str:
    """Fetch lean-toolchain content from a GitHub repository.

    Args:
        git_url: GitHub repository URL
        ref: Branch name or tag (default: main)

    Returns:
        Content of the lean-toolchain file (e.g., 'leanprover/lean4:v4.27.0')

    Raises:
        RuntimeError: If the file cannot be fetched.
    """
    raw_url = github_url_to_raw(git_url, ref, "lean-toolchain")
    logger.info("Fetching lean-toolchain from %s", raw_url)

    try:
        return _read_url(raw_url).strip()
    except Exception as error:
        raise RuntimeError(
            f"Failed to fetch lean-toolchain from {raw_url}: {error}"
        ) from error


def _semver_key(tag: str) -> list[int]:
    """Sort key comparing tags by every integer they contain.

    Known bug (kept for behavior compatibility): the ``rc`` number is just
    another integer, so ``v1.2.0-rc1`` ([1, 2, 0, 1]) sorts above ``v1.2.0``
    ([1, 2, 0]).
    """
    return [int(part) for part in re.findall(r"\d+", tag)]


def _select_latest_tag(tag_names: list[str]) -> str:
    """Pick the newest semver-like tag, or the first tag if none look semver.

    Args:
        tag_names: Tag names in the order GitHub returned them (non-empty).

    Returns:
        The selected tag name.
    """
    semver_tags = [name for name in tag_names if _SEMVER_TAG_PATTERN.match(name)]
    if not semver_tags:
        return tag_names[0]
    return max(semver_tags, key=_semver_key)


def fetch_latest_tag(git_url: str) -> str:
    """Fetch the latest semver tag from a GitHub repository.

    Only the first 100 tags returned by the GitHub API are considered.

    Args:
        git_url: GitHub repository URL

    Returns:
        Latest tag name (e.g., 'v4.26.0')

    Raises:
        ValueError: If the URL is not a GitHub repository URL.
        RuntimeError: If the tags cannot be fetched or none exist.
    """
    owner, repo = _parse_github_repo(git_url)
    api_url = f"https://api.github.com/repos/{owner}/{repo}/tags?per_page=100"
    logger.info("Fetching tags from %s", api_url)

    request = urllib.request.Request(
        api_url, headers={"Accept": "application/vnd.github.v3+json"}
    )
    try:
        tags = json.loads(_read_url(request))
    except Exception as error:
        raise RuntimeError(f"Failed to fetch tags from {api_url}: {error}") from error

    if not tags:
        raise RuntimeError(f"No tags found for {git_url}")
    return _select_latest_tag([tag["name"] for tag in tags])


def extract_lean_version(toolchain: str) -> str:
    """Extract version from lean-toolchain content.

    Args:
        toolchain: Toolchain content like 'leanprover/lean4:v4.27.0'
            or 'leanprover/lean4:v4.28.0-rc1'.

    Returns:
        Version string like 'v4.27.0' or 'v4.28.0-rc1'

    Raises:
        ValueError: If no version can be found.
    """
    match = re.search(r"v\d+\.\d+\.\d+(?:-rc\d+)?", toolchain)
    if not match:
        raise ValueError(f"Could not extract version from toolchain: {toolchain}")
    return match.group()
