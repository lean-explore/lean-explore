"""Build-free parser for Lean 4 source files.

`parse` and `parse_files` return one dict per declaration with its kind, fully
qualified name, modifiers, attributes and spans. Spans are tuples
`(start_byte, start_line, start_col, end_byte, end_line, end_col)`, with 1-based lines
and columns in code points, as Lean reports them.
"""

from ._native import commands, parse, parse_files

__all__ = ["commands", "parse", "parse_files"]
