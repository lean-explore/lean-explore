"""Parsing of griffe Google-style docstrings into structured section data."""

from collections.abc import Callable

from griffe import (
    DocstringNamedElement,
    DocstringSectionAdmonition,
    DocstringSectionAttributes,
    DocstringSectionExamples,
    DocstringSectionParameters,
    DocstringSectionRaises,
    DocstringSectionReturns,
    DocstringSectionText,
    Expr,
)

from .schema import (
    DocstringAttributeDict,
    DocstringSections,
    ExampleDict,
    ExceptionDict,
    ParameterDict,
    ReturnsSectionData,
)


def resolve_annotation(annotation: str | Expr | None) -> str:
    """Converts a griffe annotation to its string representation."""
    if isinstance(annotation, Expr | str):
        return str(annotation)
    return ""


def _strip_description(description: str | None) -> str:
    """Returns a stripped description, or an empty string when absent."""
    return description.strip() if description else ""


def extract_summary_and_text(
    sections: list, summary_holder: list[str], text_parts: list[str]
) -> None:
    """Extracts summary and full text from docstring text sections.

    Args:
        sections: List of docstring sections to process.
        summary_holder: Single-element list to store the summary (first text block).
        text_parts: List to accumulate all text parts.
    """
    for section in sections:
        if isinstance(section, DocstringSectionText):
            if not summary_holder[0]:
                summary_holder[0] = section.value.strip()
            text_parts.append(section.value.strip())


def parse_parameters_section(
    section: DocstringSectionParameters,
) -> list[ParameterDict]:
    """Parses a parameters section from a docstring.

    Note that the emitted dictionaries carry a ``value`` key and omit the
    ``kind``/``default`` keys declared by ``ParameterDict``. The output format is
    consumed by the frontend, so this shape is preserved as-is.
    """
    return [
        {
            "name": parameter.name,
            "annotation": resolve_annotation(parameter.annotation),
            "description": _strip_description(parameter.description),
            "value": str(parameter.value) if parameter.value is not None else None,
        }
        for parameter in section.value
    ]


def parse_returns_section(section: DocstringSectionReturns) -> ReturnsSectionData:
    """Parses a returns section from a docstring.

    Returns a single dict for one return value, a list for multiple, or None for empty.
    """
    returns_data = [
        {
            "name": item.name if hasattr(item, "name") else "",
            "annotation": resolve_annotation(item.annotation),
            "description": _strip_description(item.description),
        }
        for item in section.value
    ]

    if len(returns_data) == 1:
        return returns_data[0]
    elif len(returns_data) > 1:
        return returns_data
    return None


def parse_attributes_section(
    section: DocstringSectionAttributes,
) -> list[DocstringAttributeDict]:
    """Parses an attributes section from a docstring."""
    return [
        {
            "name": attribute.name,
            "annotation": resolve_annotation(attribute.annotation),
            "description": _strip_description(attribute.description),
        }
        for attribute in section.value
    ]


def parse_raises_section(section: DocstringSectionRaises) -> list[ExceptionDict]:
    """Parses a raises section from a docstring."""
    return [
        {
            "type": resolve_annotation(exception.annotation),
            "description": _strip_description(exception.description),
        }
        for exception in section.value
    ]


def parse_examples_section(section: DocstringSectionExamples) -> list[ExampleDict]:
    """Parses an examples section from a docstring."""
    return [
        {
            "title": example.title.strip() if example.title else None,
            "code": example.value.strip(),
        }
        for example in section.value
    ]


def parse_admonition_section(
    section: DocstringSectionAdmonition, sections_data: DocstringSections
) -> None:
    """Parses an admonition section (note, warning, etc.).

    Adds the admonition to sections_data under its kind.
    """
    payload = section.value

    # Validate payload structure before accessing attributes
    if not (hasattr(payload, "kind") and isinstance(payload.kind, str)):
        return
    if not (hasattr(payload, "text") and isinstance(payload.text, str)):
        return

    kind = payload.kind
    admonition_item = {
        "title": section.title.strip() if section.title else kind,
        "text": payload.text.strip(),
    }

    if kind not in sections_data:
        sections_data[kind] = []
    sections_data[kind].append(admonition_item)


def _serialize_generic_item(item: object) -> dict | str:
    """Serializes one item of a generic docstring section."""
    if isinstance(item, DocstringNamedElement):
        return {
            "name": item.name,
            "annotation": resolve_annotation(item.annotation)
            if hasattr(item, "annotation")
            else "",
            "description": _strip_description(item.description),
        }
    if hasattr(item, "name") and hasattr(item, "description"):
        return {
            "name": item.name,
            "description": _strip_description(item.description),
        }
    if hasattr(item, "text"):
        return item.text.strip()
    return str(item)


def parse_generic_section(section) -> list | str:
    """Parses generic docstring sections like warns, yields, etc."""
    if not hasattr(section, "value") or not isinstance(section.value, list):
        return (
            str(section.value)
            if hasattr(section, "value")
            else "Unsupported section structure"
        )
    return [_serialize_generic_item(item) for item in section.value]


_LIST_SECTION_PARSERS: tuple[tuple[type, Callable[..., list]], ...] = (
    (DocstringSectionParameters, parse_parameters_section),
    (DocstringSectionAttributes, parse_attributes_section),
    (DocstringSectionRaises, parse_raises_section),
    (DocstringSectionExamples, parse_examples_section),
)
"""Section types whose parsed value is stored directly under the section kind."""


def parse_section(section, sections_data: DocstringSections) -> None:
    """Parses one non-text docstring section into sections_data.

    Text sections are ignored here; they are consolidated separately into the
    ``summary`` and ``text`` entries.
    """
    if isinstance(section, DocstringSectionText):
        return
    kind = section.kind.value
    if isinstance(section, DocstringSectionReturns):
        result = parse_returns_section(section)
        if result:
            sections_data[kind] = result
        return
    if isinstance(section, DocstringSectionAdmonition):
        parse_admonition_section(section, sections_data)
        return
    for section_type, parser in _LIST_SECTION_PARSERS:
        if isinstance(section, section_type):
            sections_data[kind] = parser(section)
            return
    sections_data[kind] = parse_generic_section(section)


def combine_text(summary: str, text_parts: list[str]) -> str:
    """Consolidates the docstring text parts into one text block.

    Args:
        summary: The first text block of the docstring, or an empty string.
        text_parts: All stripped text blocks of the docstring, in order.

    Returns:
        The joined text, prefixed with the summary when it does not already
        start with it.
    """
    text_content = "\n\n".join(part for part in text_parts if part)
    if summary and text_content.strip().startswith(summary.strip()):
        return text_content
    if summary:
        return f"{summary}\n\n{text_content}".strip() if text_content else summary
    return text_content


def parse_docstring(docstring_object: object | None) -> DocstringSections:
    """Parses all sections from a griffe docstring object into structured data."""
    if not docstring_object or not hasattr(docstring_object, "parsed"):
        return DocstringSections()

    sections_data: DocstringSections = DocstringSections()
    summary = [""]
    text_parts: list[str] = []
    extract_summary_and_text(docstring_object.parsed, summary, text_parts)

    for section in docstring_object.parsed:
        parse_section(section, sections_data)

    if summary[0]:
        sections_data["summary"] = summary[0]
    sections_data["text"] = combine_text(summary[0], text_parts)
    return sections_data
