"""Build-free parser for Lean 4 source files.

`parse` and `parse_files` return one dict per declaration with its kind, fully
qualified name, modifiers, attributes and spans. Spans are tuples
`(start_byte, start_line, start_col, end_byte, end_line, end_col)`, with 1-based lines
and columns in code points, as Lean reports them.

`parse_files(paths, references=True, known_names=[...], context_paths=[...])` also
finds, for every declaration, what its signature refers to and adds it as
`"references"`: full names of declarations of `paths`, of `context_paths` or of
`known_names`, in order of first appearance. `"type_references"` are the ones of them
written in the type after the signature's colon. A reference is an identifier resolved
by Lean's lookup rules (namespaces, `open`, `export`, section variables, declaration
order and imports), a constant that a notation declared in source stands for (`+` is
`HAdd.hAdd`), or a field accessed on a term whose type is written (`F.obj` with
`F : C ⥤ D` is `CategoryTheory.Functor.obj`). `context_paths` are Lean files outside
the project, those of Lean itself and of dependencies, which declare the notations
and names that the project uses; a file's path must end with its module name.
Nothing is elaborated, so references made through instances, coercions and implicit
arguments are missing. Names that only shadow others, such as private declarations
and structure fields, are included, and so can be the declaration's own name; a
caller keeps the ones it stores. `include_approximate=True` lets names derived by a
naming rule be referred to as well.
"""

from ._native import commands, parse, parse_files

__all__ = ["commands", "parse", "parse_files"]
