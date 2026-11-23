"""Build-free parser for Lean 4 source files.

`parse` and `parse_files` return one dict per declaration with its kind, fully
qualified name, modifiers, attributes and spans. Spans are tuples
`(start_byte, start_line, start_col, end_byte, end_line, end_col)`, with 1-based lines
and columns in code points, as Lean reports them.

`parse_files(paths, references=True, known_names=[...])` also resolves, for every
declaration, the names written in its signature by Lean's lookup rules (namespaces,
`open`, `export`, section variables, declaration order and imports) and adds them
as `"references"`: full names of declarations of `paths` or of `known_names`, in
order of first appearance. Nothing is elaborated, so references made through
notation, field access or instances are missing. Names that only shadow others,
such as private declarations and structure fields, are included, and so can be the
declaration's own name; a caller keeps the ones it stores. `include_approximate=True`
lets names derived by a naming rule be referred to as well.
"""

from ._native import commands, parse, parse_files

__all__ = ["commands", "parse", "parse_files"]
