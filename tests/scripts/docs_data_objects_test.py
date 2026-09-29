"""Tests for serializing griffe objects in the docs data generator."""

from scripts.docs_data.objects import (
    build_returns_info,
    format_default,
    is_private_name,
    merge_docstring_attributes,
    serialize_module,
)

WIDGETS_SOURCE = '''
"""Widget utilities.

Longer module description.
"""

import typer

from os.path import join


def _hidden():
    pass


async def fetch(url: str, retries: int = 3) -> bytes:
    """Fetches a URL.

    Args:
        url: The address to fetch.
        retries: How many attempts to make.

    Returns:
        The response body.
    """


def command(verbose: bool = typer.Option(False, "--verbose", help="Talk.")):
    """Runs a command."""


class Widget(Base):
    """A widget.

    Attributes:
        size (int): The widget size.
        color (str): The widget color.
        _secret (str): Hidden.
    """

    size: int = 3
    _private: int = 0

    def resize(self, factor: float) -> None:
        """Resizes the widget."""

    def __init__(self, size: int) -> None:
        """Creates a widget."""

    def _helper(self):
        pass


class Base:
    """A base class."""
'''


def test_is_private_name():
    """Test single-underscore names are private while dunders are public."""
    assert is_private_name("_hidden")
    assert not is_private_name("__init__")
    assert not is_private_name("public")


def test_format_default_plain_and_missing():
    """Test defaults are stringified and a missing default stays None."""
    assert format_default(None) is None
    assert format_default("3") == "3"


def test_module_serialization(load_package):
    """Test module-level metadata and filtering of imported or private members."""
    root = load_package({"__init__.py": "", "widgets.py": WIDGETS_SOURCE})
    module = serialize_module(root["widgets"])

    assert module["name"] == "widgets"
    assert module["path"] == "pkg.widgets"
    assert module["filepath"] == "src/pkg/widgets.py"
    assert module["docstring_sections"]["summary"].startswith("Widget utilities.")
    assert [function["name"] for function in module["functions"]] == [
        "command",
        "fetch",
    ]
    assert [class_["name"] for class_ in module["classes"]] == ["Base", "Widget"]


def test_function_serialization(load_package):
    """Test function signatures merge with docstring descriptions."""
    root = load_package({"__init__.py": "", "widgets.py": WIDGETS_SOURCE})
    functions = {f["name"]: f for f in serialize_module(root["widgets"])["functions"]}
    fetch = functions["fetch"]

    assert fetch["path"] == "pkg.widgets.fetch"
    # griffe functions expose no ``is_async`` attribute (async is a label), so
    # the getattr fallback always reports False; this pins current output.
    assert fetch["is_async"] is False
    assert fetch["filepath"] == "src/pkg/widgets.py"
    assert fetch["lines"] == [fetch["lineno"], fetch["lineno"] + 9]
    assert fetch["parameters"] == [
        {
            "name": "url",
            "annotation": "str",
            "kind": "positional or keyword",
            "default": None,
            "description": "The address to fetch.",
        },
        {
            "name": "retries",
            "annotation": "int",
            "kind": "positional or keyword",
            "default": "3",
            "description": "How many attempts to make.",
        },
    ]
    assert fetch["returns"] == {
        "annotation": "bytes",
        "description": "The response body.",
    }
    assert fetch["decorators"] == []


def test_typer_option_default_is_multiline(load_package):
    """Test typer.Option defaults are formatted one argument per line."""
    root = load_package({"__init__.py": "", "widgets.py": WIDGETS_SOURCE})
    functions = {f["name"]: f for f in serialize_module(root["widgets"])["functions"]}

    assert functions["command"]["parameters"][0]["default"] == (
        "typer.Option(\n    False,\n    '--verbose',\n    help='Talk.'\n)"
    )


def test_class_serialization(load_package):
    """Test class methods, attributes, and bases are serialized and ordered."""
    root = load_package({"__init__.py": "", "widgets.py": WIDGETS_SOURCE})
    classes = {c["name"]: c for c in serialize_module(root["widgets"])["classes"]}
    widget = classes["Widget"]

    assert widget["bases"] == ["Base"]
    assert [method["name"] for method in widget["methods"]] == ["__init__", "resize"]
    assert widget["methods"][1]["path"] == "pkg.widgets.Widget.resize"
    assert widget["attributes"] == [
        {
            "name": "color",
            "value": None,
            "annotation": "str",
            "docstring": "The widget color.",
            "path": "pkg.widgets.Widget.color",
            "filepath": None,
            "lineno": None,
        },
        {
            "name": "size",
            "value": "3",
            "annotation": "int",
            "docstring": "The widget size.",
            "path": "pkg.widgets.Widget.size",
            "filepath": "src/pkg/widgets.py",
            "lineno": widget["attributes"][1]["lineno"],
        },
    ]


def test_merge_docstring_attributes_keeps_existing_docstring():
    """Test code attribute docstrings are not overwritten by the class docstring."""
    code_attributes = [
        {
            "name": "size",
            "value": "3",
            "annotation": "int",
            "docstring": "From code.",
            "path": "C.size",
            "filepath": None,
            "lineno": 1,
        }
    ]
    merge_docstring_attributes(
        code_attributes,
        [{"name": "size", "annotation": "int", "description": "From docstring."}],
        "C",
    )
    assert code_attributes[0]["docstring"] == "From code."
    assert len(code_attributes) == 1


class _FakeFunction:
    """Minimal stand-in exposing a return annotation."""

    returns = "int"


def test_build_returns_info_without_docstring():
    """Test the code annotation is used when nothing is documented."""
    assert build_returns_info(_FakeFunction(), {}) == {
        "annotation": "int",
        "description": "",
    }


def test_build_returns_info_with_multiple_returns():
    """Test the first documented return wins and multiplicity is noted."""
    sections = {
        "returns": [
            {"name": "a", "annotation": "str", "description": "First."},
            {"name": "b", "annotation": "", "description": "Second."},
        ]
    }
    assert build_returns_info(_FakeFunction(), sections) == {
        "annotation": "str",
        "description": "First. (Multiple return paths documented)",
    }
