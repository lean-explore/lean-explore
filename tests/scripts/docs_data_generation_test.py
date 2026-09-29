"""End-to-end tests for documentation data generation on a fixture package."""

import json
import pathlib

from scripts.docs_data.traversal import (
    collect_modules_recursively,
    generate_docs_data,
    is_target_package_module,
)

FIXTURE_FILES = {
    "__init__.py": '"""Fixture package."""\n\nfrom pkg.core import add\n',
    "core.py": '''
        """Core helpers."""


        def add(a: int, b: int) -> int:
            """Adds two numbers.

            Args:
                a: First addend.
                b: Second addend.

            Returns:
                The sum.
            """
            return a + b
        ''',
    "sub/__init__.py": '"""Subpackage."""\n',
    "sub/leaf.py": '"""Leaf module."""\n\nVALUE = 1\n',
}


def test_generate_docs_data_writes_sorted_modules(load_package):
    """Test generation writes every package module, sorted by path."""
    load_package(FIXTURE_FILES)
    package_path = pathlib.Path("src/pkg")
    output_path = pathlib.Path("out/module_data.json")

    generate_docs_data(package_path, output_path)

    raw = output_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert raw == json.dumps(data, indent=2, ensure_ascii=False)
    assert [module["path"] for module in data["modules"]] == [
        "pkg",
        "pkg.core",
        "pkg.sub",
        "pkg.sub.leaf",
    ]

    modules = {module["path"]: module for module in data["modules"]}
    # Re-exported functions are documented only where they are defined.
    assert modules["pkg"]["functions"] == []
    add = modules["pkg.core"]["functions"][0]
    assert add["name"] == "add"
    assert add["filepath"] == "src/pkg/core.py"
    assert add["returns"] == {"annotation": "int", "description": "The sum."}
    assert [parameter["description"] for parameter in add["parameters"]] == [
        "First addend.",
        "Second addend.",
    ]
    assert modules["pkg.sub.leaf"]["docstring"] == "Leaf module."


def test_collect_modules_skips_processed_modules(load_package):
    """Test already processed modules are not serialized twice."""
    root = load_package(FIXTURE_FILES)
    package_path = pathlib.Path("src/pkg")
    processed: set[str] = set()

    first = collect_modules_recursively(root, "pkg", processed, package_path)
    second = collect_modules_recursively(root, "pkg", processed, package_path)

    assert len(first) == 4
    assert second == []


def test_is_target_package_module_uses_loose_prefix(load_package):
    """Test membership falls back to a plain name-prefix match."""
    root = load_package(FIXTURE_FILES)
    elsewhere = pathlib.Path("src/other")

    assert is_target_package_module(root["core"], "pkg", pathlib.Path("src/pkg"))
    assert is_target_package_module(root["core"], "pk", elsewhere)
    assert not is_target_package_module(root["core"], "other", elsewhere)
