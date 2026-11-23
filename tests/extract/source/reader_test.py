"""Tests for turning unbuilt Lean sources into declaration rows."""

import dataclasses

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lean_explore.extract.docgen import parser as docgen_parser
from lean_explore.extract.packages.config import ExtractionMethod, PackageConfig
from lean_explore.extract.source import reader
from lean_explore.extract.source.fetch import read_commit
from lean_explore.extract.source.reader import (
    extract_source_package,
    is_stored,
    lean_docstring,
    load_parser,
    package_files,
    parse_package_sources,
    source_link_base,
)
from lean_explore.models import Declaration as DBDeclaration
from tests.extract.source.fixtures import make_repository

BASE = "https://github.com/owner/pkg/blob/abc123/"

PKG_A = """/-! Module documentation. -/
namespace Pkg

/-- Adds one. -/
def addOne (n : Nat) : Nat := n + 1

private theorem hidden : True := trivial

/--
A point.
  Its fields are skipped.
-/
structure Point where
  x : Nat
  y : Nat

instance : Inhabited Point := ⟨⟨0, 0⟩⟩

instance instReprPoint : Repr Point := ⟨fun _ _ => "p"⟩

@[simp]
theorem addOne_zero : addOne 0 = 1 := rfl

example : True := trivial

end Pkg
"""


@pytest.fixture
def requires_parser():
    """Skip a test when the parser's Python bindings are not installed."""
    pytest.importorskip("lean_source_parser")


@pytest.fixture
def package(tmp_path):
    """A package checkout with one module inside and one outside its prefix."""
    root = tmp_path / "pkg"
    (root / "Pkg").mkdir(parents=True)
    (root / "Pkg" / "A.lean").write_text(PKG_A, encoding="utf-8")
    (root / "Other").mkdir()
    (root / "Other" / "B.lean").write_text("def other := 1\n", encoding="utf-8")
    (root / ".lake" / "Pkg").mkdir(parents=True)
    (root / ".lake" / "Pkg" / "C.lean").write_text("def c := 1\n", encoding="utf-8")
    return root


def _include_pkg(module: str) -> bool:
    return module == "Pkg" or module.startswith("Pkg.")


class TestLeanDocstring:
    """The stored docstring follows Lean's de-indentation."""

    def test_single_line_keeps_trailing_space(self):
        """Only leading whitespace is removed."""
        assert lean_docstring("/-- Adds one. -/") == "Adds one. "

    def test_multi_line_is_deindented(self):
        """The smallest indentation after the first line is removed."""
        doc = "/--\n  First line.\n    Indented.\n  Last. -/"
        assert lean_docstring(doc) == "First line.\n  Indented.\nLast. "

    def test_line_after_blank_line_is_not_counted(self):
        """Lean skips the line after a blank line when measuring indentation."""
        doc = "/-- Top.\n\nzero\n    four\n  two -/"
        assert lean_docstring(doc) == "Top.\n\nzero\n  four\ntwo "

    def test_tabs_count_as_indentation(self):
        """Tabs and spaces both count."""
        assert lean_docstring("/-- a\n\tb\n\tc -/") == "a\nb\nc "


def test_source_link_base_normalises_url():
    """A trailing slash or .git suffix does not change the link."""
    assert source_link_base("https://github.com/o/p.git", "c0") == (
        "https://github.com/o/p/blob/c0/"
    )
    assert source_link_base("https://github.com/o/p/", "c0") == (
        "https://github.com/o/p/blob/c0/"
    )


def test_package_files_selects_package_modules(package):
    """Files outside the prefixes and inside .lake are not parsed."""
    assert package_files(package, _include_pkg) == [package / "Pkg" / "A.lean"]


@pytest.mark.parametrize(
    ("changes", "stored"),
    [
        ({}, True),
        ({"name": None}, False),
        ({"name": "Pkg.Point.mk"}, False),
        ({"kind": "field"}, False),
        ({"kind": "where_decl"}, False),
        ({"modifiers": ["private"]}, False),
        ({"approximate": True}, True),
    ],
)
def test_is_stored(changes, stored):
    """Unnamed, private, auxiliary and generated constructors are dropped."""
    declaration = {
        "name": "Pkg.foo",
        "kind": "theorem",
        "modifiers": [],
        "approximate": False,
    } | changes
    assert is_stored(declaration, include_approximate=True) is stored


def test_approximate_names_can_be_excluded():
    """Rule-derived names are dropped on request."""
    declaration = {"name": "Pkg.add", "kind": "theorem", "modifiers": []}
    assert not is_stored(declaration | {"approximate": True}, False)


def test_missing_parser_has_install_hint(monkeypatch):
    """A missing extension module raises with the install command."""

    def fail(name):
        raise ImportError(name)

    monkeypatch.setattr(reader.importlib, "import_module", fail)
    with pytest.raises(RuntimeError, match="pip install ./src/lean_explore/parser"):
        load_parser()


@pytest.mark.usefixtures("requires_parser")
class TestParsePackageSources:
    """Rows built from a small fixture package."""

    def test_rows(self, package):
        """Stored rows match what doc-gen4 extraction writes."""
        rows = {d.name: d for d in parse_package_sources(package, BASE, _include_pkg)}

        assert list(rows) == [
            "Pkg.addOne",
            "Pkg.Point",
            "Pkg.instReprPoint",
            "Pkg.addOne_zero",
        ]
        add_one = rows["Pkg.addOne"]
        assert add_one.module == "Pkg.A"
        assert add_one.docstring == "Adds one. "
        assert add_one.source_text == (
            "/-- Adds one. -/\ndef addOne (n : Nat) : Nat := n + 1\n"
        )
        assert add_one.source_link == BASE + "Pkg/A.lean#L4-L5"
        assert add_one.dependencies is None
        assert rows["Pkg.Point"].docstring == "A point.\n  Its fields are skipped.\n"
        assert rows["Pkg.addOne_zero"].source_text == (
            "@[simp]\ntheorem addOne_zero : addOne 0 = 1 := rfl"
        )


@pytest.mark.usefixtures("requires_parser")
class TestSourceExtraction:
    """Source packages through the extraction entry points."""

    def test_unfetched_package_is_empty(self, tmp_path, caplog):
        """A package without a clone yields nothing and a warning."""
        config = PackageConfig(name="pkg", git_url="u", module_prefixes=["Pkg"])
        assert extract_source_package(tmp_path, config) == []
        assert "No sources found for pkg" in caplog.text

    async def test_extract_declarations(
        self, async_db_engine, temp_directory, monkeypatch
    ):
        """A source package is parsed and stored without dependencies."""
        source = make_repository(
            temp_directory / "lean" / "pkg" / "source", {"Pkg/A.lean": PKG_A}
        )
        config = PackageConfig(
            name="pkg",
            git_url="https://github.com/owner/pkg",
            module_prefixes=["Pkg"],
            extraction=ExtractionMethod.SOURCE,
        )
        monkeypatch.setattr(
            "lean_explore.extract.packages.registry.PACKAGE_REGISTRY",
            {"pkg": dataclasses.replace(config)},
        )
        monkeypatch.setattr(
            "lean_explore.extract.packages.workspace.get_extraction_order",
            lambda: ["pkg"],
        )
        monkeypatch.chdir(temp_directory)

        await docgen_parser.extract_declarations(async_db_engine)

        async with AsyncSession(async_db_engine) as session:
            stored = (await session.execute(select(DBDeclaration))).scalars().all()
        commit = read_commit(source)
        assert sorted(row.name for row in stored) == [
            "Pkg.Point",
            "Pkg.addOne",
            "Pkg.addOne_zero",
            "Pkg.instReprPoint",
        ]
        assert all(row.dependencies is None for row in stored)
        assert {row.source_link.split("#")[0] for row in stored} == {
            f"https://github.com/owner/pkg/blob/{commit}/Pkg/A.lean"
        }
