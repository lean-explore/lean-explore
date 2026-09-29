"""Rich progress bars shared by the extraction pipeline steps."""

import time
from collections import deque

from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.text import Text


def create_progress(*extra_columns: ProgressColumn) -> Progress:
    """Create the pipeline's standard progress bar.

    Args:
        extra_columns: Columns inserted before the time-remaining column.

    Returns:
        Progress display with spinner, description, bar, percentage, any extra
        columns, and estimated time remaining.
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        *extra_columns,
        TimeRemainingColumn(),
    )


class RateColumn(ProgressColumn):
    """Custom column showing embeddings per second over a rolling window."""

    def __init__(self, window_seconds: int = 300):
        """Initialize rate column.

        Args:
            window_seconds: Rolling window size in seconds for rate calculation
        """
        super().__init__()
        self.window_seconds = window_seconds
        self.history: deque[tuple[float, int]] = deque()
        self.total_count = 0

    def add_count(self, count: int) -> None:
        """Add embedding count with timestamp."""
        now = time.time()
        self.history.append((now, count))
        self.total_count += count
        # Remove old entries outside window
        cutoff = now - self.window_seconds
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()

    def render(self, task: Task) -> Text:
        """Render the rate column."""
        if not self.history:
            return Text("-- emb/s", style="cyan")

        now = time.time()
        cutoff = now - self.window_seconds
        window_count = sum(c for t, c in self.history if t >= cutoff)
        elapsed = now - max(self.history[0][0], cutoff)
        if elapsed > 0:
            return Text(f"{window_count / elapsed:.1f} emb/s", style="cyan")

        return Text("-- emb/s", style="cyan")
