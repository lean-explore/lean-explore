"""Package loading, module collection, and JSON output for documentation data."""

import json
import logging
import pathlib

from griffe import Alias, AliasResolutionError, GriffeLoader, Module, get_logger

from .objects import serialize_module
from .schema import ModuleDict

PACKAGE_PATH = pathlib.Path("src/lean_explore")
"""Default package directory to document, relative to the working directory."""

OUTPUT_PATH = pathlib.Path("data/module_data.json")
"""Default JSON output path, relative to the working directory."""

logger = logging.getLogger(__name__)


def is_target_package_module(
    module: Module, package_name: str, package_path: pathlib.Path
) -> bool:
    """Checks if a module belongs to the target package.

    Uses both filepath and canonical name to determine membership. The name check
    is a plain prefix match, so a sibling such as ``lean_explore_extra`` would
    also match ``lean_explore``; this is kept for output compatibility.

    Args:
        module: The module to check.
        package_name: Name of the root package.
        package_path: Directory of the root package.

    Returns:
        True if the module lies under package_path or its name has the package
        name as a prefix.
    """
    if module.filepath:
        absolute_module_path = module.filepath.resolve()
        absolute_package_path = package_path.resolve()
        if (
            absolute_package_path == absolute_module_path
            or absolute_package_path in absolute_module_path.parents
        ):
            return True

    canonical_name = module.canonical_path
    return bool(
        package_name
        and isinstance(canonical_name, str)
        and canonical_name.startswith(package_name)
    )


def collect_modules_recursively(
    module: Module,
    package_name: str,
    processed: set[str],
    package_path: pathlib.Path = PACKAGE_PATH,
) -> list[ModuleDict]:
    """Recursively collects and serializes all modules in the package.

    Args:
        module: The current module to process.
        package_name: Name of the root package.
        processed: Set of already processed module paths to avoid duplicates.
        package_path: Directory of the root package.

    Returns:
        List of serialized module dictionaries.
    """
    if (
        not module
        or not hasattr(module, "canonical_path")
        or module.canonical_path in processed
    ):
        return []

    if not is_target_package_module(module, package_name, package_path):
        return []

    logger.info("Processing module: %s", module.canonical_path)
    processed.add(module.canonical_path)

    modules = [serialize_module(module)]

    for member in module.members.values():
        try:
            if member.is_module:
                actual_member = member.final_target if member.is_alias else member
                if actual_member:
                    modules.extend(
                        collect_modules_recursively(
                            actual_member, package_name, processed, package_path
                        )
                    )
        except AliasResolutionError:
            # External module imports cannot be resolved, skip them
            continue

    return modules


def load_root_module(package_path: pathlib.Path) -> Module:
    """Loads a package with griffe and returns its resolved root module.

    Args:
        package_path: Directory of the package to load; its parent is used as
            the griffe search path.

    Returns:
        The root griffe module of the package.

    Raises:
        ValueError: If the loaded package does not resolve to a module.
    """
    loader = GriffeLoader(
        search_paths=[str(package_path.parent)], docstring_parser="google"
    )
    package_name = package_path.name
    root_package = loader.load(package_name)
    loader.resolve_aliases(implicit=True, external=False)

    if isinstance(root_package, Module):
        return root_package
    if isinstance(root_package, Alias) and isinstance(
        root_package.resolved_target, Module
    ):
        return root_package.resolved_target
    raise ValueError(f"Failed to resolve root module for package: {package_name}")


def generate_docs_data(
    package_path: pathlib.Path = PACKAGE_PATH,
    output_path: pathlib.Path = OUTPUT_PATH,
) -> None:
    """Generates documentation data for a package and writes it as JSON.

    File paths in the output are relative to the current working directory,
    so the package must lie beneath it.

    Args:
        package_path: Directory of the package to document.
        output_path: Destination of the JSON file; parents are created.
    """
    logger.info("Starting documentation generation for package: %s", package_path)
    root_module = load_root_module(package_path)

    logger.info("Collecting modules from root: %s", root_module.canonical_path)
    processed_paths: set[str] = set()
    modules = collect_modules_recursively(
        root_module, package_path.name, processed_paths, package_path
    )

    output_data = {"modules": sorted(modules, key=lambda x: x["path"])}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(output_data, file, indent=2, ensure_ascii=False)

    logger.info("Documentation data successfully written to: %s", output_path)


def main() -> None:
    """Configures logging and generates the default documentation data file."""
    get_logger().setLevel(logging.WARNING)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s"
    )
    generate_docs_data()
