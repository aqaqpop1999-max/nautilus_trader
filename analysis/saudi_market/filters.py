"""Optional context filters for the weekly dip system.

Two filters are supported, and each can be combined with the other:

1. **Regime filter (SMA)** — only take a dip when the benchmark is still above
   its N-day simple moving average (default 200). Rationale: buying "dips"
   inside a structural uptrend is a different trade from buying dips inside a
   bear market; the 2026 Saudi decline was macro/geopolitical, and the index
   spent most of it below its own 200-day average.
2. **Volume filter** — only take a dip when the signal week's volume is at
   least ``x`` times the average weekly volume of the last ``k`` weeks.
   Rationale: a heavy-volume down week is closer to capitulation (forced
   selling) than a quiet drift lower.

Both filters are computed from data that is known at the signal week's close,
so the resulting mask is free of look-ahead bias.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from typing import Sequence

from saudi_data import DailyBar
from saudi_data import WeeklyBar
from signals import is_dip_signal


@dataclass(frozen=True, slots=True)
class FilterConfig:
    """Settings for the optional signal filters."""

    use_sma: bool = False
    sma_period: int = 200
    use_volume: bool = False
    volume_mult: float = 1.2
    volume_lookback: int = 20


@dataclass(slots=True)
class SignalMask:
    """Per-week allow/block decision plus diagnostics for reporting."""

    allowed: list[bool] = field(default_factory=list)
    signals: int = 0
    blocked_regime: int = 0
    blocked_volume: int = 0
    blocked_no_data: int = 0
    volume_available: bool = True

    @property
    def blocked(self) -> int:
        return self.blocked_regime + self.blocked_volume + self.blocked_no_data

    def describe(self) -> str:
        if self.signals == 0:
            return "لا توجد إشارات لتصفيتها"
        parts = [f"إشارات: {self.signals}"]
        if self.blocked_regime:
            parts.append(f"مُنعت (اتجاه): {self.blocked_regime}")
        if self.blocked_volume:
            parts.append(f"مُنعت (حجم): {self.blocked_volume}")
        if self.blocked_no_data:
            parts.append(f"مُنعت (نقص بيانات): {self.blocked_no_data}")
        if not self.volume_available:
            parts.append("تحذير: لا توجد بيانات حجم — فلتر الحجم معطَّل")
        return " | ".join(parts)


def build_sma_by_date(daily: Sequence[DailyBar], period: int) -> dict[date, float]:
    """Return ``{date: SMA(period)}`` using closes up to and including ``date``."""
    if period < 1:
        raise ValueError("period must be >= 1")
    result: dict[date, float] = {}
    window: deque[float] = deque()
    running = 0.0
    for bar in daily:
        window.append(bar.close)
        running += bar.close
        if len(window) > period:
            running -= window.popleft()
        if len(window) == period:
            result[bar.ts] = running / period
    return result


def closes_by_date(daily: Sequence[DailyBar]) -> dict[date, float]:
    """Return ``{date: close}`` for a daily series."""
    return {bar.ts: bar.close for bar in daily}


def weekly_volume_ratio(weekly: Sequence[WeeklyBar], index: int, lookback: int) -> float | None:
    """Return ``volume[index] / mean(volume[index-lookback:index])`` or None."""
    if lookback < 1:
        return None
    start = max(0, index - lookback)
    history = [bar.volume for bar in weekly[start:index]]
    if not history:
        return None
    average = sum(history) / len(history)
    if average <= 0:
        return None
    return weekly[index].volume / average


def build_signal_mask(
    weekly: Sequence[WeeklyBar],
    daily: Sequence[DailyBar],
    *,
    n_down: int,
    config: FilterConfig,
    benchmark_daily: Sequence[DailyBar] | None = None,
) -> SignalMask:
    """Evaluate every dip signal against the enabled filters.

    ``benchmark_daily`` is the series used by the regime filter (for example the
    TASI index). When omitted, the traded instrument itself is used, i.e. "buy
    the dip only while the stock is above its own N-day average".
    """
    mask = SignalMask(volume_available=any(bar.volume > 0 for bar in weekly))

    regime_series = benchmark_daily if benchmark_daily is not None else daily
    sma_by_date = (
        build_sma_by_date(regime_series, config.sma_period) if config.use_sma else {}
    )
    regime_closes = closes_by_date(regime_series) if config.use_sma else {}

    for index in range(len(weekly)):
        if not is_dip_signal(weekly, index, n_down):
            mask.allowed.append(False)
            continue

        mask.signals += 1
        allowed = True

        if config.use_sma:
            signal_day = weekly[index].last_trading_day
            sma_value = sma_by_date.get(signal_day)
            close_value = regime_closes.get(signal_day)
            if sma_value is None or close_value is None:
                mask.blocked_no_data += 1
                allowed = False
            elif close_value <= sma_value:
                mask.blocked_regime += 1
                allowed = False

        if allowed and config.use_volume and mask.volume_available:
            ratio = weekly_volume_ratio(weekly, index, config.volume_lookback)
            if ratio is not None and ratio < config.volume_mult:
                mask.blocked_volume += 1
                allowed = False

        mask.allowed.append(allowed)

    return mask


def allowed_weeks(weekly: Sequence[WeeklyBar], mask: SignalMask) -> frozenset[date]:
    """Return the week-start dates whose signal survives the filters."""
    return frozenset(
        bar.week_start for bar, allowed in zip(weekly, mask.allowed, strict=False) if allowed
    )
