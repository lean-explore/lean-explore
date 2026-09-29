"""Builders for synthetic doc-gen4 output used by the extraction tests.

Provides encoders for leansqlite RenderedCode BLOBs, a builder for small
``api-docs.db`` databases, and helpers for Lean source and BMP files.
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

MATHLIB_URL = "https://github.com/leanprover-community/mathlib4/blob/master/"


# ---------------------------------------------------------------------------
# RenderedCode BLOB encoders
# ---------------------------------------------------------------------------


def encode_nat(n: int) -> bytes:
    """Encode a natural number in leansqlite's variable-length format."""
    chunks = []
    while n >= 128:
        chunks.append((n & 0x7F) | 0x80)
        n >>= 7
    chunks.append(n)
    return bytes(chunks)


def encode_string(s: str) -> bytes:
    """Encode a string: Nat(utf8_byte_length) + UTF-8 bytes."""
    encoded = s.encode("utf-8")
    return encode_nat(len(encoded)) + encoded


def encode_name(name: str) -> bytes:
    """Encode a dotted Lean name (e.g. 'Nat.add') into binary."""
    result = b"\x00"  # anonymous
    if not name:
        return result
    for part in name.split("."):
        # Name.str = tag 1 + parent + string
        result = b"\x01" + result + encode_string(part)
    return result


def const_tag(name: str) -> bytes:
    """Build a RenderedCode.Tag.const(name) blob fragment."""
    return b"\x02" + encode_name(name)


def text_node(s: str) -> bytes:
    """Build a TaggedText.text(s) blob fragment."""
    return b"\x00" + encode_string(s)


def tag_node(tag_bytes: bytes, inner: bytes) -> bytes:
    """Build a TaggedText.tag(tag, inner) blob fragment."""
    return b"\x01" + tag_bytes + inner


def append_node(children: list[bytes]) -> bytes:
    """Build a TaggedText.append(children) blob fragment."""
    return b"\x02" + encode_nat(len(children)) + b"".join(children)


def const_node(name: str) -> bytes:
    """Build a tagged reference to ``name`` rendered as its own text."""
    return tag_node(const_tag(name), text_node(name))


# ---------------------------------------------------------------------------
# api-docs.db builder
# ---------------------------------------------------------------------------

DOCGEN_SCHEMA = """
CREATE TABLE modules (name TEXT PRIMARY KEY, source_url TEXT);
CREATE TABLE name_info (
  module_name TEXT NOT NULL,
  position INTEGER NOT NULL,
  kind TEXT,
  name TEXT NOT NULL,
  type BLOB NOT NULL,
  sorried INTEGER NOT NULL,
  render INTEGER NOT NULL,
  PRIMARY KEY (module_name, position)
);
CREATE TABLE declaration_ranges (
  module_name TEXT NOT NULL,
  position INTEGER NOT NULL,
  start_line INTEGER NOT NULL,
  start_column INTEGER NOT NULL,
  start_utf16 INTEGER NOT NULL,
  end_line INTEGER NOT NULL,
  end_column INTEGER NOT NULL,
  end_utf16 INTEGER NOT NULL,
  PRIMARY KEY (module_name, position)
);
CREATE TABLE declaration_markdown_docstrings (
  module_name TEXT NOT NULL,
  position INTEGER NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY (module_name, position)
);
CREATE TABLE declaration_verso_docstrings (
  module_name TEXT NOT NULL,
  position INTEGER NOT NULL,
  content BLOB NOT NULL,
  PRIMARY KEY (module_name, position)
);
"""


@dataclass
class DocgenRow:
    """One declaration to store in a synthetic api-docs.db."""

    module: str
    name: str
    start_line: int = 1
    end_line: int = 1
    type_blob: bytes = b""
    render: int = 1
    docstring: str | None = None
    verso_docstring: bytes | None = None


def create_docgen_db(
    database_path: Path, modules: dict[str, str | None], rows: list[DocgenRow]
) -> Path:
    """Create a doc-gen4 api-docs.db containing the given modules and rows.

    Args:
        database_path: Where to write the database; parents are created.
        modules: Module name to stored ``source_url`` (None for core modules).
        rows: Declarations to store; positions are assigned in list order.

    Returns:
        ``database_path``.
    """
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.executescript(DOCGEN_SCHEMA)
    connection.executemany("INSERT INTO modules VALUES (?, ?)", modules.items())
    for position, row in enumerate(rows, start=1):
        key = (row.module, position)
        connection.execute(
            "INSERT INTO name_info VALUES (?, ?, 'def', ?, ?, 0, ?)",
            (*key, row.name, row.type_blob, row.render),
        )
        connection.execute(
            "INSERT INTO declaration_ranges VALUES (?, ?, ?, 0, 0, ?, 0, 0)",
            (*key, row.start_line, row.end_line),
        )
        if row.docstring is not None:
            connection.execute(
                "INSERT INTO declaration_markdown_docstrings VALUES (?, ?, ?)",
                (*key, row.docstring),
            )
        if row.verso_docstring is not None:
            connection.execute(
                "INSERT INTO declaration_verso_docstrings VALUES (?, ?, ?)",
                (*key, row.verso_docstring),
            )
    connection.commit()
    connection.close()
    return database_path


def create_minimal_docgen_db(database_path: Path, tables: list[str]) -> Path:
    """Create a SQLite file containing only the named (column-less) tables."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    for table in tables:
        connection.execute(f"CREATE TABLE {table} (module_name TEXT)")
    connection.close()
    return database_path


# ---------------------------------------------------------------------------
# Workspace files
# ---------------------------------------------------------------------------


def write_file(path: Path, text: str) -> Path:
    """Write ``text`` to ``path``, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def mathlib_package_dir(lean_root: Path) -> Path:
    """Return the mathlib4 checkout inside the mathlib workspace."""
    return lean_root / "mathlib" / ".lake" / "packages" / "mathlib4"


def write_bmp(path: Path, module_name: str, declarations: list[dict]) -> Path:
    """Write a legacy doc-gen4 BMP JSON file for one module.

    Args:
        path: Destination ``.bmp`` path; parents are created.
        module_name: Lean module name stored in the file.
        declarations: Entries with ``info`` (name, doc, sourceLink) and an
            optional ``header`` HTML string.

    Returns:
        ``path``.
    """
    return write_file(
        path, json.dumps({"name": module_name, "declarations": declarations})
    )


def bmp_entry(
    name: str, source_link: str, doc: str | None = None, header: str | None = None
) -> dict:
    """Build one BMP declaration entry."""
    info = {"name": name, "sourceLink": source_link}
    if doc is not None:
        info["doc"] = doc
    entry: dict = {"info": info}
    if header is not None:
        entry["header"] = header
    return entry
