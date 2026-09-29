"""Tests for GitHub metadata helpers, with the HTTP layer stubbed out."""

import json
import urllib.request

import pytest

from lean_explore.extract.packages import github
from lean_explore.extract.packages.github import (
    extract_lean_version,
    fetch_latest_tag,
    fetch_lean_toolchain,
    github_url_to_raw,
)

REPO = "https://github.com/owner/repo"


@pytest.fixture
def responses(monkeypatch) -> dict:
    """Serve canned bodies by URL; missing URLs raise like a 404."""
    bodies: dict = {}

    def read_url(request):
        url = (
            request.full_url if isinstance(request, urllib.request.Request) else request
        )
        if url not in bodies:
            raise OSError("HTTP Error 404: Not Found")
        return bodies[url]

    monkeypatch.setattr(github, "_read_url", read_url)
    return bodies


def _serve_tags(responses: dict, names: list[str]) -> None:
    url = "https://api.github.com/repos/owner/repo/tags?per_page=100"
    responses[url] = json.dumps([{"name": name} for name in names])


class TestUrlHelpers:
    """Tests for URL parsing and version extraction."""

    @pytest.mark.parametrize("url", [REPO, REPO + ".git"])
    def test_github_url_to_raw(self, url):
        """Repository URLs (with or without .git) map to raw URLs."""
        assert github_url_to_raw(url, "v1", "lean-toolchain") == (
            "https://raw.githubusercontent.com/owner/repo/v1/lean-toolchain"
        )

    def test_non_github_url_rejected(self):
        """Non-GitHub URLs raise ValueError."""
        with pytest.raises(ValueError, match="Could not parse GitHub URL"):
            github_url_to_raw("https://gitlab.com/owner/repo", "main", "x")

    @pytest.mark.parametrize(
        ("toolchain", "version"),
        [
            ("leanprover/lean4:v4.27.0", "v4.27.0"),
            ("leanprover/lean4:v4.28.0-rc1\n", "v4.28.0-rc1"),
        ],
    )
    def test_extract_lean_version(self, toolchain, version):
        """The version (including any -rc suffix) is extracted."""
        assert extract_lean_version(toolchain) == version

    def test_extract_lean_version_invalid(self):
        """Toolchains without a version raise ValueError."""
        with pytest.raises(ValueError):
            extract_lean_version("leanprover/lean4:nightly")


class TestReadUrl:
    """Tests for the raw HTTP helper."""

    def test_decodes_body(self, monkeypatch):
        """The response body is read and decoded as UTF-8."""

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return "héllo".encode()

        seen = {}

        def urlopen(request, timeout):
            seen.update(request=request, timeout=timeout)
            return Response()

        monkeypatch.setattr(github.urllib.request, "urlopen", urlopen)
        assert github._read_url("https://x") == "héllo"
        assert seen == {"request": "https://x", "timeout": 30}


class TestFetchLeanToolchain:
    """Tests for fetching lean-toolchain files."""

    def test_returns_stripped_content(self, responses):
        """File content is stripped of surrounding whitespace."""
        url = "https://raw.githubusercontent.com/owner/repo/main/lean-toolchain"
        responses[url] = "leanprover/lean4:v4.27.0\n"
        assert fetch_lean_toolchain(REPO) == "leanprover/lean4:v4.27.0"

    def test_failure_raises_runtime_error(self, responses):
        """HTTP failures are wrapped in RuntimeError."""
        with pytest.raises(RuntimeError, match="Failed to fetch lean-toolchain"):
            fetch_lean_toolchain(REPO, "v1.0.0")


class TestFetchLatestTag:
    """Tests for selecting the latest release tag."""

    def test_picks_highest_semver(self, responses):
        """Tags are compared numerically, not lexically."""
        _serve_tags(responses, ["v1.9.0", "v1.10.0", "v1.2.3", "nightly"])
        assert fetch_latest_tag(REPO) == "v1.10.0"

    def test_accepts_tags_without_v_prefix(self, responses):
        """Bare numeric tags count as semver."""
        _serve_tags(responses, ["1.0.0", "v0.9.0"])
        assert fetch_latest_tag(REPO) == "1.0.0"

    def test_rc_ranks_above_release(self, responses):
        """Current behavior: an -rc tag outranks its own release.

        Known bug, kept for behavior compatibility: the rc number is compared
        as an extra version component, so v1.2.0-rc1 > v1.2.0.
        """
        _serve_tags(responses, ["v1.2.0", "v1.2.0-rc1", "v1.1.0"])
        assert fetch_latest_tag(REPO) == "v1.2.0-rc1"

    def test_falls_back_to_first_tag(self, responses):
        """Without semver tags the first tag returned is used."""
        _serve_tags(responses, ["release-b", "release-a"])
        assert fetch_latest_tag(REPO) == "release-b"

    def test_no_tags(self, responses):
        """An empty tag list raises RuntimeError."""
        _serve_tags(responses, [])
        with pytest.raises(RuntimeError, match="No tags found"):
            fetch_latest_tag(REPO)

    def test_request_failure(self, responses):
        """HTTP failures are wrapped in RuntimeError."""
        with pytest.raises(RuntimeError, match="Failed to fetch tags"):
            fetch_latest_tag(REPO)

    def test_invalid_url(self, responses):
        """Non-GitHub URLs are rejected before any request."""
        with pytest.raises(ValueError):
            fetch_latest_tag("https://example.com/repo")
