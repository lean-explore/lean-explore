"""Generates structured API documentation data for the lean_explore package.

The package uses griffe to parse Python sources and serializes modules, classes,
functions, and their parsed docstrings to JSON for the frontend documentation
pages. Modules are organized by responsibility:

- ``schema``: TypedDict schemas describing the JSON output.
- ``docstrings``: Parsing of Google-style docstring sections.
- ``objects``: Serialization of functions, classes, attributes, and modules.
- ``traversal``: Package loading, module collection, and the command-line entry.
"""
