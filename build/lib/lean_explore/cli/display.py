"""Display and formatting utilities for CLI output."""

import textwrap

from rich.console import Console
from rich.panel import Panel

from lean_explore.models import SearchResponse, SearchResult


def _wrap_line(line: str, width: int) -> list[str]:
    """Wraps a single line of text to the specified width.

    Args:
        line: The line to wrap.
        width: The target width for wrapped text.

    Returns:
        List of wrapped line segments, each padded to the target width.
    """
    empty_line = " " * width
    if not line.strip():
        return [empty_line]

    segments = textwrap.wrap(
        line,
        width=width,
        replace_whitespace=True,
        drop_whitespace=True,
        break_long_words=True,
        break_on_hyphens=True,
    )
    return [segment.ljust(width) for segment in segments] if segments else [empty_line]


def _format_text_for_panel(text_content: str | None, width: int = 80) -> str:
    """Wraps text and pads lines to ensure fixed content width for a Panel.

    Splits text into paragraphs (by double newline), wraps each line within
    paragraphs, and pads all lines to the target width.

    Args:
        text_content: The text to format.
        width: The target width for wrapped text.

    Returns:
        Formatted text with proper line wrapping and padding.
    """
    empty_line = " " * width
    if not text_content:
        return empty_line

    output_lines = []
    paragraphs = text_content.split("\n\n")

    for index, paragraph in enumerate(paragraphs):
        # Handle empty paragraphs (preserve blank lines between paragraphs)
        if not paragraph.strip():
            if index < len(paragraphs) - 1:
                output_lines.append(empty_line)
            continue

        # Process each line within the paragraph
        for line in paragraph.splitlines():
            output_lines.extend(_wrap_line(line, width))

        # Add separator between paragraphs (except after the last one)
        if index < len(paragraphs) - 1:
            output_lines.append(empty_line)

    return "\n".join(output_lines) if output_lines else empty_line


def _print_text_panel(
    console: Console, text: str | None, title: str, color: str
) -> None:
    """Print text in a fixed-width titled panel, skipping empty text.

    Args:
        console: The Rich console to print to.
        text: The panel contents. Nothing is printed when empty.
        title: The panel title.
        color: Rich color used for the title and border.
    """
    if not text:
        return
    console.print(
        Panel(
            _format_text_for_panel(text),
            title=f"[bold {color}]{title}[/bold {color}]",
            border_style=color,
            expand=False,
            padding=(0, 1),
        )
    )


def _print_result(console: Console, position: int, item: SearchResult) -> None:
    """Print one search result with its metadata and text panels.

    Args:
        console: The Rich console to print to.
        position: One-based position of the result in the list.
        item: The search result to print.
    """
    console.rule(f"[bold]Result {position}[/bold]", style="dim")
    console.print(f"[bold cyan]ID:[/bold cyan] [dim]{item.id}[/dim]")
    console.print(f"[bold cyan]Name:[/bold cyan] {item.name}")
    console.print(f"[bold cyan]Module:[/bold cyan] [green]{item.module}[/green]")
    console.print(
        f"[bold cyan]Source:[/bold cyan] "
        f"[link={item.source_link}]{item.source_link}[/link]"
    )
    _print_text_panel(console, item.source_text, "Code", "green")
    _print_text_panel(console, item.docstring, "Docstring", "blue")
    _print_text_panel(console, item.informalization, "Informalization", "magenta")


def display_search_results(
    response: SearchResponse,
    display_limit: int = 5,
    console: Console | None = None,
) -> None:
    """Displays search results using fixed-width Panels for each item.

    Args:
        response: The search response containing results to display.
        display_limit: Maximum number of results to show.
        console: The Rich console to use for output. If None, creates a new one.
    """
    if console is None:
        console = Console()

    console.print(
        Panel(
            f"[bold cyan]Search Query:[/bold cyan] {response.query}",
            expand=False,
            border_style="dim",
        )
    )

    shown_results = response.results[:display_limit]
    time_info = (
        f"Time: {response.processing_time_ms}ms" if response.processing_time_ms else ""
    )
    console.print(
        f"Showing {len(shown_results)} of {response.count} results. {time_info}"
    )

    if not shown_results:
        console.print("[yellow]No results found.[/yellow]")
        return

    console.print("")
    for position, item in enumerate(shown_results, start=1):
        _print_result(console, position, item)
        if position < len(shown_results):
            console.print("")

    console.rule(style="dim")
    hidden_count = len(response.results) - len(shown_results)
    if hidden_count > 0:
        console.print(
            f"...and {hidden_count} more results received but not shown due to limit."
        )
