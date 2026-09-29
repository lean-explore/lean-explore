"""Report Python files and functions that exceed the project's size limits.

Soft limits: files at most 500 lines, and function bodies at most 40 lines of
code. Docstrings, comments, and blank lines do not count toward a function's
length.

Usage:
    python scripts/check_sizes.py [--strict] [PATH ...]

Paths default to src, tests, and scripts. Without --strict the script only
reports; with --strict it exits with status 1 when any limit is exceeded.
"""

import argparse
import ast
import io
import sys
import tokenize
from pathlib import Path

MAX_FILE_LINES = 500
MAX_FUNCTION_CODE_LINES = 40
DEFAULT_PATHS = ("src", "tests", "scripts")
_NON_CODE_TOKENS = {
    tokenize.COMMENT,
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENDMARKER,
}


def _code_lines(source: str) -> set[int]:
    """Return the line numbers that contain code tokens."""
    lines: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in _NON_CODE_TOKENS:
            lines.update(range(token.start[0], token.end[0] + 1))
    return lines


def _docstring_lines(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[int]:
    """Return the line numbers of a function's docstring, if it has one."""
    first = function.body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return set(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return set()


def oversized_functions(path: Path) -> list[tuple[int, str, int]]:
    """Find functions in a file whose bodies exceed the code-line limit.

    Args:
        path: Python file to inspect.

    Returns:
        (line number, function name, code lines) for each oversized function.
    """
    source = path.read_text()
    code_lines = _code_lines(source)
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = set(range(node.body[0].lineno, (node.end_lineno or 0) + 1))
        count = len((body & code_lines) - _docstring_lines(node))
        if count > MAX_FUNCTION_CODE_LINES:
            found.append((node.lineno, node.name, count))
    return found


def main() -> int:
    """Print size violations and return the process exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", default=DEFAULT_PATHS)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    files = sorted(
        file for path in map(Path, args.paths) for file in path.rglob("*.py")
    )
    violations = []
    for file in files:
        line_count = len(file.read_text().splitlines())
        if line_count > MAX_FILE_LINES:
            violations.append(f"{file}: {line_count} lines (max {MAX_FILE_LINES})")
        for line, name, count in oversized_functions(file):
            violations.append(
                f"{file}:{line}: {name} has {count} code lines "
                f"(max {MAX_FUNCTION_CODE_LINES})"
            )

    for violation in violations:
        print(violation)
    print(f"{len(violations)} size limit violation(s) in {len(files)} files.")
    return 1 if violations and args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
