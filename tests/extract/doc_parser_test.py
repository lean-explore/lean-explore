"""Tests for the doc_parser extraction orchestration.

Covers doc-gen4 output format detection and the end-to-end
``extract_declarations`` pipeline over synthetic package workspaces.
"""

import json
import logging

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lean_explore.extract.doc_parser import _detect_docgen_format, extract_declarations
from lean_explore.models import Declaration as DBDeclaration
from tests.extract.docgen_fixtures import (
    MATHLIB_URL,
    DocgenRow,
    bmp_entry,
    const_node,
    create_docgen_db,
    create_minimal_docgen_db,
    mathlib_package_dir,
    write_bmp,
    write_file,
)

REQUIRED_TABLES = ["modules", "name_info", "declaration_ranges"]
FLT_URL = "https://github.com/ImperialCollegeLondon/FLT/blob/main/"


@pytest.fixture
def build_dir(temp_directory):
    """The ``.lake/build`` directory of a workspace named ``pkg``."""
    return temp_directory / "pkg" / ".lake" / "build"


def _write_placeholder_bmp(build_dir):
    write_file(build_dir / "doc-data" / "nested" / "Foo.bmp", "{}")


class TestDetectDocgenFormat:
    """Tests for doc-gen4 output format detection."""

    def test_valid_sqlite(self, build_dir):
        """Test detection returns 'sqlite' for a valid api-docs.db."""
        create_minimal_docgen_db(build_dir / "api-docs.db", REQUIRED_TABLES)
        _write_placeholder_bmp(build_dir)

        assert _detect_docgen_format(build_dir.parent.parent) == "sqlite"

    def test_bmp(self, build_dir):
        """Test detection returns 'bmp' when only BMP files exist."""
        _write_placeholder_bmp(build_dir)

        assert _detect_docgen_format(build_dir.parent.parent) == "bmp"

    def test_none_when_empty(self, build_dir):
        """Test detection returns 'none' without output or with an empty doc-data."""
        (build_dir / "doc-data").mkdir(parents=True)

        assert _detect_docgen_format(build_dir.parent.parent) == "none"

    @pytest.mark.parametrize(
        "database_bytes", [b"", b"this is not a sqlite database", None]
    )
    @pytest.mark.parametrize(("has_bmp", "expected"), [(True, "bmp"), (False, "none")])
    def test_invalid_sqlite_falls_back(
        self, build_dir, caplog, database_bytes, has_bmp, expected
    ):
        """Test empty, corrupt, or incomplete databases fall back to BMP."""
        database = build_dir / "api-docs.db"
        if database_bytes is None:
            create_minimal_docgen_db(database, ["modules"])
        else:
            write_file(database, database_bytes.decode())
        if has_bmp:
            _write_placeholder_bmp(build_dir)

        assert _detect_docgen_format(build_dir.parent.parent) == expected
        assert "checking for BMP fallback" in caplog.text


def _build_mathlib_sqlite_workspace(lean_root):
    """Create a mathlib workspace with SQLite output, including a projection."""
    write_file(
        mathlib_package_dir(lean_root) / "Mathlib/Foo.lean",
        "structure Foo extends Bar where\n  x : Nat\n@[simp]\ntheorem foo : True\n",
    )
    url = MATHLIB_URL + "Mathlib/Foo.lean"
    create_docgen_db(
        lean_root / "mathlib" / ".lake" / "build" / "api-docs.db",
        {"Mathlib.Foo": url, "Aesop.Y": url},
        [
            DocgenRow("Mathlib.Foo", "Foo", 1, 2, const_node("Bar"), docstring="Doc"),
            DocgenRow("Mathlib.Foo", "Foo.toBar", 1, 2),
            DocgenRow("Mathlib.Foo", "Foo.mk", 1, 2),
            DocgenRow("Mathlib.Foo", "foo", 3, 3),
            DocgenRow("Aesop.Y", "Aesop.y", 1, 1),
        ],
    )


def _build_flt_bmp_workspace(lean_root):
    """Create an flt workspace with legacy BMP output."""
    write_file(
        lean_root / "flt" / ".lake" / "packages" / "flt" / "FLT" / "B.lean",
        "def FLT.b := 1\n",
    )
    header = '<a href="#Nat">Nat</a><a href="#Foo">Foo</a>'
    write_bmp(
        lean_root / "flt" / ".lake" / "build" / "doc-data" / "FLT.B.bmp",
        "FLT.B",
        [
            bmp_entry("FLT.b", FLT_URL + "FLT/B.lean#L1-L1", "b doc", header),
            # Same name as a mathlib declaration: skipped on insert.
            bmp_entry("Foo", FLT_URL + "FLT/B.lean#L1-L1"),
        ],
    )


async def _stored_rows(engine):
    async with AsyncSession(engine) as session:
        result = await session.execute(select(DBDeclaration).order_by(DBDeclaration.id))
        return [
            (row.name, row.module, row.docstring, row.source_text, row.dependencies)
            for row in result.scalars()
        ]


class TestExtractDeclarations:
    """End-to-end tests for declaration extraction."""

    async def test_extracts_sqlite_and_bmp_workspaces(
        self, async_db_engine, temp_directory, monkeypatch, caplog
    ):
        """Test the full pipeline across SQLite and BMP workspaces."""
        lean_root = temp_directory / "lean"
        _build_mathlib_sqlite_workspace(lean_root)
        _build_flt_bmp_workspace(lean_root)
        monkeypatch.chdir(temp_directory)

        with caplog.at_level(logging.INFO, logger="lean_explore.extract"):
            await extract_declarations(async_db_engine, batch_size=2)

        assert await _stored_rows(async_db_engine) == [
            (
                "Foo",
                "Mathlib.Foo",
                "Doc",
                "structure Foo extends Bar where\n  x : Nat\n",
                json.dumps(["Bar"]),
            ),
            ("foo", "Mathlib.Foo", None, "@[simp]\ntheorem foo : True", None),
            ("FLT.b", "FLT.B", "b doc", "def FLT.b := 1\n", json.dumps(["Nat", "Foo"])),
        ]
        for message in [
            "[mathlib] Using SQLite format (api-docs.db)",
            "[flt] Using BMP format (1 files)",
            "No doc-gen4 output found for physlean",
            "Total declarations extracted: 5",
            "Filtered 1 auto-generated 'to*' projections",
            "Inserted 3 new declarations into database (skipped 1 duplicates)",
        ]:
            assert message in caplog.text

    async def test_raises_when_nothing_extracted(
        self, async_db_engine, temp_directory, monkeypatch
    ):
        """Test that a lean root without doc-gen4 output is an error."""
        (temp_directory / "lean").mkdir()
        monkeypatch.chdir(temp_directory)

        with pytest.raises(FileNotFoundError, match="No declarations extracted"):
            await extract_declarations(async_db_engine)

        assert await _stored_rows(async_db_engine) == []
