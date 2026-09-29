"""Tests for Google-style docstring parsing in the docs data generator."""

import logging
from types import SimpleNamespace

import pytest
from griffe import Docstring, get_logger

from scripts.docs_data.docstrings import (
    combine_text,
    parse_admonition_section,
    parse_docstring,
    parse_examples_section,
    parse_generic_section,
    resolve_annotation,
)


@pytest.fixture(autouse=True)
def quiet_griffe():
    """Silences griffe warnings about docstrings parsed without a signature."""
    griffe_logger = get_logger()
    previous_level = griffe_logger.level
    griffe_logger.setLevel(logging.ERROR)
    yield
    griffe_logger.setLevel(previous_level)


def parse(text: str) -> dict:
    """Parses a raw Google-style docstring into section data."""
    return parse_docstring(Docstring(text, parser="google"))


class TestResolveAnnotation:
    """Tests for resolve_annotation."""

    def test_string_is_returned_unchanged(self):
        """Test string annotations pass through."""
        assert resolve_annotation("int") == "int"

    def test_none_becomes_empty_string(self):
        """Test missing annotations become an empty string."""
        assert resolve_annotation(None) == ""

    def test_unsupported_type_becomes_empty_string(self):
        """Test non-string, non-expression annotations are ignored."""
        assert resolve_annotation(42) == ""


class TestParseDocstring:
    """Tests for parse_docstring on real griffe docstrings."""

    def test_missing_docstring_returns_empty_sections(self):
        """Test None and objects without parsed sections yield no data."""
        assert parse_docstring(None) == {}
        assert parse_docstring(object()) == {}

    def test_summary_and_text(self):
        """Test the summary is the first text block and text holds all text."""
        sections = parse("Summary line.\n\nMore detail.")
        assert sections == {
            "summary": "Summary line.\n\nMore detail.",
            "text": "Summary line.\n\nMore detail.",
        }

    def test_text_and_summary_are_last_keys(self):
        """Test summary and text are appended after the structured sections."""
        sections = parse("Summary.\n\nArgs:\n    x (int): The x.")
        assert list(sections) == ["parameters", "summary", "text"]

    def test_args_section(self):
        """Test Args sections keep annotations, descriptions, and a value key."""
        sections = parse("Summary.\n\nArgs:\n    x (int): The x.\n    y: The y.")
        assert sections["parameters"] == [
            {"name": "x", "annotation": "int", "description": "The x.", "value": None},
            {"name": "y", "annotation": "", "description": "The y.", "value": None},
        ]

    def test_single_return_is_a_dict(self):
        """Test a single documented return value is stored as a dict."""
        sections = parse("Summary.\n\nReturns:\n    The total.")
        assert sections["returns"] == {
            "name": "",
            "annotation": "",
            "description": "The total.",
        }

    def test_raises_section(self):
        """Test Raises sections record the exception type and description."""
        sections = parse(
            "Summary.\n\nRaises:\n    ValueError: If bad.\n    KeyError: If absent."
        )
        assert sections["raises"] == [
            {"type": "ValueError", "description": "If bad."},
            {"type": "KeyError", "description": "If absent."},
        ]

    def test_attributes_section(self):
        """Test Attributes sections record name, annotation, and description."""
        sections = parse("Summary.\n\nAttributes:\n    size (int): The size.")
        assert sections["attributes"] == [
            {"name": "size", "annotation": "int", "description": "The size."}
        ]

    def test_yields_section_uses_generic_parsing(self):
        """Test Yields sections are serialized as named elements."""
        sections = parse("Summary.\n\nYields:\n    Each item.")
        assert sections["yields"] == [
            {"name": "", "annotation": "", "description": "Each item."}
        ]

    def test_empty_docstring_has_empty_text(self):
        """Test an empty docstring produces only an empty text entry."""
        assert parse("") == {"text": ""}

    def test_admonitions_are_currently_dropped(self):
        """Test Note sections are dropped because griffe payloads lack ``text``.

        This pins current output; griffe's admonition payload exposes
        ``description`` rather than ``text``, so no ``note`` key is emitted.
        """
        sections = parse("Summary.\n\nNote:\n    Careful.")
        assert "note" not in sections
        assert "admonition" not in sections

    @pytest.mark.xfail(raises=AttributeError, strict=True)
    def test_examples_from_griffe_are_unsupported(self):
        """Test griffe's tuple-shaped Examples values are not yet handled."""
        parse("Summary.\n\nExamples:\n    >>> 1 + 1\n    2")


class TestSectionParsers:
    """Tests for individual section parsers using stand-in section objects."""

    def test_examples_section(self):
        """Test examples keep stripped titles and code."""
        section = SimpleNamespace(
            value=[
                SimpleNamespace(title=" Usage ", value=" f() \n"),
                SimpleNamespace(title=None, value="g()"),
            ]
        )
        assert parse_examples_section(section) == [
            {"title": "Usage", "code": "f()"},
            {"title": None, "code": "g()"},
        ]

    def test_admonition_is_grouped_by_kind(self):
        """Test admonitions with kind and text payloads accumulate per kind."""
        sections: dict = {}
        for title in ("First", None):
            section = SimpleNamespace(
                title=title, value=SimpleNamespace(kind="note", text=" Body ")
            )
            parse_admonition_section(section, sections)
        assert sections == {
            "note": [
                {"title": "First", "text": "Body"},
                {"title": "note", "text": "Body"},
            ]
        }

    def test_admonition_with_invalid_payload_is_ignored(self):
        """Test admonitions without string kind and text are skipped."""
        sections: dict = {}
        section = SimpleNamespace(title="T", value=SimpleNamespace(kind="note"))
        parse_admonition_section(section, sections)
        assert sections == {}

    def test_generic_section_item_shapes(self):
        """Test generic sections handle named, text, and arbitrary items."""
        section = SimpleNamespace(
            value=[
                SimpleNamespace(name="w", description=" Warned. "),
                SimpleNamespace(text=" plain "),
                7,
            ]
        )
        assert parse_generic_section(section) == [
            {"name": "w", "description": "Warned."},
            "plain",
            "7",
        ]

    def test_generic_section_non_list_values(self):
        """Test generic sections stringify scalar values or report no value."""
        assert parse_generic_section(SimpleNamespace(value="Old API.")) == "Old API."
        assert parse_generic_section(object()) == "Unsupported section structure"


class TestCombineText:
    """Tests for combine_text."""

    def test_no_summary_joins_parts(self):
        """Test text parts are joined when there is no summary."""
        assert combine_text("", ["a", "", "b"]) == "a\n\nb"

    def test_text_starting_with_summary_is_kept(self):
        """Test text already starting with the summary is not prefixed."""
        assert combine_text("A.", ["A.", "B."]) == "A.\n\nB."

    def test_summary_is_prefixed_when_missing(self):
        """Test the summary is prepended when the text does not start with it."""
        assert combine_text("A.", ["B."]) == "A.\n\nB."

    def test_summary_without_text(self):
        """Test the summary is used alone when there is no text."""
        assert combine_text("A.", []) == "A."
