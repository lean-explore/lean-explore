"""TypedDict schemas describing the serialized documentation data."""

from typing_extensions import NotRequired, TypedDict


class ParameterDict(TypedDict):
    """Parameter information from function signature and docstring."""

    name: str
    annotation: str
    kind: str
    default: str | None
    description: NotRequired[str]


class ReturnDict(TypedDict):
    """Return value information from function signature and docstring."""

    name: NotRequired[str]
    annotation: str
    description: str


class DocstringAttributeDict(TypedDict):
    """Attribute information from docstring only."""

    name: str
    annotation: str
    description: str
    value: NotRequired[str | None]


class AttributeDict(TypedDict):
    """Attribute information from class code definition."""

    name: str
    value: str | None
    annotation: str
    docstring: str
    path: str
    filepath: str | None
    lineno: int | None


class ExceptionDict(TypedDict):
    """Exception information from docstring raises section."""

    type: str
    description: str


class ExampleDict(TypedDict):
    """Example code from docstring examples section."""

    title: str | None
    code: str


class AdmonitionDict(TypedDict):
    """Admonition (note, warning, etc.) from docstring."""

    title: str
    text: str


class DecoratorDict(TypedDict):
    """Decorator information from function or class definition."""

    text: str
    path: str
    lineno: int | None
    endlineno: int | None


class DocstringSections(TypedDict, total=False):
    """All possible sections parsed from a docstring."""

    summary: str
    text: str
    parameters: list[ParameterDict]
    returns: ReturnDict | list[ReturnDict]
    attributes: list[DocstringAttributeDict]
    raises: list[ExceptionDict]
    examples: list[ExampleDict]
    note: list[AdmonitionDict]
    warning: list[AdmonitionDict]
    deprecated: list[str] | str
    warns: list[str] | str
    yields: list[str] | str
    receives: list[str] | str


class FunctionDict(TypedDict):
    """Serialized function with full documentation."""

    name: str
    path: str
    docstring: str
    docstring_sections: DocstringSections
    parameters: list[ParameterDict]
    returns: ReturnDict
    decorators: list[DecoratorDict]
    is_async: bool
    filepath: str | None
    lineno: int | None
    lines: list[int]


class ClassDict(TypedDict):
    """Serialized class with full documentation."""

    name: str
    path: str
    docstring: str
    docstring_sections: DocstringSections
    methods: list[FunctionDict]
    attributes: list[AttributeDict]
    bases: list[str]
    filepath: str | None
    lineno: int | None
    lines: list[int]


class ModuleDict(TypedDict):
    """Serialized module with full documentation."""

    name: str
    path: str
    filepath: str | None
    docstring: str
    docstring_sections: DocstringSections
    functions: list[FunctionDict]
    classes: list[ClassDict]
    lineno: int | None


ReturnsSectionData = ReturnDict | list[ReturnDict] | None
"""Return type for docstring returns section: single return, multiple, or none."""
