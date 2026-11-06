"""Signal definitions for the weekly dip system.

Kept in its own module so both the backtest engines and the optional filters can
share exactly the same definition of a "dip".
"""

from __future__ import annotations

from typing import Sequence

from saudi_data import WeeklyBar


def is_dip_signal(weekly: Sequence[WeeklyBar], index: int, n_down: int) -> bool:
    """Return True if weeks ``index - n_down + 1 .. index`` are all down weeks.

    A week is "down" when its close is below the previous week's close.
    """
    first = index - n_down + 1
    if first - 1 < 0:
        return False
    for offset in range(n_down):
        current = weekly[first + offset]
        previous = weekly[first + offset - 1]
        if current.close >= previous.close:
            return False
    return True


def signal_indices(weekly: Sequence[WeeklyBar], n_down: int) -> list[int]:
    """Return every week index that triggers a dip signal."""
    return [index for index in range(len(weekly)) if is_dip_signal(weekly, index, n_down)]


def down_streak(weekly: Sequence[WeeklyBar], index: int) -> int:
    """Return how many consecutive down weeks end at ``index``."""
    streak = 0
    for cursor in range(index, 0, -1):
        if weekly[cursor].close < weekly[cursor - 1].close:
            streak += 1
        else:
            break
    return streak
