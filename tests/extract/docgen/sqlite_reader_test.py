"""Tests for reading declarations from a doc-gen4 api-docs.db."""

import logging

import pytest

from lean_explore.extract.docgen.lean_source import build_package_cache
from lean_explore.extract.docgen.sqlite_reader import (
    parse_declarations_from_sqlite,
    validate_docgen_sqlite,
)
from lean_explore.extract.packages.config import PackageConfig
from tests.extract.docgen.fixtures import (
    MATHLIB_URL,
    DocgenRow,
    append_node,
    const_node,
    create_docgen_db,
    create_minimal_docgen_db,
    mathlib_package_dir,
    text_node,
    write_file,
)

MODULE = "Mathlib.Data.Nat.Basic"
MODULE_URL = MATHLIB_URL + "Mathlib/Data/Nat/Basic.lean"


def _include(*prefixes: str):
    """Build a module predicate for the given prefixes."""
    return PackageConfig("test", "", list(prefixes)).should_include_module


@pytest.fixture
def lean_root(temp_directory):
    """A lean root whose mathlib workspace holds ``Mathlib/Data/Nat/Basic``."""
    root = temp_directory / "lean"
    write_file(
        mathlib_package_dir(root) / "Mathlib/Data/Nat/Basic.lean",
        "theorem Nat.visible : True := trivial\n"
        "def Nat.hidden : Nat := 0\n"
        "def Nat.myFunc (n : Nat) : Bool := true\n",
    )
    return root


@pytest.fixture
def parse(lean_root, temp_directory):
    """Build an api-docs.db from rows and parse it with Mathlib prefixes."""

    def run(rows, modules=None, prefixes=("Mathlib",), **kwargs):
        database = create_docgen_db(
            temp_directory / "api-docs.db", modules or {MODULE: MODULE_URL}, rows
        )
        return parse_declarations_from_sqlite(
            database,
            lean_root,
            kwargs.pop("package_cache", None) or build_package_cache(lean_root),
            _include(*prefixes),
            **kwargs,
        )

    return run


class TestParseDeclarationsFromSqlite:
    """Tests for converting api-docs.db rows to declarations."""

    def test_builds_declaration(self, parse):
        """Test all fields of a rendered declaration are populated."""
        rows = [DocgenRow(MODULE, "Nat.visible", 1, 1, docstring="Visible")]

        [declaration] = parse(rows)

        assert declaration.name == "Nat.visible"
        assert declaration.module == MODULE
        assert declaration.docstring == "Visible"
        assert declaration.source_text == "theorem Nat.visible : True := trivial\n"
        assert declaration.source_link == f"{MODULE_URL}#L1-L1"
        assert declaration.dependencies is None

    def test_filters_non_rendered(self, parse):
        """Test SQLite parsing only keeps rendered declarations."""
        rows = [
            DocgenRow(MODULE, "Nat.visible", 1, 1),
            DocgenRow(MODULE, "Nat.hidden", 2, 2, render=0),
        ]

        assert [d.name for d in parse(rows)] == ["Nat.visible"]

    def test_extracts_dependencies(self, parse):
        """Test that dependencies come from the type BLOB, minus self-references."""
        type_blob = append_node(
            [
                const_node("Nat"),
                text_node(" → "),
                const_node("Nat.myFunc"),
                const_node("Bool"),
            ]
        )
        rows = [DocgenRow(MODULE, "Nat.myFunc", 3, 3, type_blob=type_blob)]

        assert parse(rows)[0].dependencies == ["Nat", "Bool"]

    def test_self_reference_only_gives_no_dependencies(self, parse):
        """Test a type referencing only the declaration itself yields None."""
        rows = [DocgenRow(MODULE, "Nat.myFunc", 3, 3, const_node("Nat.myFunc"))]

        assert parse(rows)[0].dependencies is None

    def test_detects_verso_docstrings(self, parse, caplog):
        """Test that Verso-only docstrings are stored as None and logged."""
        rows = [
            DocgenRow(MODULE, "Nat.withVerso", 1, 1, verso_docstring=b"\x00\x01"),
            DocgenRow(MODULE, "Nat.withMarkdown", 2, 2, docstring="Markdown"),
        ]

        with caplog.at_level(logging.WARNING):
            declarations = parse(rows)

        assert [(d.name, d.docstring) for d in declarations] == [
            ("Nat.withVerso", None),
            ("Nat.withMarkdown", "Markdown"),
        ]
        assert "1 declarations have Verso-only docstrings" in caplog.text

    def test_skips_and_reports_filtered_rows(self, parse, caplog):
        """Test prefix, constructor, missing-link, and unreadable rows are skipped."""
        modules = {MODULE: MODULE_URL, "Aesop.Rule": None, "Mathlib.Gone": None}
        rows = [
            DocgenRow("Aesop.Rule", "Aesop.rule"),  # outside prefixes
            DocgenRow(MODULE, "Nat.Foo.mk"),  # constructor
            DocgenRow("Mathlib.Gone", "Gone.x"),  # no source URL
            DocgenRow(MODULE, "Nat.oob", 1, 99),  # unreadable range
            DocgenRow(MODULE, "Nat.visible"),
        ]

        with caplog.at_level(logging.INFO):
            declarations = parse(rows, modules=modules)

        assert [d.name for d in declarations] == ["Nat.visible"]
        for message in [
            "Found 5 declarations in api-docs.db",
            "Skipped 1 declarations outside allowed prefixes",
            "Skipped 1 .mk constructors",
            "Skipped 1 declarations without source URL",
            "Could not extract source text for 1 declarations",
        ]:
            assert message in caplog.text

    def test_prefix_match_requires_module_boundary(self, parse):
        """Test that prefix "Mathlib" does not match module "MathlibExtras"."""
        modules = {"MathlibExtras": MODULE_URL, MODULE: MODULE_URL}
        rows = [
            DocgenRow("MathlibExtras", "Extra.x"),
            DocgenRow(MODULE, "Nat.visible"),
        ]

        assert [d.name for d in parse(rows, modules=modules)] == ["Nat.visible"]

    def test_uses_core_fallback_source_link(self, parse, temp_directory):
        """Test SQLite parsing for core modules without a stored source URL."""
        lean_source = temp_directory / "toolchain" / "src" / "lean"
        write_file(lean_source / "Init/Data/Nat/Basic.lean", "theorem Nat.core\n")
        rows = [DocgenRow("Init.Data.Nat.Basic", "Nat.core")]

        [declaration] = parse(
            rows,
            modules={"Init.Data.Nat.Basic": None},
            prefixes=("Init",),
            package_cache={"lean4": lean_source},
            lean_version="v4.29.0-rc6",
        )

        assert declaration.source_text == "theorem Nat.core\n"
        assert declaration.source_link == (
            "https://github.com/leanprover/lean4/blob/v4.29.0-rc6/"
            "src/lean/Init/Data/Nat/Basic.lean#L1-L1"
        )

    def test_orders_by_module_then_position(self, parse, lean_root):
        """Test declarations are returned sorted by module and position."""
        write_file(mathlib_package_dir(lean_root) / "Mathlib/A.lean", "def a\n")
        modules = {MODULE: MODULE_URL, "Mathlib.A": MATHLIB_URL + "Mathlib/A.lean"}
        rows = [
            DocgenRow(MODULE, "Nat.visible", 1, 1),
            DocgenRow(MODULE, "Nat.hidden", 2, 2),
            DocgenRow("Mathlib.A", "a"),
        ]

        names = [d.name for d in parse(rows, modules=modules)]

        assert names == ["a", "Nat.visible", "Nat.hidden"]


class TestValidateDocgenSqlite:
    """Tests for api-docs.db validation."""

    def test_valid(self, temp_directory):
        """Test validation passes for a well-formed database."""
        database = create_minimal_docgen_db(
            temp_directory / "api-docs.db",
            ["modules", "name_info", "declaration_ranges"],
        )

        assert validate_docgen_sqlite(database) is True

    def test_empty_file(self, temp_directory, caplog):
        """Test validation rejects an empty file."""
        database = write_file(temp_directory / "api-docs.db", "")

        assert validate_docgen_sqlite(database) is False
        assert "exists but is empty" in caplog.text

    def test_corrupt_file(self, temp_directory, caplog):
        """Test validation rejects a non-SQLite file."""
        database = write_file(temp_directory / "api-docs.db", "not a database")

        assert validate_docgen_sqlite(database) is False
        assert "not a valid SQLite file" in caplog.text

    def test_missing_tables(self, temp_directory, caplog):
        """Test validation rejects a database without the required tables."""
        database = create_minimal_docgen_db(temp_directory / "api-docs.db", ["modules"])

        assert validate_docgen_sqlite(database) is False
        assert "missing required tables" in caplog.text
