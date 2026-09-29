"""Tests for reading declarations from legacy doc-gen4 BMP files."""

import logging

import pytest

from lean_explore.extract.docgen.bmp_reader import (
    extract_dependencies_from_html,
    parse_declarations_from_files,
)
from lean_explore.extract.docgen.lean_source import build_package_cache
from lean_explore.extract.packages.config import PackageConfig
from tests.extract.docgen.fixtures import (
    MATHLIB_URL,
    bmp_entry,
    mathlib_package_dir,
    write_bmp,
    write_file,
)

NAT_LINK = MATHLIB_URL + "Mathlib/Init/Data/Nat.lean#L1-L1"


@pytest.fixture
def lean_root(temp_directory):
    """A lean root whose mathlib workspace holds ``Mathlib/Init/Data/Nat``."""
    root = temp_directory / "lean"
    write_file(
        mathlib_package_dir(root) / "Mathlib/Init/Data/Nat.lean",
        "def Nat.add (n m : Nat) : Nat := n + m\n",
    )
    return root


@pytest.fixture
def parse(lean_root):
    """Write BMP files as ``{module: entries}`` and parse them for Mathlib."""
    doc_data = lean_root / "mathlib" / ".lake" / "build" / "doc-data"

    def run(modules: dict[str, list[dict]]):
        files = [
            write_bmp(doc_data / f"{module}.bmp", module, entries)
            for module, entries in modules.items()
        ]
        include = PackageConfig("mathlib", "", ["Mathlib"]).should_include_module
        return parse_declarations_from_files(
            files, lean_root, build_package_cache(lean_root), include
        )

    return run


class TestExtractDependenciesFromHtml:
    """Tests for dependency extraction from HTML."""

    def test_extracts_linked_names(self):
        """Test extracting declaration dependencies from HTML header."""
        html = """
        <div class="header">
            <a href="#Nat">Nat</a> →
            <a href="./Init/Core.html#Nat.add">Nat.add</a> →
            <a href="#List">List</a>
        </div>
        """

        assert extract_dependencies_from_html(html) == ["Nat", "Nat.add", "List"]

    def test_deduplicates(self):
        """Test that duplicate dependencies are removed, keeping first order."""
        html = '<a href="#Nat">N</a><a href="#List">L</a><a href="#Nat">N</a>'

        assert extract_dependencies_from_html(html) == ["Nat", "List"]

    def test_no_links(self):
        """Test extracting from HTML with no dependencies."""
        assert extract_dependencies_from_html("<div>No links here</div>") == []


class TestParseDeclarationsFromFiles:
    """Tests for BMP file parsing."""

    def test_builds_declaration(self, parse):
        """Test parsing declarations from BMP files."""
        entry = bmp_entry(
            "Nat.add", NAT_LINK, doc="Addition", header='<a href="#Nat">Nat</a>'
        )

        [declaration] = parse({"Mathlib.Init.Data.Nat": [entry]})

        assert declaration.name == "Nat.add"
        assert declaration.module == "Mathlib.Init.Data.Nat"
        assert declaration.docstring == "Addition"
        assert declaration.source_text == "def Nat.add (n m : Nat) : Nat := n + m\n"
        assert declaration.source_link == NAT_LINK
        assert declaration.dependencies == ["Nat"]

    def test_missing_header_and_doc(self, parse):
        """Test entries without header or doc give no dependencies or docstring."""
        [declaration] = parse({"Mathlib.Init.Data.Nat": [bmp_entry("x", NAT_LINK)]})

        assert declaration.docstring is None
        assert declaration.dependencies is None

    def test_drops_self_references(self, parse):
        """Test a declaration's own name is not listed as a dependency."""
        header = '<a href="#Nat.add">Nat.add</a><a href="#Nat">Nat</a>'
        entry = bmp_entry("Nat.add", NAT_LINK, header=header)

        [declaration] = parse({"Mathlib.Init.Data.Nat": [entry]})

        assert declaration.dependencies == ["Nat"]

    @pytest.mark.parametrize("module", ["SomeOtherPackage.Basic", "MathlibExtras"])
    def test_filters_modules_outside_prefixes(self, parse, module):
        """Test that modules not under the allowed prefixes are skipped."""
        assert parse({module: [bmp_entry("Nat.add", NAT_LINK)]}) == []

    def test_skips_constructors_and_unreadable_sources(self, parse, caplog):
        """Test .mk constructors and unreadable sources are skipped and counted."""
        entries = [
            bmp_entry("Foo.mk", NAT_LINK),
            bmp_entry("Bad.link", "not a link"),
            bmp_entry("Missing", MATHLIB_URL + "Mathlib/Missing.lean#L1-L1"),
            bmp_entry("Nat.add", NAT_LINK),
        ]

        with caplog.at_level(logging.WARNING):
            declarations = parse({"Mathlib.Init.Data.Nat": entries})

        assert [d.name for d in declarations] == ["Nat.add"]
        assert "Could not extract source text for 2 declarations" in caplog.text

    def test_combines_files_in_order(self, parse):
        """Test declarations from several files are concatenated in file order."""
        declarations = parse(
            {
                "Mathlib.B": [bmp_entry("b", NAT_LINK)],
                "Mathlib.A": [bmp_entry("a1", NAT_LINK), bmp_entry("a2", NAT_LINK)],
                "Mathlib.Empty": [],
            }
        )

        assert [(d.module, d.name) for d in declarations] == [
            ("Mathlib.B", "b"),
            ("Mathlib.A", "a1"),
            ("Mathlib.A", "a2"),
        ]
