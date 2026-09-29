"""Manages local Lean Explore data toolchains.

Provides CLI commands to download, install, and clean data files (database,
FAISS index, BM25 indexes, etc.) from remote storage.
"""

import logging
import shutil
from pathlib import Path

import requests
import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TransferSpeedColumn,
)

from lean_explore.config import Config

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="data",
    help="Manage local data toolchains for Lean Explore (e.g., download, list, "
    "select, clean).",
    no_args_is_help=True,
)

# Files required for the search engine (relative to version directory)
REQUIRED_FILES: list[str] = [
    "lean_explore.db",
    "informalization_faiss.index",
    "informalization_faiss_ids_map.json",
    "bm25_ids_map.json",
]

# BM25 index directories and their contents
BM25_DIRECTORIES: dict[str, list[str]] = {
    "bm25_name_raw": [
        "data.csc.index.npy",
        "indices.csc.index.npy",
        "indptr.csc.index.npy",
        "nonoccurrence_array.index.npy",
        "params.index.json",
        "vocab.index.json",
    ],
    "bm25_name_spaced": [
        "data.csc.index.npy",
        "indices.csc.index.npy",
        "indptr.csc.index.npy",
        "nonoccurrence_array.index.npy",
        "params.index.json",
        "vocab.index.json",
    ],
}


def _get_console() -> Console:
    """Create a Rich console instance for output."""
    return Console()


def _fetch_latest_version() -> str:
    """Fetch the latest version identifier from remote storage.

    Returns:
        The version string (e.g., "20260127_103630").

    Raises:
        ValueError: If the latest version cannot be fetched.
    """
    latest_url = f"{Config.R2_ASSETS_BASE_URL}/assets/latest.txt"
    try:
        response = requests.get(latest_url, timeout=10)
        response.raise_for_status()
        return response.text.strip()
    except requests.exceptions.RequestException as error:
        logger.error("Failed to fetch latest version: %s", error)
        raise ValueError(f"Failed to fetch latest version: {error}") from error


def _download_file(url: str, destination: Path, progress: Progress) -> None:
    """Download a file with progress tracking.

    Args:
        url: The URL to download from.
        destination: The local path to save the file.
        progress: Rich progress instance for tracking.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)

    response = requests.get(url, stream=True, timeout=300)
    response.raise_for_status()

    total_size = int(response.headers.get("content-length", 0))
    task_id = progress.add_task(destination.name, total=total_size)

    with open(destination, "wb") as file:
        for chunk in response.iter_content(chunk_size=8192):
            file.write(chunk)
            progress.update(task_id, advance=len(chunk))


def _write_active_version(version: str) -> None:
    """Write the active version to the version file.

    Args:
        version: The version string to write.
    """
    version_file = Config.CACHE_DIRECTORY.parent / "active_version"
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(version)
    logger.info("Set active version to: %s", version)


def _cleanup_old_versions(current_version: str) -> None:
    """Remove all cached versions except the current one.

    Args:
        current_version: The version to keep.
    """
    if not Config.CACHE_DIRECTORY.exists():
        return

    for item in Config.CACHE_DIRECTORY.iterdir():
        if item.is_dir() and item.name != current_version:
            logger.info("Removing old version: %s", item.name)
            try:
                shutil.rmtree(item)
            except OSError as error:
                logger.warning("Failed to remove %s: %s", item.name, error)


def _build_download_list(base_url: str, cache_path: Path) -> list[tuple[str, Path]]:
    """List every (URL, destination) pair that makes up a data toolchain.

    Args:
        base_url: Remote base URL of the version's assets.
        cache_path: Local directory the version is installed into.

    Returns:
        Pairs of source URL and local destination path, top-level files first,
        then each BM25 directory's files.
    """
    files_to_download = [
        (f"{base_url}/{filename}", cache_path / filename) for filename in REQUIRED_FILES
    ]
    for directory_name, directory_files in BM25_DIRECTORIES.items():
        for filename in directory_files:
            url = f"{base_url}/{directory_name}/{filename}"
            files_to_download.append((url, cache_path / directory_name / filename))
    return files_to_download


def _download_missing_files(
    files_to_download: list[tuple[str, Path]], console: Console
) -> None:
    """Download each file whose destination does not already exist.

    Args:
        files_to_download: Pairs of source URL and local destination path.
        console: Console the progress display renders to.

    Raises:
        ValueError: If any download fails.
    """
    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        console=console,
    ) as progress:
        for url, destination in files_to_download:
            if destination.exists():
                logger.info("Skipping existing file: %s", destination.name)
                continue
            try:
                _download_file(url, destination, progress)
            except requests.exceptions.RequestException as error:
                logger.error("Failed to download %s: %s", url, error)
                raise ValueError(f"Failed to download {url}: {error}") from error


def _install_toolchain(version: str | None = None) -> None:
    """Install the data toolchain for the specified version.

    Downloads all required data files (database, FAISS index, BM25 indexes)
    from remote storage. After successful installation, sets this version
    as the active version and cleans up old versions.

    Args:
        version: The version to install. If None, fetches the latest version.

    Raises:
        ValueError: If version fetch fails or download errors occur.
    """
    console = _get_console()

    if version:
        resolved_version = version
    else:
        console.print("Fetching latest version...")
        resolved_version = _fetch_latest_version()

    console.print(f"Installing version: [bold]{resolved_version}[/bold]")

    base_url = f"{Config.R2_ASSETS_BASE_URL}/assets/{resolved_version}"
    cache_path = Config.CACHE_DIRECTORY / resolved_version
    _download_missing_files(_build_download_list(base_url, cache_path), console)

    # Set this version as active and clean up old versions
    _write_active_version(resolved_version)
    _cleanup_old_versions(resolved_version)

    console.print(f"[green]Installed data for version {resolved_version}[/green]")


@app.callback()
def main() -> None:
    """Lean-Explore data CLI.

    This callback exists only to prevent Typer from treating the first
    sub-command as a *default* command when there is otherwise just one.
    """
    pass


@app.command()
def fetch(
    version: str = typer.Option(
        None,
        "--version",
        "-v",
        help="Version to install (e.g., '20260127_103630'). Defaults to latest.",
    ),
) -> None:
    """Fetch and install the data toolchain from remote storage.

    Downloads the database, FAISS index, and BM25 indexes required for
    local search. Automatically cleans up old cached versions.
    """
    _install_toolchain(version)


@app.command("clean")
def clean_data_toolchains() -> None:
    """Remove all downloaded local data toolchains."""
    console = _get_console()

    cache_exists = Config.CACHE_DIRECTORY.exists()
    version_file = Config.CACHE_DIRECTORY.parent / "active_version"
    version_exists = version_file.exists()

    if not cache_exists and not version_exists:
        console.print("[yellow]No local data found to clean.[/yellow]")
        return

    if typer.confirm("Delete all cached data?", default=False, abort=True):
        try:
            if cache_exists:
                shutil.rmtree(Config.CACHE_DIRECTORY)
            if version_exists:
                version_file.unlink()
            console.print("[green]Data cache cleared.[/green]")
        except OSError as error:
            logger.error("Failed to clean cache directory: %s", error)
            console.print(f"[bold red]Error cleaning data: {error}[/bold red]")
            raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
