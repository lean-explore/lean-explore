"""Reader for legacy doc-gen4 BMP JSON output.

Doc-gen4 versions before v4.29.0-rc2 write one JSON file per module to
``.lake/build/doc-data/**/*.bmp``. Each file holds the module name and a list
of declarations with their docstring, GitHub source link, and rendered HTML
header, from which dependencies are scraped.
"""

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lean_explore.extract.docgen_common import drop_self_references, new_progress
from lean_explore.extract.lean_source import SourceTextReader
from lean_explore.extract.types import Declaration


def extract_dependencies_from_html(html: str) -> list[str]:
    """Extract dependency names from HTML declaration header.

    Args:
        html: Rendered declaration header containing ``href="...#Name"`` links.

    Returns:
        Linked declaration names in first-seen order, without duplicates.
    """
    matches = re.findall(r'href="[^"]*#([^"]+)"', html)
    return list(dict.fromkeys(matches))


def _declaration_from_entry(
    entry: dict[str, Any], module_name: str, source_reader: SourceTextReader
) -> Declaration | None:
    """Build a Declaration from one BMP declaration entry.

    Returns:
        The declaration, or None for ``.mk`` constructors and declarations
        whose source text cannot be read.
    """
    information = entry["info"]
    declaration_name = information["name"]

    # Skip auto-generated .mk constructors
    if declaration_name.endswith(".mk"):
        return None

    source_text = source_reader.read(information["sourceLink"], declaration_name)
    if source_text is None:
        return None

    dependencies = extract_dependencies_from_html(entry.get("header", ""))
    return Declaration(
        name=declaration_name,
        module=module_name,
        docstring=information.get("doc"),
        source_text=source_text,
        source_link=information["sourceLink"],
        dependencies=drop_self_references(dependencies, declaration_name),
    )


def _parse_bmp_file(
    file_path: Path,
    include_module: Callable[[str], bool],
    source_reader: SourceTextReader,
) -> list[Declaration]:
    """Parse the declarations of one BMP file if its module is included."""
    with open(file_path, encoding="utf-8") as f:
        data = json.load(f)

    module_name = data["name"]
    if not include_module(module_name):
        return []

    declarations = []
    for entry in data.get("declarations", []):
        declaration = _declaration_from_entry(entry, module_name, source_reader)
        if declaration is not None:
            declarations.append(declaration)
    return declarations


def parse_declarations_from_files(
    bmp_files: list[Path],
    lean_root: Path,
    package_cache: dict[str, Path],
    include_module: Callable[[str], bool],
) -> list[Declaration]:
    """Parse declarations from doc-gen4 BMP files.

    Args:
        bmp_files: List of paths to BMP files containing declaration data.
        lean_root: Root directory of the Lean project.
        package_cache: Dictionary mapping package names to their directories.
        include_module: Predicate selecting the modules to extract, typically
            ``PackageConfig.should_include_module``.

    Returns:
        List of parsed Declaration objects.
    """
    source_reader = SourceTextReader(lean_root, package_cache)
    declarations = []

    with new_progress() as progress:
        task = progress.add_task("[cyan]Parsing BMP files...", total=len(bmp_files))
        for file_path in bmp_files:
            declarations.extend(
                _parse_bmp_file(file_path, include_module, source_reader)
            )
            progress.update(task, advance=1)

    source_reader.log_summary()
    return declarations
