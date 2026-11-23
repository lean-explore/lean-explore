"""Helpers shared by the doc-gen4 output readers and the declaration writer."""

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)


def new_progress() -> Progress:
    """Create the progress bar used by doc-gen4 parsing and insertion steps.

    Returns:
        A rich Progress with spinner, description, bar, percentage, and ETA.
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
    )


def drop_self_references(
    dependencies: list[str], declaration_name: str
) -> list[str] | None:
    """Remove a declaration's own name from its dependency list.

    Args:
        dependencies: Referenced declaration names.
        declaration_name: Name of the declaration that owns the list.

    Returns:
        The remaining dependencies, or None if none remain.
    """
    return [name for name in dependencies if name != declaration_name] or None
