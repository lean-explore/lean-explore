"""Generates structured documentation data from Python source files.

This script uses griffe to parse the ``src/lean_explore`` package and extract
comprehensive information about modules, classes, functions, and their
docstrings. The output is serialized to ``data/module_data.json`` for
consumption by frontend applications. Run it from the repository root::

    python scripts/generate_docs_data.py

The implementation lives in the sibling ``docs_data`` package.
"""

if __package__:
    from .docs_data.traversal import main
else:
    from docs_data.traversal import main

if __name__ == "__main__":
    main()
