"""Locate and read Lean source text for doc-gen4 declarations.

Doc-gen4 output identifies each declaration's source by a GitHub link with a
line range. This module maps those links back to files in the local package
workspaces (or the elan-managed Lean toolchain) and reads the relevant lines.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_SOURCE_LINK_PATTERN = (
    r"github\.com/([^/]+)/([^/]+)/blob/[^/]+/(.+\.lean)#L(\d+)-L(\d+)"
)

_DECLARATION_KEYWORDS = [
    " def ",
    " theorem ",
    " lemma ",
    " instance ",
    " class ",
    " structure ",
    " inductive ",
    " abbrev ",
    ":=",
]
"""Markers showing that an attribute-only snippet has reached its declaration."""

_MAX_LOGGED_SOURCE_ERRORS = 10


def _toolchain_source_path(lean_root: Path, workspaces: list[str]) -> Path | None:
    """Find the elan toolchain ``src/lean`` directory for the first workspace.

    Args:
        lean_root: Root directory containing package workspaces.
        workspaces: Workspace names to check, in priority order.

    Returns:
        Path to the toolchain's Lean sources for the first workspace that has a
        ``lean-toolchain`` file whose toolchain is installed, or None.
    """
    for workspace_name in workspaces:
        toolchain_file = lean_root / workspace_name / "lean-toolchain"
        if toolchain_file.exists():
            version = toolchain_file.read_text().strip().split(":")[-1]
            toolchain_path = (
                Path.home()
                / ".elan"
                / "toolchains"
                / f"leanprover--lean4---{version}"
                / "src"
                / "lean"
            )
            if toolchain_path.exists():
                return toolchain_path
    return None


def build_package_cache(
    lean_root: str | Path, workspace_name: str | None = None
) -> dict[str, Path]:
    """Build a cache of package names to their actual directories.

    When workspace_name is provided, only includes packages from that specific
    workspace's .lake/packages directory. This ensures source files are resolved
    from the correct workspace, avoiding version mismatches between workspaces.

    Args:
        lean_root: Root directory containing package workspaces.
        workspace_name: If provided, only include packages from this workspace.
            If None, includes packages from all workspaces (legacy behavior).

    Returns:
        Dictionary mapping lowercase package names to their directory paths.
        The Lean toolchain sources, if found, are stored under ``"lean4"``.
    """
    from lean_explore.extract.packages.workspace import get_extraction_order

    lean_root = Path(lean_root)
    cache = {}

    workspaces = [workspace_name] if workspace_name else get_extraction_order()

    for ws_name in workspaces:
        packages_directory = lean_root / ws_name / ".lake" / "packages"
        if packages_directory.exists():
            for package_directory in packages_directory.iterdir():
                if package_directory.is_dir():
                    cache[package_directory.name.lower()] = package_directory

    toolchain_workspaces = (
        [workspace_name] if workspace_name else get_extraction_order()
    )
    toolchain_path = _toolchain_source_path(lean_root, toolchain_workspaces)
    if toolchain_path is not None:
        cache["lean4"] = toolchain_path

    return cache


def read_source_lines(file_path: str | Path, line_start: int, line_end: int) -> str:
    """Read specific lines from a source file.

    If the extracted text is just an attribute (like @[to_additive]), extends
    the range to include the full declaration.

    Args:
        file_path: Path to the Lean source file.
        line_start: First line to read (1-based, inclusive).
        line_end: Last line to read (1-based, inclusive).

    Returns:
        The source text for the requested line range.

    Raises:
        ValueError: If the line range is out of bounds for the file.
    """
    file_path = Path(file_path)
    with open(file_path, encoding="utf-8") as f:
        lines = f.readlines()
    if line_start > len(lines) or line_end > len(lines):
        raise ValueError(
            f"Line range {line_start}-{line_end} out of bounds for {file_path}"
        )

    result = "".join(lines[line_start - 1 : line_end])

    # If result starts with an attribute, extend to get the full declaration
    if result.strip().startswith("@["):
        extended_end = line_end
        while extended_end < len(lines):
            extended_end += 1
            extended_result = "".join(lines[line_start - 1 : extended_end])
            if any(keyword in extended_result for keyword in _DECLARATION_KEYWORDS):
                return extended_result.rstrip()
        return "".join(lines[line_start - 1 : extended_end]).rstrip()

    return result


def _parse_source_link(source_link: str) -> tuple[str, str, int, int]:
    """Split a GitHub source link into its components.

    Args:
        source_link: GitHub URL of the form ``.../blob/<ref>/<path>.lean#La-Lb``.

    Returns:
        Tuple of (package name, file path in repository, start line, end line).

    Raises:
        ValueError: If the link does not match the expected format.
    """
    match = re.search(_SOURCE_LINK_PATTERN, source_link)
    if not match:
        raise ValueError(f"Could not parse source link: {source_link}")
    _, package_name, file_path_string, line_start, line_end = match.groups()
    return package_name, file_path_string, int(line_start), int(line_end)


def _path_in_package(variant: str, package_root: Path, file_path_string: str) -> Path:
    """Map a repository file path onto a cached package directory.

    The Lean toolchain (``lean4``) is cached as its ``src/lean`` directory, so
    repository paths under ``src/lean/``, ``src/lake/``, and ``src/`` are
    rebased accordingly. Other packages use the repository path unchanged.
    """
    if variant == "lean4":
        if file_path_string.startswith("src/lean/"):
            return package_root / file_path_string[9:]
        if file_path_string.startswith("src/lake/"):
            return package_root.parent / "lake" / file_path_string[9:]
        if file_path_string.startswith("src/"):
            return package_root / file_path_string[4:]
    return package_root / file_path_string


def _candidate_paths(
    package_name: str,
    file_path_string: str,
    lean_root: Path,
    package_cache: dict[str, Path],
) -> list[Path]:
    """List local paths that may hold a file, in lookup priority order."""
    candidates = []
    for variant in [
        package_name.lower(),
        package_name.rstrip("0123456789").lower(),
        package_name.replace("-", "").lower(),
    ]:
        if variant in package_cache:
            candidates.append(
                _path_in_package(variant, package_cache[variant], file_path_string)
            )
    candidates.append(lean_root / file_path_string)
    # Last resort: the same relative path inside any cached package.
    candidates.extend(
        package_directory / file_path_string
        for package_directory in package_cache.values()
    )
    return candidates


def extract_source_text(
    source_link: str, lean_root: str | Path, package_cache: dict[str, Path]
) -> str:
    """Extract source text from a Lean file given a GitHub source link.

    Args:
        source_link: GitHub URL with a ``#L<start>-L<end>`` line range.
        lean_root: Root directory of the Lean package workspaces.
        package_cache: Mapping of package names to local directories.

    Returns:
        The source text of the linked line range.

    Raises:
        ValueError: If the link cannot be parsed or the line range is invalid.
        FileNotFoundError: If no local copy of the linked file exists.
    """
    package_name, file_path_string, line_start, line_end = _parse_source_link(
        source_link
    )
    candidates = _candidate_paths(
        package_name, file_path_string, Path(lean_root), package_cache
    )
    for candidate in candidates:
        if candidate.exists():
            return read_source_lines(candidate, line_start, line_end)

    raise FileNotFoundError(
        f"Could not find {file_path_string} for package {package_name}"
    )


@dataclass
class SourceTextReader:
    """Reads declaration source text, counting and logging failures.

    Attributes:
        lean_root: Root directory of the Lean package workspaces.
        package_cache: Mapping of package names to local directories.
        errors: Number of declarations whose source could not be read.
    """

    lean_root: Path
    package_cache: dict[str, Path]
    errors: int = 0

    def read(self, source_link: str, declaration_name: str) -> str | None:
        """Read the source text for one declaration.

        Args:
            source_link: GitHub source link of the declaration.
            declaration_name: Declaration name, used in debug logging.

        Returns:
            The source text, or None if it could not be located or read.
        """
        try:
            return extract_source_text(source_link, self.lean_root, self.package_cache)
        except (FileNotFoundError, ValueError) as error:
            self.errors += 1
            if self.errors <= _MAX_LOGGED_SOURCE_ERRORS:
                logger.debug(
                    "Could not extract source for %s: %s", declaration_name, error
                )
            return None

    def log_summary(self) -> None:
        """Log a warning if any declaration's source text could not be read."""
        if self.errors > 0:
            logger.warning(
                "Could not extract source text for %d declarations", self.errors
            )


def read_lean_toolchain_version(workspace_path: Path) -> str | None:
    """Read the Lean version from a workspace's lean-toolchain file.

    Args:
        workspace_path: Path to the package workspace (e.g., lean/mathlib).

    Returns:
        Version string like 'v4.29.0-rc6', or None if not found.
    """
    toolchain_file = workspace_path / "lean-toolchain"
    if not toolchain_file.exists():
        return None
    try:
        content = toolchain_file.read_text().strip()
        match = re.search(r"v\d+\.\d+\.\d+(?:-rc\d+)?", content)
        return match.group() if match else None
    except OSError:
        return None


def construct_source_link(
    module_name: str,
    module_source_url: str | None,
    start_line: int,
    end_line: int,
    lean_version: str | None = None,
) -> str | None:
    """Construct a GitHub source link from module URL and line range.

    Args:
        module_name: Lean module name from api-docs.db.
        module_source_url: GitHub URL to the module file from api-docs.db.
        start_line: Start line number in the source file.
        end_line: End line number in the source file.
        lean_version: Lean toolchain version (e.g., 'v4.29.0-rc6') used as
            the git ref for core module fallback URLs.

    Returns:
        GitHub URL with line range fragment, or None if no source URL exists.
    """
    if module_source_url:
        return f"{module_source_url}#L{start_line}-L{end_line}"

    git_ref = lean_version or "master"
    module_path = module_name.replace(".", "/")
    root = module_name.split(".", 1)[0]
    if root in {"Init", "Lean", "Std"}:
        return (
            f"https://github.com/leanprover/lean4/blob/{git_ref}/"
            f"src/lean/{module_path}.lean#L{start_line}-L{end_line}"
        )
    if root == "Lake":
        return (
            f"https://github.com/leanprover/lean4/blob/{git_ref}/"
            f"src/lake/{module_path}.lean#L{start_line}-L{end_line}"
        )

    return None
