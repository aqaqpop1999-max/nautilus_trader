#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""أداة اختبار «الشراء بعد أسابيع الهبوط» — ملف واحد للسوق السعودي.

الطريقة من الجوال (Pydroid 3):
    1) نزّل هذا الملف فقط.
    2) افتحه في Pydroid 3.
    3) عدّل الأسطر القريبة من الأعلى (SYMBOL و START).
    4) اضغط ▶.

الطريقة من الطرفية (Termux أو كمبيوتر):
    python single_file.py                    # يستخدم الإعدادات أدناه
    python single_file.py --demo --compare   # بيانات تجريبية بلا إنترنت
    python single_file.py --yahoo 1120.SR --start 2026-01-01 --compare
    python single_file.py --help             # كل الخيارات

ملف مُولَّد آليًا من saudi_data.py + signals.py + filters.py + vector_engine.py +
run_backtest.py بواسطة build_single_file.py. لا تعدّله يدويًا.
"""

from __future__ import annotations

# ===========================================================================
# ١) الإعدادات — عدّل هنا فقط
# ===========================================================================

SYMBOL = "1120.SR"  # 1120 الراجحي | 1180 الأهلي | 1010 الرياض | 2010 سابك | 7010 stc
START = "2026-01-01"  # بداية المتاجرة الفعلية
HISTORY_START = "2025-01-01"  # تاريخ إضافي مُبكر ليشتغل عليه متوسط 200 يوم
N_DOWN = 3  # عدد أسابيع الهبوط المتتالية المطلوبة
HOLD_WEEKS = 4  # مدة الاحتفاظ بالأسابيع
STOP_PCT = 7.0  # وقف الخسارة %
TARGET_PCT = 0.0  # هدف الربح % (0 = بدون هدف)
FEE_BPS = 15.5  # العمولة لكل جهة (نقطة أساس)

COMPARE = True  # جدول مقارنة الفلاتر
SWEEP = False  # مسح شبكي (بطيء على الجوال، لكن يعمل)
SAVE_TRADES = True  # حفظ الصفقات في ملف CSV بجانب هذا الملف

# إذا لم يعمل الإنترنت: ضع اسم ملف CSV بجانب هذا الملف واكتبه هنا بدل ""
CSV_FILE = ""  # مثال: "1120_daily.csv"

# ===========================================================================
# ٢) الاستيرادات (مكتبة بايثون القياسية فقط — لا حاجة لأي pip install)
# ===========================================================================

import argparse
import csv
import json
import random
import re
import sys
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Iterable
from typing import Iterator
from typing import Sequence


# ===========================================================================
# من saudi_data.py
# ===========================================================================

import csv
import json
import random
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Iterable
from typing import Iterator


RIYADH_TZ = timezone(timedelta(hours=3), "AST")

# The Tadawul continuous session closes at 15:00, with the closing auction and
# "trade at last" window running until ~15:20 AST.
SESSION_CLOSE = (15, 20)

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%m/%d/%Y",
    "%Y%m%d",
)

_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "datetime", "trade_date", "timestamp", "day", "التاريخ", "تاريخ"),
    "open": ("open", "open_price", "افتتاح", "الافتتاح"),
    "high": ("high", "high_price", "اعلى", "الأعلى"),
    "low": ("low", "low_price", "ادنى", "الأدنى"),
    "close": ("close", "close_price", "last", "إغلاق", "الاغلاق", "الإغلاق"),
    "volume": ("volume", "vol", "qty", "quantity", "الكمية", "كمية"),
}


@dataclass(frozen=True, slots=True)
class DailyBar:
    """A single daily OHLCV bar for a Tadawul instrument."""

    ts: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def as_row(self) -> list[object]:
        return [self.ts.isoformat(), self.open, self.high, self.low, self.close, self.volume]


@dataclass(frozen=True, slots=True)
class WeeklyBar:
    """One aggregated trading week (Sunday -> Thursday)."""

    week_start: date
    last_trading_day: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------


def week_start(d: date) -> date:
    """Return the Sunday that starts the Saudi trading week containing ``d``."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def is_trading_day(d: date) -> bool:
    """Return True for Sunday..Thursday (Friday/Saturday are the weekend)."""
    return d.weekday() != 4 and d.weekday() != 5


def trading_days(start: date, count: int) -> Iterator[date]:
    """Yield ``count`` consecutive trading days starting at ``start``."""
    current = start
    yielded = 0
    while yielded < count:
        if is_trading_day(current):
            yield current
            yielded += 1
        current += timedelta(days=1)


def week_close_timestamp(d: date) -> datetime:
    """Return the Riyadh-time close timestamp for the given trading day."""
    return datetime(d.year, d.month, d.day, SESSION_CLOSE[0], SESSION_CLOSE[1], tzinfo=RIYADH_TZ)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_date(value: str, date_format: str | None = None) -> date:
    """Parse a date string, trying the common Tadawul/Yahoo/GCC formats."""
    text = value.strip()
    if not text:
        raise ValueError("empty date value")
    # ISO datetimes such as '2026-01-04T10:00:00+03:00' are common in exports.
    if "T" in text or " " in text:
        text = text.replace("T", " ").split(" ")[0]
    formats = (date_format,) if date_format else _DATE_FORMATS
    for fmt in formats:
        if fmt is None:
            continue
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date format: {value!r}")


def _resolve_columns(header: Iterable[str]) -> dict[str, int]:
    normalized = {str(name).strip().lower(): idx for idx, name in enumerate(header)}
    resolved: dict[str, int] = {}
    for canonical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                resolved[canonical] = normalized[alias]
                break
    missing = [key for key in ("date", "open", "high", "low", "close") if key not in resolved]
    if missing:
        raise ValueError(
            f"CSV is missing required column(s): {missing}. "
            f"Found columns: {list(header)}",
        )
    return resolved


def load_csv(
    path: str | Path,
    *,
    date_format: str | None = None,
    delimiter: str | None = None,
) -> list[DailyBar]:
    """Load daily bars from a CSV file with flexible column names."""
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise ValueError(f"CSV file is empty: {file_path}")

    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","

    reader = csv.reader(text.splitlines(), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError(f"CSV file has no rows: {file_path}")

    header, body = rows[0], rows[1:]
    columns = _resolve_columns(header)

    by_date: dict[date, DailyBar] = {}
    for row in body:
        if len(row) <= max(columns.values()):
            continue
        try:
            ts = parse_date(row[columns["date"]], date_format)
            bar = DailyBar(
                ts=ts,
                open=float(row[columns["open"]]),
                high=float(row[columns["high"]]),
                low=float(row[columns["low"]]),
                close=float(row[columns["close"]]),
                volume=float(row[columns["volume"]]) if "volume" in columns else 0.0,
            )
        except (TypeError, ValueError):
            continue
        if bar.close <= 0:
            continue
        by_date[ts] = bar

    bars = [by_date[key] for key in sorted(by_date)]
    if not bars:
        raise ValueError(f"no valid bars parsed from: {file_path}")
    return bars


def save_csv(bars: Iterable[DailyBar], path: str | Path) -> Path:
    """Write daily bars to a simple ISO-dated CSV (useful for cached downloads)."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for bar in bars:
            writer.writerow(bar.as_row())
    return file_path


# ---------------------------------------------------------------------------
# Yahoo Finance
# ---------------------------------------------------------------------------


def yahoo_chart_url(
    symbol: str,
    *,
    start: date | None = None,
    end: date | None = None,
    interval: str = "1d",
) -> str:
    """Build a public Yahoo Finance chart endpoint URL."""
    base = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
    params: dict[str, str] = {"interval": interval, "includePrePost": "false", "events": "div,split"}
    if start is None and end is None:
        params["range"] = "ytd"
    else:
        start_dt = datetime.combine(start or date(1970, 1, 1), datetime.min.time(), RIYADH_TZ)
        end_dt = datetime.combine(end or date.today(), datetime.min.time(), RIYADH_TZ)
        params["period1"] = str(int(start_dt.timestamp()))
        params["period2"] = str(int(end_dt.timestamp()))
    return f"{base}?{urllib.parse.urlencode(params)}"


def parse_yahoo_payload(payload: dict) -> list[DailyBar]:
    """Parse a Yahoo Finance chart JSON payload into daily bars."""
    chart = payload.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        error = chart.get("error") or {}
        raise ValueError(f"Yahoo Finance returned no data: {error or payload}")
    result = results[0]
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quotes = indicators.get("quote") or [{}]
    quote = quotes[0]

    bars: dict[date, DailyBar] = {}
    for idx, ts in enumerate(timestamps):
        try:
            o = quote["open"][idx]
            h = quote["high"][idx]
            l = quote["low"][idx]  # noqa: E741 - mirrors the upstream field name
            c = quote["close"][idx]
        except (KeyError, IndexError, TypeError):
            continue
        if None in (o, h, l, c):
            continue
        try:
            volumes = quote.get("volume") or []
            volume = volumes[idx] if idx < len(volumes) and volumes[idx] is not None else 0
            bar = DailyBar(
                ts=datetime.fromtimestamp(int(ts), RIYADH_TZ).date(),
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=float(volume),
            )
        except (TypeError, ValueError):
            continue
        bars[bar.ts] = bar

    ordered = [bars[key] for key in sorted(bars)]
    if not ordered:
        raise ValueError("no valid bars in Yahoo Finance payload")
    return ordered


def load_yahoo(
    symbol: str,
    *,
    start: date | None = None,
    end: date | None = None,
    timeout: float = 30.0,
) -> list[DailyBar]:
    """Download daily bars for ``symbol`` (e.g. ``1120.SR``) from Yahoo Finance."""
    url = yahoo_chart_url(symbol, start=start, end=end)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https host
        payload = json.loads(response.read().decode("utf-8"))
    return parse_yahoo_payload(payload)


# ---------------------------------------------------------------------------
# Synthetic data (offline smoke tests only)
# ---------------------------------------------------------------------------


def synthetic_series(
    *,
    start: date = date(2025, 1, 5),
    num_days: int = 495,
    start_price: float = 55.0,
    seed: int = 7,
) -> list[DailyBar]:
    """Generate a deterministic fake 2025-2026 series with a crash and a rebound.

    The shape mimics the 2026 Saudi year (grind higher, violent drawdown from
    mid-2026, sharp November rebound) and starts in 2025 so that a 200-day
    moving average is available for the regime filter. The numbers are
    fabricated: never draw conclusions from this series.
    """
    rng = random.Random(seed)
    days = list(trading_days(start, num_days))
    price = start_price
    bars: list[DailyBar] = []

    for day in days:
        if day < date(2026, 1, 1):
            drift = 0.0006
        elif day < date(2026, 7, 1):
            drift = 0.0011
        elif day < date(2026, 10, 22):
            drift = -0.0032
        elif day < date(2026, 11, 3):
            drift = 0.0060
        else:
            drift = 0.0015

        shock = rng.gauss(0.0, 0.011)
        open_px = price * (1.0 + rng.gauss(0.0, 0.004))
        close_px = max(1.0, price * (1.0 + drift + shock))
        high_px = max(open_px, close_px) * (1.0 + abs(rng.gauss(0.0, 0.005)))
        low_px = min(open_px, close_px) * (1.0 - abs(rng.gauss(0.0, 0.005)))

        # Capitals show up on heavy down days: volume rises with the size of the move.
        move = abs(close_px / price - 1.0)
        volume = float(rng.randint(400_000, 3_000_000)) * (1.0 + 25.0 * move)

        bars.append(
            DailyBar(
                ts=day,
                open=round(open_px, 2),
                high=round(high_px, 2),
                low=round(low_px, 2),
                close=round(close_px, 2),
                volume=round(volume, 0),
            ),
        )
        price = close_px

    return bars


def synthetic_2026(**kwargs: object) -> list[DailyBar]:
    """Backwards-compatible alias for :func:`synthetic_series`."""
    return synthetic_series(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Weekly aggregation
# ---------------------------------------------------------------------------


def to_weekly(bars: Iterable[DailyBar]) -> list[WeeklyBar]:
    """Aggregate daily bars into Sunday->Thursday weekly bars."""
    weeks: dict[date, list[DailyBar]] = {}
    for bar in bars:
        weeks.setdefault(week_start(bar.ts), []).append(bar)

    weekly: list[WeeklyBar] = []
    for start_day in sorted(weeks):
        group = sorted(weeks[start_day], key=lambda bar: bar.ts)
        weekly.append(
            WeeklyBar(
                week_start=start_day,
                last_trading_day=group[-1].ts,
                open=group[0].open,
                high=max(bar.high for bar in group),
                low=min(bar.low for bar in group),
                close=group[-1].close,
                volume=sum(bar.volume for bar in group),
            ),
        )
    return weekly


def infer_price_increment_from_prices(prices: Iterable[float]) -> tuple[int, str]:
    """Infer a sensible (precision, increment) pair from observed prices.

    Tadawul uses price-banded tick sizes (0.01 / 0.05 / 0.10 / 0.20 / ...), so
    this is a heuristic used only to build a valid instrument definition.
    """
    increments = (0.01, 0.05, 0.10, 0.20, 0.25, 0.50, 1.0)
    observed = [round(float(price), 4) for price in prices]
    if not observed:
        return 2, "0.01"
    for increment in increments:
        if all(abs(round(price / increment) * increment - price) < 1e-6 for price in observed):
            return 2, f"{increment:.2f}"
    return 2, "0.01"


def infer_price_increment(bars: Iterable[DailyBar]) -> tuple[int, str]:
    """Infer a (precision, increment) pair from the closes of ``bars``."""
    return infer_price_increment_from_prices(bar.close for bar in bars)

# ===========================================================================
# من signals.py
# ===========================================================================

from typing import Sequence



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

# ===========================================================================
# من filters.py
# ===========================================================================

from collections import deque
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from typing import Sequence



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

# ===========================================================================
# من vector_engine.py
# ===========================================================================

from dataclasses import dataclass
from dataclasses import field
from datetime import date
from typing import Iterable
from typing import Sequence



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


def run_vector_backtest(
    weekly: Sequence[WeeklyBar],
    *,
    n_down: int = 3,
    hold_weeks: int = 4,
    stop_pct: float = 0.07,
    target_pct: float = 0.0,
    fee_rate: float = 0.00155,
    mask: object | None = None,
    verbose: bool = False,
) -> BacktestResult:
    """Run the weekly dip system over ``weekly`` bars.

    ``mask`` is an optional :class:`filters.SignalMask`; when supplied, only
    signals allowed by the context filters are traded.
    """
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
        if mask is not None and not mask.allowed[index]:
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

# ===========================================================================
# من run_backtest.py
# ===========================================================================

import argparse
import csv
import sys
from datetime import date
from pathlib import Path





def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Weekly dip backtest for Saudi equities (TASI / Tadawul).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", type=Path, help="Daily OHLCV CSV file")
    source.add_argument("--yahoo", metavar="SYMBOL", help="Yahoo Finance symbol, e.g. 1120.SR")
    source.add_argument("--demo", action="store_true", help="Use deterministic synthetic data")

    parser.add_argument("--symbol", default=None, help="Instrument symbol (default: inferred)")
    parser.add_argument("--venue", default="TADAWUL", help="NautilusTrader venue code")
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=None,
        help="Start date filter (defaults to 2026-01-01 for --yahoo, no filter otherwise)",
    )
    parser.add_argument(
        "--history-start",
        type=date.fromisoformat,
        default=None,
        help=(
            "How far back to download data for indicator warm-up "
            "(defaults to --start, or 2026-01-01 for --yahoo)"
        ),
    )
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--date-format", default=None, help="strptime format for CSV dates")

    rules = parser.add_argument_group("strategy")
    rules.add_argument("--n-down", type=int, default=3, help="Consecutive down weeks required")
    rules.add_argument("--hold-weeks", type=int, default=4, help="Weeks to hold")
    rules.add_argument("--stop", type=float, default=7.0, help="Stop-loss in %% (0 disables)")
    rules.add_argument("--target", type=float, default=0.0, help="Take-profit in %% (0 disables)")
    rules.add_argument("--fee-bps", type=float, default=15.5, help="Commission per side in bps")

    ctx = parser.add_argument_group("context filters")
    ctx.add_argument("--filter-sma", action="store_true", help="Only buy above the N-day SMA")
    ctx.add_argument("--sma-period", type=int, default=200, help="SMA period for the regime filter")
    ctx.add_argument(
        "--filter-volume",
        action="store_true",
        help="Only buy when the signal week's volume is elevated",
    )
    ctx.add_argument("--volume-mult", type=float, default=1.2, help="Volume ratio threshold")
    ctx.add_argument("--volume-lookback", type=int, default=20, help="Weeks for the volume average")
    ctx.add_argument("--index-csv", type=Path, default=None, help="Benchmark daily CSV (e.g. TASI)")
    ctx.add_argument(
        "--index-yahoo",
        metavar="SYMBOL",
        default=None,
        help="Benchmark Yahoo symbol (defaults to the traded instrument)",
    )
    ctx.add_argument(
        "--compare",
        action="store_true",
        help="Compare baseline vs SMA filter vs volume filter vs both",
    )

    engine = parser.add_argument_group("execution")
    engine.add_argument(
        "--mode",
        choices=("vector", "nautilus", "auto"),
        default="auto",
        help="Which engine(s) to run",
    )
    engine.add_argument("--balance", type=float, default=100_000.0, help="Starting cash (SAR)")
    engine.add_argument("--notional", type=float, default=25_000.0, help="SAR per trade")
    engine.add_argument("--export-trades", type=Path, default=None, help="Write trades to CSV")
    engine.add_argument("--save-data", type=Path, default=None, help="Cache loaded daily bars to CSV")
    engine.add_argument("--quiet", action="store_true", help="Hide the per-trade lines")
    engine.add_argument(
        "--sweep",
        action="store_true",
        help="Grid-search n_down x hold_weeks on the vector engine",
    )
    return parser


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_bars(args: argparse.Namespace) -> tuple[list[DailyBar], str, str]:
    if args.demo:
        bars = synthetic_series()
        return bars, "synthetic", args.symbol or "DEMO"
    if args.csv is not None:
        bars = load_csv(args.csv, date_format=args.date_format)
        return bars, str(args.csv), args.symbol or args.csv.stem.split("_")[0]
    history_start = args.history_start or args.start or date(2026, 1, 1)
    bars = load_yahoo(args.yahoo, start=history_start, end=args.end)
    return bars, f"yahoo:{args.yahoo}", args.symbol or args.yahoo.split(".")[0]


def load_benchmark(args: argparse.Namespace) -> tuple[list[DailyBar] | None, str]:
    if args.index_csv is not None:
        return load_csv(args.index_csv, date_format=args.date_format), str(args.index_csv)
    if args.index_yahoo:
        bars = load_yahoo(args.index_yahoo, start=date(2015, 1, 1), end=args.end)
        return bars, f"yahoo:{args.index_yahoo}"
    return None, "نفس السهم"


def filter_range(bars: list[DailyBar], start: date | None, end: date | None) -> list[DailyBar]:
    selected = bars
    if start is not None:
        selected = [bar for bar in selected if bar.ts >= start]
    if end is not None:
        selected = [bar for bar in selected if bar.ts <= end]
    return selected


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_header(
    *,
    symbol: str,
    source: str,
    daily: list[DailyBar],
    weekly: list[WeeklyBar],
    n_down: int,
) -> None:
    dip_signals = signals.signal_indices(weekly, n_down)
    first, last = daily[0], daily[-1]
    print("\n=== البيانات ===")
    print(f"  الرمز              : {symbol}")
    print(f"  المصدر             : {source}")
    print(f"  الفترة             : {first.ts} -> {last.ts}")
    print(f"  الأيام / الأسابيع   : {len(daily)} / {len(weekly)}")
    print(f"  أول إغلاق / آخره    : {first.close:.2f} / {last.close:.2f}")
    print(
        f"  أعلى / أدنى إغلاق   : "
        f"{max(b.close for b in daily):.2f} / {min(b.close for b in daily):.2f}",
    )
    print(f"  إشارات {n_down} أسابيع هابطة : {len(dip_signals)}")


def print_filters(
    config: filters.FilterConfig,
    mask: filters.SignalMask | None,
    *,
    benchmark_source: str,
) -> None:
    if not config.use_sma and not config.use_volume:
        print("\n=== الفلاتر ===\n  معطّلة (النسخة الأساسية)")
        return
    print("\n=== الفلاتر ===")
    if config.use_sma:
        print(f"  فلتر الاتجاه       : نعم (SMA {config.sma_period} يوم — مرجع: {benchmark_source})")
    if config.use_volume:
        print(
            f"  فلتر الحجم         : نعم (× {config.volume_mult:.2f} "
            f"على متوسط {config.volume_lookback} أسبوعًا)",
        )
    if mask is not None:
        print(f"  النتيجة            : {mask.describe()}")


def write_trades(path: Path, trades: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["entry_week", "exit_week", "entry_price", "exit_price", "return", "reason"])
        for trade in trades:
            if isinstance(trade, vector_engine.Trade):
                writer.writerow(
                    [
                        trade.entry_date,
                        trade.exit_date,
                        f"{trade.entry_price:.2f}",
                        f"{trade.exit_price:.2f}",
                        f"{trade.exit_price / trade.entry_price - 1.0:.6f}",
                        trade.exit_reason,
                    ],
                )
            else:
                writer.writerow(
                    [
                        trade["entry_week"],
                        trade["exit_week"],
                        f"{trade['entry_price']:.2f}",
                        f"{trade['exit_price']:.2f}",
                        f"{trade['exit_price'] / trade['entry_price'] - 1.0:.6f}",
                        trade["reason"],
                    ],
                )


def print_sweep(
    weekly: list[WeeklyBar],
    daily: list[DailyBar],
    *,
    config: filters.FilterConfig,
    benchmark_daily: list[DailyBar] | None,
    fee_rate: float,
    stop_pct: float,
    target_pct: float,
) -> None:
    """Print a small grid of (n_down, hold_weeks) results.

    The filter mask depends on ``n_down`` (which weeks count as signals), so it
    is rebuilt for every row.
    """
    use_filters = config.use_sma or config.use_volume
    print("\n=== مسح المعاملات (n_down x hold_weeks) — العائد التراكمي ===")
    header = "  n_down | " + " | ".join(f"hold {weeks}w" for weeks in range(2, 9))
    print(header)
    print("  " + "-" * (len(header) - 2))
    for n_down in range(1, 6):
        mask = None
        if use_filters:
            mask = filters.build_signal_mask(
                weekly,
                daily,
                n_down=n_down,
                config=config,
                benchmark_daily=benchmark_daily,
            )
        cells = []
        for hold_weeks in range(2, 9):
            if len(weekly) < n_down + hold_weeks + 2:
                cells.append("     n/a")
                continue
            result = vector_engine.run_vector_backtest(
                weekly,
                n_down=n_down,
                hold_weeks=hold_weeks,
                stop_pct=stop_pct,
                target_pct=target_pct,
                fee_rate=fee_rate,
                mask=mask,
            )
            stats = vector_engine.summarize(result)
            cells.append(f"{stats['cumulative_return'] * 100:7.2f}%")
        print(f"  {n_down:6d} | " + " | ".join(cells))
    print("  (الأرقام: العائد التراكمي بعد العمولات؛ الفلاتر المفعّلة مطبَّقة)")


def print_comparison(
    weekly: list[WeeklyBar],
    daily: list[DailyBar],
    *,
    benchmark_daily: list[DailyBar] | None,
    n_down: int,
    hold_weeks: int,
    stop_pct: float,
    target_pct: float,
    fee_rate: float,
    config: filters.FilterConfig,
) -> None:
    """Compare baseline vs each filter combination on the vector engine."""
    variants: list[tuple[str, filters.FilterConfig]] = [
        ("baseline", filters.FilterConfig()),
        (
            f"sma{config.sma_period}",
            filters.FilterConfig(use_sma=True, sma_period=config.sma_period),
        ),
        (
            f"volume>={config.volume_mult:.2f}x",
            filters.FilterConfig(
                use_volume=True,
                volume_mult=config.volume_mult,
                volume_lookback=config.volume_lookback,
            ),
        ),
        (
            "sma+volume",
            filters.FilterConfig(
                use_sma=True,
                sma_period=config.sma_period,
                use_volume=True,
                volume_mult=config.volume_mult,
                volume_lookback=config.volume_lookback,
            ),
        ),
    ]

    print("\n=== مقارنة الفلاتر (محرك المتجهات) ===")
    print(
        "  variant           trades  win%    avg%     cum%     maxDD%   exposure  blocked",
    )
    print("  " + "-" * 84)
    for name, variant in variants:
        mask = filters.build_signal_mask(
            weekly,
            daily,
            n_down=n_down,
            config=variant,
            benchmark_daily=benchmark_daily,
        )
        result = vector_engine.run_vector_backtest(
            weekly,
            n_down=n_down,
            hold_weeks=hold_weeks,
            stop_pct=stop_pct,
            target_pct=target_pct,
            fee_rate=fee_rate,
            mask=mask,
        )
        stats = vector_engine.summarize(result)
        print(
            f"  {name:<16}"
            f" {int(stats['trades']):>6d}"
            f" {stats['win_rate'] * 100:6.1f}"
            f" {stats['avg_return'] * 100:7.2f}"
            f" {stats['cumulative_return'] * 100:8.2f}"
            f" {stats['max_drawdown'] * 100:8.2f}"
            f" {stats['exposure'] * 100:8.1f}"
            f" {mask.blocked:>7d}",
        )
    print("  (blocked = إشارات منعتها الفلاتر)")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        bars_all, source, symbol = load_bars(args)
    except OSError as exc:
        print(
            "تعذّر تحميل البيانات من الإنترنت.\n"
            f"  التفاصيل: {exc}\n"
            "  تحقّق من الاتصال، أو استخدم ملف CSV محليًا بدل --yahoo.\n"
            "  تذكير: أسهم السوق السعودي تحتاج اللاحقة .SR مثل 1120.SR",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        print(f"تعذّر قراءة البيانات: {exc}", file=sys.stderr)
        return 1
    bars = filter_range(bars_all, args.start, args.end)
    if len(bars) < 10:
        print(f"Not enough daily bars after filtering ({len(bars)}).", file=sys.stderr)
        return 1

    if args.save_data is not None:
        print(f"Cached {len(bars)} daily bars -> {save_csv(bars, args.save_data)}")

    weekly = to_weekly(bars)
    if len(weekly) < args.n_down + args.hold_weeks + 1:
        print(
            f"Not enough weekly bars for n_down={args.n_down} and "
            f"hold_weeks={args.hold_weeks} ({len(weekly)} weeks).",
            file=sys.stderr,
        )
        return 1

    print_header(symbol=symbol, source=source, daily=bars, weekly=weekly, n_down=args.n_down)

    benchmark_daily, benchmark_source = (None, "نفس السهم")
    if args.index_csv is not None or args.index_yahoo:
        try:
            benchmark_daily, benchmark_source = load_benchmark(args)
        except (OSError, ValueError) as exc:
            print(f"[index] تعذّر تحميل المرجع: {exc}", file=sys.stderr)
            return 1

    # Indicators need history *before* the trading window; trade execution never does.
    warmup_bars = benchmark_daily if benchmark_daily is not None else bars_all

    filter_config = filters.FilterConfig(
        use_sma=args.filter_sma,
        sma_period=args.sma_period,
        use_volume=args.filter_volume,
        volume_mult=args.volume_mult,
        volume_lookback=args.volume_lookback,
    )
    mask = None
    if filter_config.use_sma or filter_config.use_volume:
        # The SMA needs history *before* the trading window, so fall back to the
        # unfiltered series for indicators (never for trade execution).
        mask = filters.build_signal_mask(
            weekly,
            bars,
            n_down=args.n_down,
            config=filter_config,
            benchmark_daily=warmup_bars,
        )
    print_filters(filter_config, mask, benchmark_source=benchmark_source)

    if filter_config.use_sma and mask is not None and mask.blocked_no_data:
        print(
            "  ملاحظة: جزء من الإشارات لم يجد تاريخًا كافيًا لمتوسط "
            f"{filter_config.sma_period} يومًا — وسّع البداية (--start 2024-01-01) "
            "أو استخدم --index-csv لتاريخ أطول.",
        )

    fee_rate = args.fee_bps / 10_000.0
    stop_pct = args.stop / 100.0
    target_pct = args.target / 100.0
    buy_hold = vector_engine.buy_and_hold_return(weekly, fee_rate=fee_rate)

    print("\n=== الصفقات (محرك المتجهات) ===")
    result = vector_engine.run_vector_backtest(
        weekly,
        n_down=args.n_down,
        hold_weeks=args.hold_weeks,
        stop_pct=stop_pct,
        target_pct=target_pct,
        fee_rate=fee_rate,
        mask=mask,
        verbose=not args.quiet,
    )
    stats = vector_engine.summarize(result, buy_hold=buy_hold)
    print(vector_engine.format_stats(stats, title="نتائج محرك المتجهات (Vector)"))

    if args.compare:
        print_comparison(
            weekly,
            bars,
            benchmark_daily=warmup_bars,
            n_down=args.n_down,
            hold_weeks=args.hold_weeks,
            stop_pct=stop_pct,
            target_pct=target_pct,
            fee_rate=fee_rate,
            config=filter_config,
        )

    if args.sweep:
        print_sweep(
            weekly,
            bars,
            config=filter_config,
            benchmark_daily=warmup_bars,
            fee_rate=fee_rate,
            stop_pct=stop_pct,
            target_pct=target_pct,
        )

    allowed = filters.allowed_weeks(weekly, mask) if mask is not None else None

    nautilus_trades = None
    if args.mode in ("nautilus", "auto"):
        try:
            raise ImportError("نسخة الملف الواحد لا تضم محرك Nautilus — استخدم --mode vector")
        except ImportError as exc:  # pragma: no cover
            print(f"\n[nautilus] import failed: {exc}", file=sys.stderr)
        else:
            print("\n=== الصفقات (NautilusTrader) ===")
            try:
                nautilus_trades = run_nautilus_backtest(
                    weekly,
                    symbol=symbol,
                    venue_code=args.venue,
                    starting_balance=args.balance,
                    notional=args.notional,
                    n_down=args.n_down,
                    hold_weeks=args.hold_weeks,
                    stop_pct=stop_pct,
                    target_pct=target_pct,
                    fee_rate=fee_rate,
                    allowed_weeks=allowed,
                    verbose=not args.quiet,
                )
            except RuntimeError as exc:
                if args.mode == "nautilus":
                    print(f"\n[nautilus] {exc}", file=sys.stderr)
                    return 2
                print(f"\n[nautilus] skipped: {str(exc).splitlines()[0]}")
            else:
                print(
                    f"\n  صفقات Nautilus: {len(nautilus_trades)} | "
                    f"صفقات المتجهات: {len(result.trades)}",
                )
                if len(nautilus_trades) != len(result.trades):
                    print("  ملاحظة: الفرق طبيعي عند حدود البيانات أو عند إغلاق المركز النهائي.")

    if args.export_trades is not None:
        trades_to_write = nautilus_trades if nautilus_trades else result.trades
        write_trades(args.export_trades, list(trades_to_write))
        print(f"  تم حفظ الصفقات -> {args.export_trades}")

    print(
        "\nتنبيه: النتائج تعليمية فقط — ليست توصية استثمارية، "
        "والباكتيست لا يضمن الأداء المستقبلي.\n",
    )
    return 0

# ===========================================================================
# جسر الأسماء: الكود الأصلي يستدعي الوحدات بأسمائها (filters.x، vector_engine.y ...)
# ===========================================================================

import types as _types


def _bridge(name, **members):
    module = _types.ModuleType(name)
    module.__dict__.update(members)
    sys.modules.setdefault(name, module)
    return module

saudi_data = _bridge("saudi_data", RIYADH_TZ=RIYADH_TZ, SESSION_CLOSE=SESSION_CLOSE, DailyBar=DailyBar, WeeklyBar=WeeklyBar, week_start=week_start, is_trading_day=is_trading_day, trading_days=trading_days, week_close_timestamp=week_close_timestamp, parse_date=parse_date, load_csv=load_csv, save_csv=save_csv, yahoo_chart_url=yahoo_chart_url, parse_yahoo_payload=parse_yahoo_payload, load_yahoo=load_yahoo, synthetic_series=synthetic_series, synthetic_2026=synthetic_2026, to_weekly=to_weekly, infer_price_increment_from_prices=infer_price_increment_from_prices, infer_price_increment=infer_price_increment)
signals = _bridge("signals", is_dip_signal=is_dip_signal, signal_indices=signal_indices, down_streak=down_streak)
filters = _bridge("filters", FilterConfig=FilterConfig, SignalMask=SignalMask, build_sma_by_date=build_sma_by_date, closes_by_date=closes_by_date, weekly_volume_ratio=weekly_volume_ratio, build_signal_mask=build_signal_mask, allowed_weeks=allowed_weeks)
vector_engine = _bridge("vector_engine", Trade=Trade, BacktestResult=BacktestResult, run_vector_backtest=run_vector_backtest, max_drawdown=max_drawdown, summarize=summarize, format_stats=format_stats, buy_and_hold_return=buy_and_hold_return)

# ===========================================================================
# ٣) دوال التشغيل المُبسّط (من phone_run.py)
# ===========================================================================

HERE = Path(__file__).resolve().parent

TRADES_FILE = HERE / "trades.csv"


def build_args() -> list[str]:
    argv: list[str] = []
    if CSV_FILE:
        argv.extend(["--csv", str(HERE / CSV_FILE), "--symbol", SYMBOL.split(".")[0]])
    else:
        argv.extend(["--yahoo", SYMBOL, "--symbol", SYMBOL.split(".")[0]])
    argv.extend(
        [
            "--start",
            START,
            "--history-start",
            HISTORY_START,
            "--n-down",
            str(N_DOWN),
            "--hold-weeks",
            str(HOLD_WEEKS),
            "--stop",
            str(STOP_PCT),
            "--target",
            str(TARGET_PCT),
            "--fee-bps",
            str(FEE_BPS),
            "--mode",
            "vector",
        ]
    )
    if COMPARE:
        argv.append("--compare")
    if SWEEP:
        argv.append("--sweep")
    if SAVE_TRADES:
        argv.extend(["--export-trades", str(TRADES_FILE)])
    return argv


def show_settings() -> None:
    source = f"ملف محلي: {CSV_FILE}" if CSV_FILE else f"Yahoo: {SYMBOL}"
    print("=" * 52)
    print(f"  المصدر      : {source}")
    print(f"  المتاجرة من : {START}   (بيانات من {HISTORY_START})")
    print(f"  القاعدة     : شراء بعد {N_DOWN} أسابيع هبوط، بيع بعد {HOLD_WEEKS} أسابيع")
    print(f"  الوقف       : {STOP_PCT}%   الهدف: {TARGET_PCT or 'بدون'}")
    print(f"  المقارنة    : {'نعم' if COMPARE else 'لا'}")
    print("=" * 52)
    print()


# ---------------------------------------------------------------------------
# ٤) التشغيل
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1:  # خيارات من الطرفية تتقدّم على الإعدادات أعلاه
        raise SystemExit(main(sys.argv[1:]))

    show_settings()
    try:
        code = main(build_args())
    except OSError as exc:  # no internet, DNS blocked, TLS rejected...
        print()
        print("تعذّر تحميل البيانات من الإنترنت:")
        print(f"  {exc}")
        print("الحل: تأكد من الاتصال، أو نزّل ملف CSV واكتب اسمه في CSV_FILE أعلى الملف.")
        raise SystemExit(1) from exc

    if code == 0 and SAVE_TRADES and TRADES_FILE.exists():
        print(f"تم حفظ الصفقات في: {TRADES_FILE}")
        print("(افتحه بـ Excel أو Google Sheets لترى كل صفقة)")

    raise SystemExit(code)
