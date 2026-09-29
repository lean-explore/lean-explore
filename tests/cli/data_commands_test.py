"""Tests for the CLI data_commands module.

These tests verify the data toolchain management commands including fetch and clean.
"""

import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from lean_explore.cli.data_commands import (
    BM25_DIRECTORIES,
    REQUIRED_FILES,
    _build_download_list,
    _cleanup_old_versions,
    _fetch_latest_version,
    _get_console,
    _install_toolchain,
    _write_active_version,
    app,
)
from lean_explore.config import Config

runner = CliRunner()


class TestGetConsole:
    """Tests for the _get_console helper function."""

    def test_get_console_returns_console(self):
        """Test that _get_console returns a Console instance."""
        console = _get_console()
        assert console is not None


class TestFetchLatestVersion:
    """Tests for the _fetch_latest_version function."""

    def test_fetch_latest_version_success(self):
        """Test successful latest version fetch."""
        mock_response = MagicMock()
        mock_response.text = "20260127_103630\n"
        mock_response.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_response):
            result = _fetch_latest_version()
            assert result == "20260127_103630"

    def test_fetch_latest_version_strips_whitespace(self):
        """Test that version string is stripped of whitespace."""
        mock_response = MagicMock()
        mock_response.text = "  20260127_103630  \n"
        mock_response.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_response):
            result = _fetch_latest_version()
            assert result == "20260127_103630"

    def test_fetch_latest_version_network_error(self):
        """Test latest version fetch with network error."""
        import requests

        with patch("requests.get", side_effect=requests.exceptions.ConnectionError()):
            with pytest.raises(ValueError, match="Failed to fetch latest version"):
                _fetch_latest_version()

    def test_fetch_latest_version_http_error(self):
        """Test latest version fetch with HTTP error."""
        import requests

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError()

        with patch("requests.get", return_value=mock_response):
            with pytest.raises(ValueError, match="Failed to fetch latest version"):
                _fetch_latest_version()

    def test_fetch_latest_version_timeout(self):
        """Test latest version fetch with timeout."""
        import requests

        with patch("requests.get", side_effect=requests.exceptions.Timeout()):
            with pytest.raises(ValueError, match="Failed to fetch latest version"):
                _fetch_latest_version()


class TestWriteActiveVersion:
    """Tests for the _write_active_version function."""

    def test_write_active_version_creates_file(self):
        """Test that active version is written to file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "cache"
            version_file = Path(tmpdir) / "active_version"

            with patch(
                "lean_explore.cli.data_commands.Config.CACHE_DIRECTORY", cache_dir
            ):
                _write_active_version("20260127_103630")
                assert version_file.exists()
                assert version_file.read_text() == "20260127_103630"

    def test_write_active_version_overwrites_existing(self):
        """Test that active version file is overwritten."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "cache"
            version_file = Path(tmpdir) / "active_version"
            version_file.write_text("old_version")

            with patch(
                "lean_explore.cli.data_commands.Config.CACHE_DIRECTORY", cache_dir
            ):
                _write_active_version("new_version")
                assert version_file.read_text() == "new_version"


class TestCleanupOldVersions:
    """Tests for the _cleanup_old_versions function."""

    def test_cleanup_removes_old_versions(self):
        """Test that old version directories are removed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            old_version = cache_dir / "old_version"
            current_version = cache_dir / "current_version"
            old_version.mkdir()
            current_version.mkdir()
            (old_version / "file.txt").touch()
            (current_version / "file.txt").touch()

            with patch(
                "lean_explore.cli.data_commands.Config.CACHE_DIRECTORY", cache_dir
            ):
                _cleanup_old_versions("current_version")
                assert not old_version.exists()
                assert current_version.exists()

    def test_cleanup_handles_nonexistent_cache(self):
        """Test cleanup when cache directory doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nonexistent = Path(tmpdir) / "nonexistent"
            with patch(
                "lean_explore.cli.data_commands.Config.CACHE_DIRECTORY", nonexistent
            ):
                # Should not raise
                _cleanup_old_versions("any_version")


class StubHandler(BaseHTTPRequestHandler):
    """Serves ``server.files``; paths in ``server.truncate`` are cut short."""

    def do_GET(self):  # noqa: N802 - name required by BaseHTTPRequestHandler
        """Serve a stored file, a 404, or a truncated body."""
        self.server.requests.append(self.path)
        body = self.server.files.get(self.path)
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.path in self.server.truncate:
            # Simulate a dropped connection mid-download.
            self.wfile.write(body[: len(body) // 2])
            self.close_connection = True
            return
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        """Silence per-request logging."""


@pytest.fixture
def file_server(tmp_path, monkeypatch):
    """Run a local stand-in for the R2 asset server and point Config at it.

    Serves ``latest.txt`` = "v2" and every toolchain file of version v2 with
    content equal to its own URL path.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    server.requests, server.truncate = [], set()
    base = f"http://127.0.0.1:{server.server_port}"
    server.files = {"/assets/latest.txt": b"v2\n"}
    for url, _ in _build_download_list(f"{base}/assets/v2", Path("unused")):
        path = url.removeprefix(base)
        server.files[path] = path.encode()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(Config, "R2_ASSETS_BASE_URL", base)
    monkeypatch.setattr(Config, "CACHE_DIRECTORY", tmp_path / "cache")
    yield server
    server.shutdown()
    server.server_close()


def installed_files(version_dir: Path) -> dict[str, bytes]:
    """Map each file under version_dir (relative POSIX path) to its bytes."""
    return {
        p.relative_to(version_dir).as_posix(): p.read_bytes()
        for p in version_dir.rglob("*")
        if p.is_file()
    }


EXPECTED_RELATIVE_PATHS = sorted(
    REQUIRED_FILES
    + [f"{d}/{name}" for d, names in BM25_DIRECTORIES.items() for name in names]
)


class TestInstallToolchain:
    """End-to-end install flow against the local stub server."""

    def test_installs_latest_version(self, file_server, tmp_path):
        """Latest version installs fully, activates, and replaces old versions.

        Every file lands under cache/<version>/ with the served content, the
        active_version file is written, and old version directories (but not
        stray files) are removed.
        """
        cache = tmp_path / "cache"
        (cache / "v1").mkdir(parents=True)
        (cache / "v1" / "lean_explore.db").write_bytes(b"old")
        (cache / "notes.txt").write_text("not a version directory")

        _install_toolchain()

        files = installed_files(cache / "v2")
        assert sorted(files) == EXPECTED_RELATIVE_PATHS
        for relative, content in files.items():
            assert content == f"/assets/v2/{relative}".encode()
        assert (tmp_path / "active_version").read_text() == "v2"
        assert not (cache / "v1").exists()
        assert (cache / "notes.txt").exists()
        assert file_server.requests[0] == "/assets/latest.txt"

    def test_explicit_version_skips_latest_lookup(self, file_server, tmp_path):
        """An explicit version is installed without fetching latest.txt."""
        _install_toolchain("v2")
        assert "/assets/latest.txt" not in file_server.requests
        assert (tmp_path / "active_version").read_text() == "v2"

    def test_existing_files_are_not_redownloaded(self, file_server, tmp_path):
        """Files already present in the version directory are skipped."""
        db = tmp_path / "cache" / "v2" / "lean_explore.db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"already here")

        _install_toolchain("v2")

        assert "/assets/v2/lean_explore.db" not in file_server.requests
        assert db.read_bytes() == b"already here"

    def test_http_error_aborts_without_activating(self, file_server, tmp_path):
        """A 404 raises ValueError and leaves the previous install active."""
        (tmp_path / "cache" / "v1").mkdir(parents=True)
        (tmp_path / "active_version").write_text("v1")
        del file_server.files["/assets/v2/bm25_ids_map.json"]

        with pytest.raises(ValueError, match="Failed to download .*bm25_ids_map"):
            _install_toolchain()

        assert (tmp_path / "active_version").read_text() == "v1"
        assert (tmp_path / "cache" / "v1").exists()

    def test_latest_lookup_failure(self, file_server):
        """A missing latest.txt surfaces as ValueError before any download."""
        del file_server.files["/assets/latest.txt"]
        with pytest.raises(ValueError, match="Failed to fetch latest version"):
            _install_toolchain()
        assert file_server.requests == ["/assets/latest.txt"]

    def test_interrupted_download_leaves_partial_file_that_is_reused(
        self, file_server, tmp_path
    ):
        """Documents current behavior: a truncated file is later trusted.

        known bug, fixed separately: downloads are written straight to their
        final path, so an interrupted transfer leaves a partial file that the
        next run skips as "existing" and then activates as if complete.
        """
        path = "/assets/v2/lean_explore.db"
        full = bytes(range(256)) * 200  # Several 8 KiB chunks.
        file_server.files[path] = full
        file_server.truncate.add(path)
        db = tmp_path / "cache" / "v2" / "lean_explore.db"

        with pytest.raises(ValueError, match="Failed to download"):
            _install_toolchain("v2")
        partial = db.read_bytes()
        assert len(partial) < len(full)
        assert full.startswith(partial)

        file_server.truncate.clear()
        file_server.requests.clear()
        _install_toolchain("v2")

        assert path not in file_server.requests
        assert db.read_bytes() == partial
        assert (tmp_path / "active_version").read_text() == "v2"

    def test_fetch_command_end_to_end(self, file_server, tmp_path):
        """The ``fetch`` CLI command installs from the server."""
        result = runner.invoke(app, ["fetch"])
        assert result.exit_code == 0, result.output
        assert "Installed data for version v2" in result.output
        assert (tmp_path / "active_version").read_text() == "v2"


class TestCleanupFailures:
    """Tests for filesystem error handling."""

    def test_cleanup_continues_after_rmtree_error(self, tmp_path, monkeypatch):
        """A failure removing one old version does not stop the others."""
        for name in ("a", "b", "keep"):
            (tmp_path / name).mkdir()
        real_rmtree = shutil.rmtree

        def flaky_rmtree(path):
            if Path(path).name == "a":
                raise OSError("busy")
            real_rmtree(path)

        monkeypatch.setattr(Config, "CACHE_DIRECTORY", tmp_path)
        monkeypatch.setattr(
            "lean_explore.cli.data_commands.shutil.rmtree", flaky_rmtree
        )
        _cleanup_old_versions("keep")

        assert sorted(p.name for p in tmp_path.iterdir()) == ["a", "keep"]

    def test_clean_command_reports_os_error(self, tmp_path, monkeypatch):
        """``clean`` exits with code 1 when deletion fails."""
        (tmp_path / "cache").mkdir()
        monkeypatch.setattr(Config, "CACHE_DIRECTORY", tmp_path / "cache")

        def deny(path):
            raise OSError("permission denied")

        monkeypatch.setattr("lean_explore.cli.data_commands.shutil.rmtree", deny)
        result = runner.invoke(app, ["clean"], input="y\n")

        assert result.exit_code == 1
        assert "Error cleaning data" in result.output


class TestFetchCommand:
    """Tests for the fetch CLI command."""

    def test_fetch_command_help(self):
        """Test fetch command help output."""
        result = runner.invoke(app, ["fetch", "--help"])
        assert result.exit_code == 0
        assert "version" in result.output.lower()

    def test_fetch_command_calls_install(self):
        """Test that fetch command calls _install_toolchain."""
        with patch("lean_explore.cli.data_commands._install_toolchain") as mock_install:
            runner.invoke(app, ["fetch"])
            mock_install.assert_called_once_with(None)

    def test_fetch_command_with_version(self):
        """Test fetch command with specific version."""
        with patch("lean_explore.cli.data_commands._install_toolchain") as mock_install:
            runner.invoke(app, ["fetch", "--version", "20260127_103630"])
            mock_install.assert_called_once_with("20260127_103630")


class TestCleanCommand:
    """Tests for the clean CLI command."""

    def test_clean_command_help(self):
        """Test clean command help output."""
        result = runner.invoke(app, ["clean", "--help"])
        assert result.exit_code == 0

    def test_clean_command_no_data(self):
        """Test clean when no cache directory exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nonexistent = Path(tmpdir) / "nonexistent"
            with patch(
                "lean_explore.cli.data_commands.Config.CACHE_DIRECTORY", nonexistent
            ):
                result = runner.invoke(app, ["clean"])
                assert "No local data" in result.output

    def test_clean_command_aborted(self):
        """Test clean command when user aborts confirmation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "cache"
            cache_dir.mkdir()
            (cache_dir / "test_file").touch()

            with patch(
                "lean_explore.cli.data_commands.Config.CACHE_DIRECTORY", cache_dir
            ):
                runner.invoke(app, ["clean"], input="n\n")
                assert cache_dir.exists()

    def test_clean_command_confirmed(self):
        """Test clean command when user confirms deletion."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "cache"
            cache_dir.mkdir()
            (cache_dir / "test_file").touch()

            with patch(
                "lean_explore.cli.data_commands.Config.CACHE_DIRECTORY", cache_dir
            ):
                result = runner.invoke(app, ["clean"], input="y\n")
                assert not cache_dir.exists()
                assert "cleared" in result.output.lower()

    def test_clean_command_removes_version_file(self):
        """Test that clean also removes the active_version file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "cache"
            cache_dir.mkdir()
            version_file = Path(tmpdir) / "active_version"
            version_file.write_text("some_version")

            with patch(
                "lean_explore.cli.data_commands.Config.CACHE_DIRECTORY", cache_dir
            ):
                result = runner.invoke(app, ["clean"], input="y\n")
                assert not version_file.exists()
                assert "cleared" in result.output.lower()


class TestDataApp:
    """Tests for the data app structure."""

    def test_data_app_no_args_shows_help(self):
        """Test that data app with no args shows help."""
        result = runner.invoke(app)
        # Typer exits with code 2 when no_args_is_help=True and no args provided
        assert result.exit_code in (0, 2)
        # Should show help with available commands
        assert "fetch" in result.output.lower() or "clean" in result.output.lower()
