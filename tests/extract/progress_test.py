"""Tests for the extraction pipeline progress bars."""

import time

from rich.progress import TimeRemainingColumn

from lean_explore.extract.progress import RateColumn, create_progress


def _render(column: RateColumn) -> str:
    return column.render(task=None).plain


class TestCreateProgress:
    """Tests for create_progress."""

    def test_extra_columns_go_before_time_remaining(self):
        """Extra columns are inserted just before the time-remaining column."""
        rate_column = RateColumn()

        progress = create_progress(rate_column)

        assert progress.columns[-2] is rate_column
        assert isinstance(progress.columns[-1], TimeRemainingColumn)
        assert len(progress.columns) == 6


class TestRateColumn:
    """Tests for RateColumn."""

    def test_no_history_renders_placeholder(self):
        """Before any counts the rate is unknown."""
        assert _render(RateColumn()) == "-- emb/s"

    def test_rate_over_window(self):
        """The rate is the windowed count divided by the elapsed time."""
        column = RateColumn(window_seconds=60)
        column.history.append((time.time() - 10, 50))

        rate = float(_render(column).split()[0])

        assert 4.5 < rate <= 5.0

    def test_old_counts_are_pruned(self):
        """add_count drops history older than the window but keeps the total."""
        column = RateColumn(window_seconds=60)
        column.history.append((time.time() - 120, 1000))
        column.total_count = 1000

        column.add_count(5)

        assert [count for _, count in column.history] == [5]
        assert column.total_count == 1005
