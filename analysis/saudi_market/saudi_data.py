"""Data preparation helpers for Saudi Exchange (Tadawul) weekly-dip backtests.

This module is intentionally standard-library only, so the data pipeline can be
imported and tested without the ``nautilus_trader`` package (which requires
Python >= 3.13 for the v2 Rust core).

Supported inputs
----------------
* ``load_csv``   : a daily OHLCV CSV (Tadawul export, Argaam export, Yahoo export...).
* ``load_yahoo`` : daily bars pulled from the public Yahoo Finance chart endpoint.
* ``synthetic``  : a deterministic fake 2026 series, used for offline smoke tests.

Everything is resampled to the Saudi trading week (Sunday -> Thursday).
"""

from __future__ import annotations

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
