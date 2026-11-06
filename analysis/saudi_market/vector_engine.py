"""Vectorised (event-free) backtest for the "buy after N down weeks" idea.

Rules implemented here mirror the NautilusTrader strategy in
``nautilus_engine.py`` so the two engines can be cross-checked:

1. A week counts as "down" when its close is below the previous week's close.
2. If the last ``n_down`` completed weeks are all down, the strategy goes long
   at the **open of the next week** (the signal is only known at the close of
   the last down week, so this is free of look-ahead bias).
3. The position is closed at the open of week ``entry + hold_weeks``, or
   earlier if a close-based stop / target is breached (the exit is then taken
   at the next week's open, which is deliberately conservative).
4. One position at a time, no pyramiding, no shorting (Tadawul restriction).

Only the standard library is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import date
from typing import Iterable
from typing import Sequence

from saudi_data import WeeklyBar


@dataclass(frozen=True, slots=True)
class Trade:
    """A single round-trip in the weekly dip system."""

    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    weeks_held: int
    gross_return: float
    net_return: float
    exit_reason: str


@dataclass(slots=True)
class BacktestResult:
    """Container for a completed vector backtest."""

    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[date, float]] = field(default_factory=list)
    weeks_in_market: int = 0
    total_weeks: int = 0

    @property
    def stats(self) -> dict[str, float]:
        return summarize(self)


def is_dip_signal(weekly: Sequence[WeeklyBar], index: int, n_down: int) -> bool:
    """Return True if weeks ``index - n_down + 1 .. index`` are all down weeks."""
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
    return [
        index
        for index in range(len(weekly))
        if is_dip_signal(weekly, index, n_down)
    ]


def run_vector_backtest(
    weekly: Sequence[WeeklyBar],
    *,
    n_down: int = 3,
    hold_weeks: int = 4,
    stop_pct: float = 0.07,
    target_pct: float = 0.0,
    fee_rate: float = 0.00155,
    verbose: bool = False,
) -> BacktestResult:
    """Run the weekly dip system over ``weekly`` bars."""
    if n_down < 1:
        raise ValueError("n_down must be >= 1")
    if hold_weeks < 1:
        raise ValueError("hold_weeks must be >= 1")
    if not weekly:
        raise ValueError("weekly bars are empty")

    result = BacktestResult(total_weeks=len(weekly))
    equity = 1.0
    result.equity_curve.append((weekly[0].week_start, equity))

    index = 0
    last_index = len(weekly) - 1
    while index < last_index:
        if not is_dip_signal(weekly, index, n_down):
            index += 1
            continue

        entry_index = index + 1
        if entry_index >= last_index:
            break
        entry_bar = weekly[entry_index]
        entry_price = entry_bar.open
        cost_basis = entry_price * (1.0 + fee_rate)
        stop_price = entry_price * (1.0 - stop_pct) if stop_pct > 0 else 0.0
        target_price = entry_price * (1.0 + target_pct) if target_pct > 0 else 0.0

        exit_index = min(entry_index + hold_weeks, last_index)
        exit_price = weekly[exit_index].open
        exit_reason = "time"

        for cursor in range(entry_index, min(entry_index + hold_weeks, last_index + 1)):
            close = weekly[cursor].close
            if stop_price and close <= stop_price:
                exit_index = min(cursor + 1, last_index)
                exit_price = weekly[exit_index].open
                exit_reason = "stop"
                break
            if target_price and close >= target_price:
                exit_index = min(cursor + 1, last_index)
                exit_price = weekly[exit_index].open
                exit_reason = "target"
                break

        if exit_index == last_index and exit_reason == "time" and entry_index + hold_weeks > last_index:
            # Not enough data to hold the full window: mark out at the last close.
            exit_price = weekly[last_index].close
            exit_reason = "end_of_data"

        proceeds = exit_price * (1.0 - fee_rate)
        gross = exit_price / entry_price - 1.0
        net = proceeds / cost_basis - 1.0

        trade = Trade(
            entry_date=entry_bar.week_start,
            exit_date=weekly[exit_index].week_start,
            entry_price=entry_price,
            exit_price=exit_price,
            weeks_held=exit_index - entry_index,
            gross_return=gross,
            net_return=net,
            exit_reason=exit_reason,
        )
        result.trades.append(trade)
        result.weeks_in_market += trade.weeks_held

        equity *= 1.0 + net
        result.equity_curve.append((trade.exit_date, equity))

        index = exit_index + 1

    if verbose:
        for trade in result.trades:
            print(
                f"{trade.entry_date} -> {trade.exit_date} | "
                f"{trade.entry_price:8.2f} -> {trade.exit_price:8.2f} | "
                f"{trade.net_return * 100:6.2f}% | {trade.weeks_held}w | {trade.exit_reason}",
            )

    return result


def max_drawdown(values: Iterable[float]) -> float:
    """Return the maximum peak-to-trough drawdown of an equity series (0..1)."""
    peak = None
    worst = 0.0
    for value in values:
        if peak is None or value > peak:
            peak = value
        if peak and peak > 0:
            worst = min(worst, value / peak - 1.0)
    return abs(worst)


def summarize(result: BacktestResult, *, buy_hold: float | None = None) -> dict[str, float]:
    """Compute headline statistics for a completed backtest."""
    trades = result.trades
    count = len(trades)
    if count == 0:
        return {
            "trades": 0.0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "best": 0.0,
            "worst": 0.0,
            "profit_factor": 0.0,
            "cumulative_return": 0.0,
            "max_drawdown": 0.0,
            "exposure": 0.0,
            "buy_hold": buy_hold if buy_hold is not None else 0.0,
        }

    returns = [trade.net_return for trade in trades]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    equity_values = [value for _, value in result.equity_curve]

    return {
        "trades": float(count),
        "win_rate": len(wins) / count,
        "avg_return": sum(returns) / count,
        "best": max(returns),
        "worst": min(returns),
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else float("inf"),
        "cumulative_return": equity_values[-1] - 1.0,
        "max_drawdown": max_drawdown(equity_values),
        "exposure": result.weeks_in_market / max(result.total_weeks, 1),
        "buy_hold": buy_hold if buy_hold is not None else 0.0,
    }


def format_stats(stats: dict[str, float], *, title: str) -> str:
    """Render the stats dictionary as an aligned text block."""
    lines = [
        "",
        f"  {title}",
        "  " + "-" * (len(title) + 2),
        f"  الصفقات                : {int(stats['trades'])}",
        f"  نسبة الصفقات الرابحة   : {stats['win_rate'] * 100:6.1f}%",
        f"  متوسط عائد الصفقة      : {stats['avg_return'] * 100:6.2f}%",
        f"  أفضل صفقة              : {stats['best'] * 100:6.2f}%",
        f"  أسوأ صفقة              : {stats['worst'] * 100:6.2f}%",
        f"  معامل الربح            : {stats['profit_factor']:6.2f}",
        f"  العائد التراكمي        : {stats['cumulative_return'] * 100:6.2f}%",
        f"  أقصى تراجع (Equity)    : {stats['max_drawdown'] * 100:6.2f}%",
        f"  نسبة الوقت في السوق    : {stats['exposure'] * 100:6.1f}%",
        f"  الشراء والاحتفاظ       : {stats['buy_hold'] * 100:6.2f}%",
    ]
    return "\n".join(lines)


def buy_and_hold_return(weekly: Sequence[WeeklyBar], *, fee_rate: float = 0.00155) -> float:
    """Return the total buy & hold return over the weekly series."""
    if len(weekly) < 2:
        return 0.0
    entry = weekly[0].open * (1.0 + fee_rate)
    exit_value = weekly[-1].close * (1.0 - fee_rate)
    return exit_value / entry - 1.0
