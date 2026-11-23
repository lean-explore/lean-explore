"""Turn a package's Lean sources into declaration rows without building it.

The build-free parser in ``src/lean_explore/parser/`` (Python module
``lean_source_parser``) reads every source file of the package and reports each declaration with the
ranges Lean records for it. The rows match what the doc-gen4 path stores for
the same declaration: the same line range, source text, docstring and source
link format.

The parser does not elaborate, so the rows have no dependencies, anonymous
instances are skipped because their names come from the elaborated type, and
names that attributes such as ``to_additive`` derive by rule are left out
unless requested, because they are guesses.
Install the parser with ``pip install ./src/lean_explore/parser/crates/lean-source-py``.
"""

import importlib
import logging
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from lean_explore.extract.docgen.lean_source import select_source_lines
from lean_explore.extract.packages.config import PackageConfig
from lean_explore.extract.source.fetch import read_commit, source_directory
from lean_explore.extract.types import Declaration

logger = logging.getLogger(__name__)

_UNSTORED_KINDS = {
    "example",
    "field",
    "let_rec",
    "parent_projection",
    "proof_wanted",
    "where_decl",
}
"""Parser kinds that doc-gen4 extraction does not store as declarations."""

_IGNORED_DIRECTORIES = {".git", ".lake", "lake-packages"}


def load_parser() -> ModuleType:
    """Import the build-free parser's Python bindings.

    Returns:
        The ``lean_source_parser`` module.

    Raises:
        RuntimeError: If the bindings are not installed.
    """
    try:
        return importlib.import_module("lean_source_parser")
    except ImportError as error:
        raise RuntimeError(
            "Source extraction needs the lean_source_parser module; install it "
            "with `pip install ./src/lean_explore/parser/crates/lean-source-py`"
        ) from error


def _leading_spaces_size(text: str) -> int:
    """Count the indentation that Lean removes from a docstring.

    Ports ``findLeadingSpacesSize`` from Lean's ``Init/Data/String/Extra.lean``:
    the smallest indentation of the lines after the first, where the line that
    follows a blank line is not counted.
    """
    newline = text.find("\n")
    if newline < 0:
        return 0
    minimum = len(text.encode("utf-8"))
    current = 0
    counting = True
    for character in text[newline + 1 :]:
        if counting:
            if character in " \t":
                current += 1
                continue
            if character != "\n":
                minimum = min(current, minimum)
            counting = False
        elif character == "\n":
            counting = True
            current = 0
    return minimum


def lean_docstring(doc_comment: str) -> str:
    """Return the docstring Lean and doc-gen4 store for a doc comment.

    Args:
        doc_comment: The doc comment as written, ``/-- ... -/``.

    Returns:
        The text between the delimiters, de-indented the way Lean's
        ``String.removeLeadingSpaces`` does it, without leading whitespace.
    """
    text = doc_comment.removeprefix("/--").removesuffix("-/")
    size = _leading_spaces_size(text)
    if size:
        lines = []
        for line in text.split("\n"):
            indent = len(line) - len(line.lstrip(" \t"))
            lines.append(line[min(indent, size) :])
        text = "\n".join(lines)
    return text.lstrip()


def source_link_base(git_url: str, commit: str) -> str:
    """Return the GitHub URL prefix of the package's files at a commit.

    Args:
        git_url: Repository URL from the package configuration.
        commit: Commit the sources are checked out at.

    Returns:
        A prefix that a repository-relative path completes to a file URL.
    """
    return f"{git_url.rstrip('/').removesuffix('.git')}/blob/{commit}/"


def _module_name(path: Path, root: Path) -> str:
    """Return the Lean module name of a source file below ``root``."""
    return ".".join(path.relative_to(root).with_suffix("").parts)


def package_files(
    source_root: Path, include_module: Callable[[str], bool]
) -> list[Path]:
    """List the package's Lean files whose modules the package includes.

    Args:
        source_root: Root of the package's repository checkout.
        include_module: Predicate selecting the package's modules.

    Returns:
        The matching ``.lean`` files, sorted by path.
    """
    return sorted(
        path
        for path in source_root.rglob("*.lean")
        if not _IGNORED_DIRECTORIES & set(path.relative_to(source_root).parts)
        and include_module(_module_name(path, source_root))
    )


def is_stored(declaration: dict[str, Any], include_approximate: bool) -> bool:
    """Decide whether doc-gen4 extraction would store a parsed declaration.

    Args:
        declaration: A declaration as ``lean_source_parser`` reports it.
        include_approximate: Keep names that a naming rule derived.

    Returns:
        False for unnamed, private and auxiliary declarations, generated
        ``.mk`` constructors and, unless requested, rule-derived names.
    """
    name = declaration["name"]
    return not (
        name is None
        or name.endswith(".mk")
        or declaration["kind"] in _UNSTORED_KINDS
        or "private" in declaration["modifiers"]
        or (declaration["approximate"] and not include_approximate)
    )


def _to_declaration(
    declaration: dict[str, Any], lines: list[str], path: str, link_base: str
) -> Declaration:
    """Build the stored row for one parsed declaration."""
    _, start_line, _, _, end_line, _ = declaration["range"]
    doc_comment = declaration.get("doc_text")
    return Declaration(
        name=declaration["name"],
        module=declaration["module"],
        docstring=lean_docstring(doc_comment) if doc_comment else None,
        source_text=select_source_lines(lines, start_line, end_line, path),
        source_link=f"{link_base}{declaration['file']}#L{start_line}-L{end_line}",
        dependencies=None,
    )


def parse_package_sources(
    source_root: Path,
    link_base: str,
    include_module: Callable[[str], bool],
    include_approximate: bool = False,
) -> list[Declaration]:
    """Parse a package checkout into declaration rows.

    Args:
        source_root: Root of the package's repository checkout.
        link_base: URL prefix for source links, from ``source_link_base``.
        include_module: Predicate selecting the package's modules.
        include_approximate: Also keep names that a naming rule derived, such
            as ``to_additive`` translations without a written name. They are
            guesses and can name declarations that do not exist.

    Returns:
        One row per stored declaration name, in file order.
    """
    paths = package_files(source_root, include_module)
    parsed = load_parser().parse_files(
        [str(path) for path in paths], root=str(source_root), text=True
    )
    declarations: dict[str, Declaration] = {}
    for path, file_declarations in zip(paths, parsed, strict=True):
        with open(path, encoding="utf-8") as source_file:
            lines = source_file.readlines()
        for declaration in file_declarations:
            name = declaration["name"]
            if is_stored(declaration, include_approximate) and name not in declarations:
                declarations[name] = _to_declaration(
                    declaration, lines, str(path), link_base
                )
    logger.info(
        "Parsed %d source files into %d declarations", len(paths), len(declarations)
    )
    return list(declarations.values())


def extract_source_package(
    lean_root: Path, package_config: PackageConfig
) -> list[Declaration]:
    """Extract a source package from its checkout under ``lean_root``.

    Args:
        lean_root: Root directory containing the package workspaces.
        package_config: Configuration of the package.

    Returns:
        The package's declarations, or an empty list if it was not fetched.
    """
    directory = source_directory(lean_root, package_config.name)
    if not (directory / ".git").exists():
        logger.warning("No sources found for %s at %s", package_config.name, directory)
        return []
    link_base = source_link_base(package_config.git_url, read_commit(directory))
    logger.info("[%s] Parsing sources (no build)", package_config.name)
    return parse_package_sources(
        directory, link_base, package_config.should_include_module
    )
