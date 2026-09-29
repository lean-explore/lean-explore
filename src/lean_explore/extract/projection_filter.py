"""Filter auto-generated structure projections out of extracted declarations.

When a Lean structure extends another, doc-gen4 reports the generated parent
projection (e.g. ``Scheme.toLocallyRingedSpace``) with the same source range as
the structure itself. These duplicates are detected by comparing source text
with comments stripped.
"""

from lean_explore.extract.types import Declaration


def strip_lean_comments(source_text: str) -> str:
    """Strip Lean comments from source text for comparison.

    Removes:
    - Line comments: -- to end of line
    - Block comments: /- ... -/ (including nested)
    - Doc comments: /-- ... -/ (just a special form of block comments)

    Args:
        source_text: Lean source text.

    Returns:
        The text without comments and with whitespace collapsed to single
        spaces, for reliable comparison.
    """
    result = []
    i = 0
    length = len(source_text)

    while i < length:
        # Check for block comment (includes doc comments /-- ... -/)
        if i < length - 1 and source_text[i : i + 2] == "/-":
            # Skip the opening /-
            i += 2
            nesting_level = 1
            while i < length and nesting_level > 0:
                if i < length - 1 and source_text[i : i + 2] == "/-":
                    nesting_level += 1
                    i += 2
                elif i < length - 1 and source_text[i : i + 2] == "-/":
                    nesting_level -= 1
                    i += 2
                else:
                    i += 1
            continue

        # Check for line comment
        if i < length - 1 and source_text[i : i + 2] == "--":
            # Skip to end of line
            while i < length and source_text[i] != "\n":
                i += 1
            continue

        result.append(source_text[i])
        i += 1

    # Normalize whitespace: collapse multiple spaces/newlines into single space
    text = "".join(result)
    return " ".join(text.split())


def filter_auto_generated_projections(
    declarations: list[Declaration],
) -> tuple[list[Declaration], int]:
    """Filter out auto-generated 'to*' projections that share source text with parent.

    When a Lean structure extends another, it automatically generates projections
    like `Scheme.toLocallyRingedSpace` that point to the same source location as
    the parent `Scheme` structure. These should be filtered out.

    However, legitimate definitions like `IsOpenImmersion.toScheme` have their
    own unique source text and should be kept.

    Args:
        declarations: List of all extracted declarations.

    Returns:
        Tuple of (filtered declarations, count of removed projections).
    """
    # Build a map of stripped source text -> list of declaration names
    source_to_names: dict[str, list[str]] = {}
    for declaration in declarations:
        stripped = strip_lean_comments(declaration.source_text)
        if stripped not in source_to_names:
            source_to_names[stripped] = []
        source_to_names[stripped].append(declaration.name)

    filtered = []
    removed_count = 0

    for declaration in declarations:
        short_name = declaration.name.rsplit(".", 1)[-1]

        # Check if this looks like a 'toFoo' projection (to + uppercase letter)
        is_to_projection = (
            len(short_name) > 2
            and short_name.startswith("to")
            and short_name[2].isupper()
        )

        if is_to_projection:
            stripped = strip_lean_comments(declaration.source_text)
            declarations_with_same_source = source_to_names.get(stripped, [])

            # If other declarations share this source text, this is auto-generated
            if len(declarations_with_same_source) > 1:
                removed_count += 1
                continue

        filtered.append(declaration)

    return filtered, removed_count
