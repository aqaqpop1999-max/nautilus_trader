#!/usr/bin/env python3
"""Deep dive on one Saudi symbol: trend, levels, volume, rules verdict (stdlib only).

Why this exists
---------------
``analytics.py`` gives one compact row per symbol for the whole watchlist. Before
you actually risk money on a single stock you want the opposite: everything about
*that* stock in one page. This script opens the hood on one symbol and writes a
self-contained HTML report you can read on a phone.

What it computes
----------------
* trend against the 200-day average (exact daily SMA-200, or the 40-week average
  when only weekly rows are available) and for how many weeks price stayed below
  that line, i.e. how close an upside break is;
* weekly momentum (RSI-14 on weekly closes), current up/down streak, week move;
* 52-week range, where the price sits inside it, distance from the high and low;
* price memory: closes the market keeps returning to, plus swing highs and lows
  from the weekly bars, split into resistance above and support below;
* volume behaviour: current volume against its 20/50-session average,
  up-versus-down volume over the last month, and volume-at-price as a proxy;
* the three rules from ``SKILLS.md`` checked one by one, the resulting verdict,
  and position sizing for 20k/50k/100k SAR at 2% risk with a 7% stop.

Examples
--------
    python focus_report.py                      # default: 2222.SR (أرامكو)
    python focus_report.py --yahoo 1120.SR      # any other symbol
    python focus_report.py --csv 2222_daily.csv
    python focus_report.py --weekly-csv w.csv   # rows are already weekly
    python focus_report.py --demo               # offline smoke test
    python focus_report.py --facts notes.txt    # append a fundamentals block
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from saudi_data import DailyBar
from saudi_data import WeeklyBar
from saudi_data import load_csv
from saudi_data import load_yahoo
from saudi_data import synthetic_series
from saudi_data import to_weekly

N_DOWN = 3
SMA_PERIOD = 200
WEEKLY_TREND = 40  # 40 weeks ~= 200 trading days
VOLUME_MIN = 1.2
RISK_PCT = 0.02
STOP_PCT = 0.07
CAPITALS = (20_000, 50_000, 100_000)

DEFAULT_SYMBOL = "2222.SR"
DEFAULT_NAME = "أرامكو"


# ---------------------------------------------------------------------------
# Small statistics helpers (no numpy: the phone has to run this too)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Level:
    """One horizontal reference price and what makes it interesting."""

    price: float
    label: str
    kind: str  # "res", "sup" or "ref"


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _vwap(bars: list[DailyBar], lookback: int) -> float | None:
    window = bars[-lookback:]
    total = sum(bar.volume for bar in window)
    if total <= 0:
        return None
    return sum(bar.close * bar.volume for bar in window) / total


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Simple (not Wilder-smoothed) RSI: good enough for a weekly read."""
    if len(closes) <= period:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for index in range(len(closes) - period, len(closes)):
        change = closes[index] - closes[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = statistics.fmean(gains)
    avg_loss = statistics.fmean(losses)
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _streak(closes: list[float], rising: bool) -> int:
    count = 0
    for index in range(len(closes) - 1, 0, -1):
        if rising and closes[index] > closes[index - 1]:
            count += 1
        elif not rising and closes[index] < closes[index - 1]:
            count += 1
        else:
            break
    return count


def _swing_levels(weeks: list[WeeklyBar], span: int = 3) -> tuple[list[float], list[float]]:
    """Weekly pivots: a bar whose high (low) is the extreme of its neighbourhood."""
    highs: list[float] = []
    lows: list[float] = []
    for index in range(span, len(weeks) - span):
        window = weeks[index - span : index + span + 1]
        if weeks[index].high >= max(bar.high for bar in window):
            highs.append(weeks[index].high)
        if weeks[index].low <= min(bar.low for bar in window):
            lows.append(weeks[index].low)
    return highs, lows


def _magnet_closes(closes: list[float], minimum: int = 3) -> list[tuple[float, int]]:
    """Prices the weekly close keeps landing on (rounding to 0.1 for cheap stocks)."""
    step = 0.1
    bins: dict[float, int] = {}
    for close in closes:
        level = round(round(close / step) * step, 2)
        bins[level] = bins.get(level, 0) + 1
    ranked = [(level, count) for level, count in bins.items() if count >= minimum]
    ranked.sort(key=lambda pair: -pair[1])
    return ranked[:5]


def _cluster(levels: list[Level], tolerance: float = 0.004) -> list[Level]:
    """Merge levels that sit within ``tolerance`` of each other, keeping the best label."""
    ordered = sorted(levels, key=lambda item: item.price)
    merged: list[Level] = []
    for level in ordered:
        if merged and abs(level.price / merged[-1].price - 1) <= tolerance:
            if merged[-1].kind == "ref" and level.kind != "ref":
                merged[-1] = level
            continue
        merged.append(level)
    return merged


# ---------------------------------------------------------------------------
# The analysis
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Focus:
    """Every number the one-symbol report shows."""

    code: str
    name: str
    basis: str  # "يومي" or "أسبوعي"
    generated: date
    weeks: list[WeeklyBar]
    bars: list[DailyBar] = field(default_factory=list)

    price: float = 0.0
    change_pct: float = 0.0
    week_change_pct: float = 0.0
    previous_close: float = 0.0

    ma50: float | None = None
    ma200: float | None = None
    ma10w: float | None = None
    ma20w: float | None = None
    ma40w: float | None = None
    ma80w: float | None = None
    trend: float | None = None
    trend_label: str = ""
    weeks_below: int = 0
    to_break: float | None = None
    to_break_pct: float | None = None
    weeks_memory: list[float] = field(default_factory=list)

    rsi_w: float | None = None
    up_streak: int = 0
    down_streak: int = 0

    high_52w: float = 0.0
    low_52w: float = 0.0
    from_high_pct: float = 0.0
    from_low_pct: float = 0.0
    range_pos: float = 0.0

    vwap20: float | None = None
    vwap60: float | None = None
    volume_last: float = 0.0
    volume_avg20: float = 0.0
    volume_avg50: float = 0.0
    volume_ratio: float | None = None
    up_down_ratio: float | None = None
    week_volume: float = 0.0
    week_volume_avg: float = 0.0

    avg_move_pct: float | None = None
    vol_week_pct: float | None = None
    vol_year_pct: float | None = None

    profile: list[tuple[float, float]] = field(default_factory=list)
    resistances: list[Level] = field(default_factory=list)
    supports: list[Level] = field(default_factory=list)

    rules: list[tuple[str, bool | None, str]] = field(default_factory=list)
    verdict: str = ""
    note: str = ""
    tone: str = "no"  # "ok" / "warn" / "no"

    def stop_price(self) -> float:
        return self.price * (1 - STOP_PCT)

    def sizing(self) -> list[tuple[int, float, float, int]]:
        rows = []
        for capital in CAPITALS:
            risk = capital * RISK_PCT
            size = risk / STOP_PCT
            rows.append((capital, risk, size, int(size / self.price)))
        return rows

    def reward_risk(self) -> float | None:
        if not self.high_52w or not self.price:
            return None
        upside = self.high_52w - self.price
        downside = self.price * STOP_PCT
        if downside <= 0 or upside <= 0:
            return None
        return upside / downside


def volume_profile(weeks: list[WeeklyBar], lookback: int = 60) -> list[tuple[float, float]]:
    """Volume-at-price proxy from weekly bars (not a real footprint chart)."""
    window = weeks[-lookback:]
    if not window:
        return []
    span = max(bar.high for bar in window) - min(bar.low for bar in window)
    if span <= 0:
        return []
    step = max(round(span / 18, 1), 0.1)
    bins: dict[float, float] = {}
    for bar in window:
        level = round(round(bar.close / step) * step, 2)
        bins[level] = bins.get(level, 0.0) + bar.volume
    total = sum(bins.values()) or 1.0
    ranked = sorted(bins.items(), key=lambda pair: -pair[1])[:8]
    return [(level, volume / total) for level, volume in sorted(ranked)]


def analyse(
    code: str,
    name: str,
    *,
    bars: list[DailyBar] | None = None,
    weeks: list[WeeklyBar] | None = None,
    today: date | None = None,
) -> Focus:
    """Compute everything for one symbol from daily bars, weekly bars, or both."""
    today = today or date.today()
    daily = sorted(bars, key=lambda bar: bar.ts) if bars else []
    weekly = list(weeks) if weeks else to_weekly(daily)
    if not weekly:
        raise ValueError("لا توجد بيانات كافية")

    basis = "يومي" if daily else "أسبوعي"
    result = Focus(code=code, name=name, basis=basis, generated=today, weeks=weekly, bars=daily)

    closes_w = [bar.close for bar in weekly]
    result.price = closes_w[-1]
    result.previous_close = closes_w[-2] if len(closes_w) > 1 else closes_w[-1]
    result.week_change_pct = (closes_w[-1] / closes_w[-2] - 1) * 100 if len(closes_w) > 1 else 0.0
    if daily:
        closes_d = [bar.close for bar in daily]
        if len(closes_d) > 1 and closes_d[-2]:
            result.change_pct = (closes_d[-1] / closes_d[-2] - 1) * 100
        result.ma50 = _mean(closes_d[-50:]) if len(closes_d) >= 50 else None
        result.ma200 = _mean(closes_d[-SMA_PERIOD:]) if len(closes_d) >= SMA_PERIOD else None
        result.vwap20 = _vwap(daily, 20)
        result.vwap60 = _vwap(daily, 60)
        result.volume_last = daily[-1].volume
        result.volume_avg20 = _mean([bar.volume for bar in daily[-21:-1]]) or 0.0
        result.volume_avg50 = _mean([bar.volume for bar in daily[-51:-1]]) or 0.0
        window = daily[-21:]
        up_volume = sum(
            window[i].volume
            for i in range(1, len(window))
            if window[i].close > window[i - 1].close
        )
        down_volume = sum(
            window[i].volume
            for i in range(1, len(window))
            if window[i].close < window[i - 1].close
        )
        result.up_down_ratio = up_volume / down_volume if down_volume else None
    else:
        result.change_pct = result.week_change_pct
        result.volume_last = weekly[-1].volume
        result.volume_avg20 = _mean([bar.volume for bar in weekly[-21:-1]]) or 0.0
        result.volume_avg50 = _mean([bar.volume for bar in weekly[-51:-1]]) or 0.0
        window = weekly[-21:]
        up_volume = sum(
            window[i].volume
            for i in range(1, len(window))
            if window[i].close > window[i - 1].close
        )
        down_volume = sum(
            window[i].volume
            for i in range(1, len(window))
            if window[i].close < window[i - 1].close
        )
        result.up_down_ratio = up_volume / down_volume if down_volume else None

    if result.volume_avg20:
        result.volume_ratio = result.volume_last / result.volume_avg20

    result.ma10w = _mean(closes_w[-10:]) if len(closes_w) >= 10 else None
    result.ma20w = _mean(closes_w[-20:]) if len(closes_w) >= 20 else None
    result.ma40w = _mean(closes_w[-WEEKLY_TREND:]) if len(closes_w) >= WEEKLY_TREND else None
    result.ma80w = _mean(closes_w[-80:]) if len(closes_w) >= 80 else None

    result.trend = result.ma200 if result.ma200 else result.ma40w
    if result.ma200:
        result.trend_label = f"متوسط {SMA_PERIOD} يوم"
    elif result.ma40w:
        result.trend_label = f"متوسط {WEEKLY_TREND} أسبوعًا"
    else:
        result.trend_label = "المتوسط الطويل"

    if result.trend:
        result.to_break = result.trend - result.price
        result.to_break_pct = (result.trend / result.price - 1) * 100
        # How many recent weeks closed below a rolling 40-week average.
        below = 0
        for index in range(len(closes_w) - 1, WEEKLY_TREND - 1, -1):
            window = closes_w[index - WEEKLY_TREND + 1 : index + 1]
            if closes_w[index] < statistics.fmean(window):
                below += 1
            else:
                break
        result.weeks_below = below

    result.rsi_w = _rsi(closes_w)
    result.up_streak = _streak(closes_w, rising=True)
    result.down_streak = _streak(closes_w, rising=False)

    window_w = weekly[-52:]
    result.high_52w = max(bar.high for bar in window_w)
    result.low_52w = min(bar.low for bar in window_w)
    result.from_high_pct = (result.price / result.high_52w - 1) * 100
    result.from_low_pct = (result.price / result.low_52w - 1) * 100
    span = result.high_52w - result.low_52w
    result.range_pos = (result.price - result.low_52w) / span * 100 if span else 50.0

    result.week_volume = weekly[-1].volume
    result.week_volume_avg = _mean([bar.volume for bar in weekly[-21:-1]]) or 0.0

    changes = [
        (closes_w[i] / closes_w[i - 1] - 1) * 100
        for i in range(len(closes_w) - 20, len(closes_w))
        if closes_w[i - 1]
    ]
    result.avg_move_pct = _mean([abs(value) for value in changes])
    result.vol_week_pct = statistics.pstdev(changes) if len(changes) > 1 else None
    if result.vol_week_pct:
        result.vol_year_pct = result.vol_week_pct * (52**0.5)

    result.profile = volume_profile(weekly)

    # --- levels -------------------------------------------------------------
    candidates: list[Level] = [
        Level(result.high_52w, "قمة 52 أسبوعًا", "ref"),
        Level(result.low_52w, "قاع 52 أسبوعًا", "ref"),
    ]
    for value, label in (
        (result.ma50, "متوسط 50 يوم"),
        (result.ma200, "متوسط 200 يوم"),
        (result.ma40w, "متوسط 40 أسبوعًا"),
        (result.vwap60, "VWAP 60 يوم"),
    ):
        if value:
            candidates.append(Level(value, label, "ref"))
    swing_highs, swing_lows = _swing_levels(weekly)
    for value in swing_highs[-6:]:
        candidates.append(Level(value, "قمة أسبوعية", "res"))
    for value in swing_lows[-6:]:
        candidates.append(Level(value, "قاع أسبوعي", "sup"))
    for value, count in _magnet_closes(closes_w):
        label = f"إغلاق تكرر {count} مرات"
        candidates.append(Level(value, label, "ref"))

    merged = _cluster(candidates)
    # A swing low above the price has already been broken: it now caps rallies.
    # Same the other way round. Relabel so the report does not read backwards.
    for level in merged:
        if level.price > result.price * 1.002 and level.kind == "sup":
            level.kind, level.label = "res", "دعم مكسور"
        elif level.price < result.price * 0.998 and level.kind == "res":
            level.kind, level.label = "sup", "قمة مكسورة"
    result.resistances = sorted(
        (level for level in merged if level.price > result.price * 1.002),
        key=lambda level: level.price,
    )[:3]
    result.supports = sorted(
        (level for level in merged if level.price < result.price * 0.998),
        key=lambda level: level.price,
        reverse=True,
    )[:3]
    result.weeks_memory = [value for value, _ in _magnet_closes(closes_w)]

    # --- the three rules ----------------------------------------------------
    above = bool(result.trend and result.price > result.trend)
    rule1 = f"فوق {result.trend_label}"
    rule2 = f"{N_DOWN} أسابيع هبوط متتالية"
    rule3 = f"حجم ≥ {VOLUME_MIN:.1f}× المتوسط"
    volume_ok: bool | None = None
    if result.volume_ratio is not None:
        volume_ok = result.volume_ratio >= VOLUME_MIN
    volume_note = f"{result.volume_ratio:.2f}×" if result.volume_ratio else "غير متاح"
    trend_gap = f"يبعد {(result.trend / result.price - 1) * 100:+.2f}%" if result.trend else "—"
    result.rules = [
        (rule1, above, trend_gap),
        (rule2, result.down_streak >= N_DOWN, f"الحالي {result.down_streak} من {N_DOWN}"),
        (rule3, volume_ok, volume_note),
    ]

    passed = sum(1 for _, ok, _ in result.rules if ok)
    if passed == 3:
        result.verdict = "إشارة كاملة"
        result.note = "اجتمعت القاعدة والفلتر — راجع الخطة قبل أي أمر"
        result.tone = "ok"
    elif not above:
        result.verdict = "لا إشارة — تحت الاتجاه"
        if result.weeks_below:
            result.note = (
                f"السهم تحت {result.trend_label} منذ {result.weeks_below} أسبوعًا، "
                f"ويحتاج {result.to_break_pct:.2f}% للاختراق"
            )
        else:
            result.note = f"السهم تحت {result.trend_label} — القاعدة تمنع الشراء"
        result.tone = "no"
    elif result.down_streak < N_DOWN:
        result.verdict = f"لا إشارة — هبوط {result.down_streak} من {N_DOWN}"
        result.note = "فوق الاتجاه لكن موجة الهبوط لم تكتمل"
        result.tone = "warn"
    else:
        result.verdict = "إشارة مرفوضة بالفلتر"
        result.note = "موجة هبوط مكتملة لكن حجم التأكيد ضعيف"
        result.tone = "warn"
    return result


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------


def format_console(item: Focus) -> str:
    move = (
        f"التغير {item.change_pct:+.2f}%"
        if item.basis == "يومي"
        else f"تغير الأسبوع {item.week_change_pct:+.2f}%"
    )
    lines = [
        f"===== {item.name} ({item.code}) — {item.generated.isoformat()} [{item.basis}] =====",
        f"السعر {item.price:.2f}   {move}"
        + (f"   الأسبوع {item.week_change_pct:+.2f}%" if item.basis == "يومي" else ""),
        f"{item.trend_label}: {item.trend:.2f}" if item.trend else "المتوسط الطويل: غير متاح",
        f"تحت الاتجاه منذ {item.weeks_below} أسبوعًا   للاختراق {item.to_break_pct:+.2f}%"
        if item.to_break_pct is not None
        else "",
        f"RSI أسبوعي {item.rsi_w:.1f}" if item.rsi_w else "",
        f"نطاق 52 أسبوعًا {item.low_52w:.2f} – {item.high_52w:.2f}   الموقع {item.range_pos:.0f}%",
        f"حجم آخر فترة {item.volume_last / 1e6:.1f}M = {item.volume_ratio:.2f}× متوسط 20"
        if item.volume_ratio
        else "",
        "",
        "المقاومات:",
    ]
    for level in item.resistances:
        lines.append(f"   {level.price:8.2f}  {level.label}")
    lines.append("السعر:      " + f"   {item.price:8.2f}")
    lines.append("الدعوم:")
    for level in item.supports:
        lines.append(f"   {level.price:8.2f}  {level.label}")
    lines.append("")
    lines.append("القواعد:")
    for label, ok, detail in item.rules:
        mark = "✅" if ok else ("❔" if ok is None else "❌")
        lines.append(f"   {mark} {label} ({detail})")
    lines.append("")
    lines.append(f"الحكم: {item.verdict} — {item.note}")
    lines.append("")
    lines.append(f"وقف 7%: {item.stop_price():.2f}")
    for capital, risk, size, shares in item.sizing():
        lines.append(
            f"   {capital:>7,} ريال → مخاطرة {risk:>6,.0f} → مركز {size:>8,.0f} → {shares:>5} سهم"
        )
    if item.reward_risk():
        lines.append(f"عائد/مخاطرة حتى قمة 52 أسبوعًا: {item.reward_risk():.1f} : 1")
    return "\n".join(line for line in lines if line != "")


# ---------------------------------------------------------------------------
# SVG charts (inline, no external assets)
# ---------------------------------------------------------------------------


def _weekly_chart(item: Focus, width: int = 520, height: int = 220) -> str:
    weeks = item.weeks[-110:]
    if len(weeks) < 5:
        return ""
    closes = [bar.close for bar in weeks]
    volumes = [bar.volume for bar in weeks]
    offset = len(item.weeks) - len(weeks)

    ma_series: list[float | None] = [None] * len(closes)
    for index in range(len(closes)):
        end = offset + index + 1
        start = end - WEEKLY_TREND
        if start >= 0:
            window = [bar.close for bar in item.weeks[start:end]]
            ma_series[index] = statistics.fmean(window)

    known = [value for value in ma_series if value is not None]
    low = min(closes + known)
    high = max(closes + known)
    span = (high - low) or 1.0
    max_volume = max(volumes) or 1.0

    pad_l, pad_r, pad_t, pad_b = 6, 56, 12, 40
    plot_h = height - pad_t - pad_b

    def x_of(index: int) -> float:
        return pad_l + index * (width - pad_l - pad_r) / max(1, len(closes) - 1)

    def y_of(value: float) -> float:
        return pad_t + (high - value) / span * (plot_h - 6)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#0b0f14" rx="10"/>',
    ]
    for index, volume in enumerate(volumes):
        bar_h = (volume / max_volume) * 30
        colour = "#2a3a52" if index == 0 or closes[index] >= closes[index - 1] else "#3a2a32"
        parts.append(
            f'<rect x="{x_of(index) - 1.3:.1f}" y="{height - pad_b - bar_h:.1f}" '
            f'width="2.6" height="{bar_h:.1f}" fill="{colour}"/>'
        )

    ma_points = " ".join(
        f"{x_of(i):.1f},{y_of(value):.1f}" for i, value in enumerate(ma_series) if value is not None
    )
    if ma_points:
        parts.append(
            f'<polyline points="{ma_points}" fill="none" stroke="#f0a05a" stroke-width="1.4" '
            f'stroke-dasharray="5 3"/>'
        )
    points = " ".join(f"{x_of(i):.1f},{y_of(value):.1f}" for i, value in enumerate(closes))
    parts.append(f'<polyline points="{points}" fill="none" stroke="#8ab4f8" stroke-width="1.8"/>')

    last_y = y_of(closes[-1])
    parts.append(
        f'<line x1="{x_of(len(closes) - 1):.1f}" y1="{last_y:.1f}" x2="{width - pad_r:.1f}" '
        f'y2="{last_y:.1f}" stroke="#8ab4f8" stroke-width="0.8" stroke-dasharray="2 2"/>'
    )
    parts.append(
        f'<text x="{width - pad_r + 4}" y="{last_y + 4:.1f}" fill="#e8eaed" font-size="11" '
        f'direction="ltr">{closes[-1]:.2f}</text>'
    )
    if item.trend:
        trend_y = y_of(item.trend)
        parts.append(
            f'<line x1="{pad_l}" y1="{trend_y:.1f}" x2="{width - pad_r:.1f}" y2="{trend_y:.1f}" '
            f'stroke="#f0a05a" stroke-width="0.8" stroke-dasharray="1 3"/>'
        )
        parts.append(
            f'<text x="{width - pad_r + 4}" y="{trend_y + 4:.1f}" fill="#f0a05a" font-size="11" '
            f'direction="ltr">{item.trend:.2f}</text>'
        )
    parts.append(
        f'<text x="{pad_l}" y="{height - 8}" fill="#7c8288" font-size="10" direction="ltr">'
        f"{weeks[0].week_start.isoformat()} → {item.weeks[-1].last_trading_day.isoformat()}"
        "  · أسبوعي</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


def _ladder(item: Focus, width: int = 520, height: int = 300) -> str:
    """Vertical price ruler: resistance above, price in the middle, support below."""
    entries: list[Level] = list(item.resistances) + [
        Level(item.price, "السعر الآن", "price"),
    ] + list(item.supports)
    if not entries:
        return ""
    values = [entry.price for entry in entries]
    low, high = min(values), max(values)
    padding = (high - low) * 0.18 or high * 0.02
    low -= padding
    high += padding
    span = (high - low) or 1.0
    pad_t, pad_b = 16, 16
    plot_h = height - pad_t - pad_b

    def y_of(value: float) -> float:
        return pad_t + (high - value) / span * plot_h

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#0b0f14" rx="10"/>',
        f'<line x1="210" y1="{pad_t}" x2="210" y2="{height - pad_b}" stroke="#20252e" '
        f'stroke-width="2"/>',
    ]
    last_y = -99.0
    for entry in sorted(entries, key=lambda level: -level.price):
        y = y_of(entry.price)
        if abs(y - last_y) < 20:
            continue
        last_y = y
        if entry.kind == "price":
            colour = "#e8eaed"
            dash = ""
            weight = "2.4"
        elif entry.kind == "res":
            colour = "#f0a05a"
            dash = ""
            weight = "1.6"
        elif entry.kind == "sup":
            colour = "#8ab4f8"
            dash = ""
            weight = "1.6"
        else:
            colour = "#7c8288"
            dash = ' stroke-dasharray="4 3"'
            weight = "1.2"
        parts.append(
            f'<line x1="150" y1="{y:.1f}" x2="300" y2="{y:.1f}" stroke="{colour}" '
            f'stroke-width="{weight}"{dash}/>'
        )
        parts.append(
            f'<text x="304" y="{y + 4:.1f}" fill="{colour}" font-size="12" direction="ltr">'
            f"{entry.price:.2f}</text>"
        )
        parts.append(
            f'<text x="146" y="{y + 4:.1f}" fill="{colour}" font-size="11" text-anchor="end">'
            f"{entry.label}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _profile_bars(item: Focus) -> str:
    if not item.profile:
        return ""
    peak = max(share for _, share in item.profile) or 1.0
    rows = []
    for level, share in sorted(item.profile, key=lambda pair: -pair[0]):
        width = int(share / peak * 100)
        colour = "#34a853" if level >= item.price else "#5f6368"
        rows.append(
            '<div style="display:flex;align-items:center;gap:6px;margin:2px 0;font-size:11px;'
            'color:#9aa0a6;direction:ltr">'
            f'<span style="width:38px;text-align:right">{level:.2f}</span>'
            '<span style="flex:1;background:#20252e;border-radius:3px;height:8px">'
            f'<span style="display:block;width:{width}%;height:8px;background:{colour};'
            'border-radius:3px"></span></span>'
            f'<span style="width:34px">{share * 100:.1f}%</span></div>'
        )
    return "".join(rows)


def _kv_rows(item: Focus) -> str:
    def fmt(value: float | None, suffix: str = "") -> str:
        return f"{value:.2f}{suffix}" if value is not None else "—"

    trend_gap = (item.price / item.trend - 1) * 100 if item.trend else None
    rows = [
        ("متوسط 10 أسابيع", fmt(item.ma10w)),
        ("متوسط 20 أسبوعًا", fmt(item.ma20w)),
        ("متوسط 40 أسبوعًا", fmt(item.ma40w)),
        ("متوسط 200 يوم (يومي)", fmt(item.ma200)),
        ("متوسط 50 يوم (يومي)", fmt(item.ma50)),
        (f"البعد عن {item.trend_label}", f"{trend_gap:+.2f}%" if trend_gap is not None else "—"),
        ("أسابيع تحت الاتجاه", str(item.weeks_below)),
        ("VWAP 60 يوم", fmt(item.vwap60)),
        ("RSI أسبوعي (14)", fmt(item.rsi_w)),
        ("أعلى/أدنى 52 أسبوعًا", f"{item.low_52w:.2f} – {item.high_52w:.2f}"),
        ("من القمة / من القاع", f"{item.from_high_pct:+.1f}% / {item.from_low_pct:+.1f}%"),
        ("الموقع في النطاق", f"{item.range_pos:.0f}%"),
        ("التقلب الأسبوعي", f"{item.vol_week_pct:.2f}%" if item.vol_week_pct else "—"),
        ("التقلب السنوي (تقديري)", f"{item.vol_year_pct:.0f}%" if item.vol_year_pct else "—"),
        ("الحجم / المتوسط", f"{item.volume_ratio:.2f}×" if item.volume_ratio else "—"),
        ("حجم الصعود ÷ الهبوط", f"{item.up_down_ratio:.2f}" if item.up_down_ratio else "—"),
    ]
    return "".join(
        f"<tr><td class='dim'>{label}</td><td>{value}</td></tr>" for label, value in rows
    )


def _rules_rows(item: Focus) -> str:
    rows = []
    for label, ok, detail in item.rules:
        mark = "✅" if ok else ("❔" if ok is None else "❌")
        cls = "ok" if ok else ("dim" if ok is None else "bad")
        rows.append(
            f"<tr><td class='{cls}'>{mark} {label}</td>"
            f"<td class='dim' style='text-align:left'>{detail}</td></tr>"
        )
    return "".join(rows)


def _week_rows(item: Focus) -> str:
    weeks = item.weeks[-10:]
    start = len(item.weeks) - len(weeks)
    rows = []
    for index, week in enumerate(weeks):
        absolute = start + index
        previous = item.weeks[absolute - 1].close if absolute else week.close
        change = (week.close / previous - 1) * 100 if previous else 0.0
        colour = "up" if change >= 0 else "down"
        rows.append(
            f"<tr><td class='dim'>{week.week_start.isoformat()}</td>"
            f"<td>{week.close:.2f}</td>"
            f"<td class='{colour}'>{change:+.2f}%</td>"
            f"<td class='dim'>{week.volume:,.0f}</td></tr>"
        )
    return "".join(rows)


def _sizing_rows(item: Focus) -> str:
    return "".join(
        f"<tr><td class='dim'>{capital:,}</td><td>{risk:,.0f}</td>"
        f"<td>{size:,.0f}</td><td>{shares:,}</td></tr>"
        for capital, risk, size, shares in item.sizing()
    )


def _facts_block(text: str) -> str:
    if not text.strip():
        return ""
    parts: list[str] = ['<div class="card"><h2>ملاحظات أساسية</h2>']
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts.append(f"<h3>{line.lstrip('# ').strip()}</h3>")
        elif line.startswith(("- ", "* ")):
            parts.append(f"<li>{line[2:].strip()}</li>")
        else:
            parts.append(f"<p>{line}</p>")
    parts.append("</div>")
    return "".join(parts)


CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: #0e1116; color: #e8eaed;
       font-family: system-ui, -apple-system, "Segoe UI", Tahoma, sans-serif;
       direction: rtl; }
.wrap { max-width: 560px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 0 0 10px; color: #c9ced6; font-weight: 600; }
h3 { font-size: 13px; margin: 12px 0 4px; color: #9aa0a6; font-weight: 600; }
p { font-size: 13px; line-height: 1.8; margin: 0 0 8px; color: #c9ced6; }
li { font-size: 13px; line-height: 1.8; color: #c9ced6; }
.sub { color: #9aa0a6; font-size: 13px; margin-bottom: 14px; }
.card { background: #171b22; border-radius: 16px; padding: 14px; margin-bottom: 14px; }
.head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.name { font-size: 18px; font-weight: 600; }
.px { font-size: 20px; font-variant-numeric: tabular-nums; }
.up { color: #34a853; } .down { color: #ea4335; } .ok { color: #8ff0b0; }
.bad { color: #f0a0a0; } .dim { color: #9aa0a6; font-size: 13px; }
table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13px; }
td { padding: 3px 0; }
td:last-child { text-align: left; font-variant-numeric: tabular-nums; }
.badge { display: inline-block; padding: 4px 12px; border-radius: 999px; font-size: 13px; }
.b-ok { background: #1e5c34; color: #8ff0b0; }
.b-warn { background: #5c4a1e; color: #f0dc8f; }
.b-no { background: #3a1f22; color: #f0a0a0; }
.tiles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 14px; }
.tile { background: #171b22; border-radius: 12px; padding: 10px; text-align: center; }
.tile b { display: block; font-size: 16px; font-variant-numeric: tabular-nums; }
.tile span { font-size: 11px; color: #9aa0a6; }
footer { color: #7c8288; font-size: 11px; line-height: 1.8; margin: 18px 0 8px; }
"""


def render_html(item: Focus, facts: str = "") -> str:
    badge = {"ok": "b-ok", "warn": "b-warn"}.get(item.tone, "b-no")
    change_class = "up" if item.change_pct >= 0 else "down"
    trend_gap = (item.price / item.trend - 1) * 100 if item.trend else 0.0
    rr = item.reward_risk()
    tiles = [
        ("السعر", f"{item.price:.2f}", f"{item.change_pct:+.2f}% اليوم"),
        (item.trend_label, f"{item.trend:.2f}" if item.trend else "—", f"{trend_gap:+.2f}%"),
        ("أسابيع تحت الاتجاه", str(item.weeks_below), "كلما قلّ اقترب الاختراق"),
        ("RSI أسبوعي", f"{item.rsi_w:.1f}" if item.rsi_w else "—", "50 = محايد"),
        ("الموقع في نطاق السنة", f"{item.range_pos:.0f}%", "0 = القاع · 100 = القمة"),
        ("الحجم / المتوسط", f"{item.volume_ratio:.2f}×" if item.volume_ratio else "—", "1.2× = تأكيد"),
    ]
    tile_html = "".join(
        f'<div class="tile"><b>{value}</b><span>{label}</span>'
        f'<span style="display:block">{hint}</span></div>'
        for label, value, hint in tiles
    )
    return f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{item.name} {item.code} — تحليل مفصّل</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>{item.name} · تحليل مفصّل</h1>
  <div class="sub">{item.code} · {item.generated.isoformat()} · بيانات {item.basis}</div>

  <div class="card">
    <div class="head">
      <div>
        <div class="name">{item.name}</div>
        <div class="dim">{item.code}</div>
      </div>
      <div style="text-align:left">
        <div class="px">{item.price:.2f}</div>
        <div class="{change_class}" style="font-size:13px">{item.change_pct:+.2f}%</div>
      </div>
    </div>
    <span class="badge {badge}">{item.verdict}</span>
    <div class="dim" style="margin-top:8px;line-height:1.7">{item.note}</div>
  </div>

  <div class="tiles">{tile_html}</div>

  <div class="card">
    <h2>الاتجاه الأسبوعي والمتوسط الطويل</h2>
    {_weekly_chart(item)}
    <div class="dim" style="margin-top:6px">
      أزرق: الإغلاق الأسبوعي · برتقالي متقطع: {item.trend_label}
    </div>
  </div>

  <div class="card">
    <h2>المستويات</h2>
    {_ladder(item)}
  </div>

  <div class="card">
    <h2>القواعد الثلاث</h2>
    <table>{_rules_rows(item)}</table>
  </div>

  <div class="card">
    <h2>الأرقام</h2>
    <table>{_kv_rows(item)}</table>
  </div>

  <div class="card">
    <h2>تكدّس الحجم (تقريبي)</h2>
    {_profile_bars(item)}
    <div class="dim" style="margin-top:8px">
      تقريب مبني على إغلاق الأسبوع وحجمه، وليس فوتبرنت حقيقيًا.
    </div>
  </div>

  <div class="card">
    <h2>آخر 10 أسابيع</h2>
    <table>{_week_rows(item)}</table>
  </div>

  <div class="card">
    <h2>لو دخلت (وقف {STOP_PCT:.0%} · مخاطرة {RISK_PCT:.0%})</h2>
    <div class="dim" style="margin-bottom:6px">الوقف عند {item.stop_price():.2f}</div>
    <table>
      <tr><td class="dim">رأس المال</td><td>مخاطرة</td><td>أقصى مركز</td><td>أسهم</td></tr>
      {_sizing_rows(item)}
    </table>
    <div class="dim" style="margin-top:8px">
      {"عائد/مخاطرة حتى قمة 52 أسبوعًا: " + f"{rr:.1f} : 1" if rr else ""}
    </div>
  </div>

  {_facts_block(facts)}

  <footer>
    المصدر: Yahoo Finance للأرقام اليومية/الأسبوعية. مقاييس فقط — ليست توصية استثمارية،
    والقرار والتنفيذ لك وحدك.<br>
    توليد آلي بواسطة focus_report.py · القاعدة: {N_DOWN} أسابيع هبوط + فوق المتوسط + حجم {VOLUME_MIN}×
  </footer>
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="تحليل مفصّل لسهم واحد مع تقرير HTML (بلا مكتبات خارجية).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--yahoo", default=None, metavar="SYMBOL", help="رمز Yahoo، مثال 2222.SR (افتراضي)"
    )
    parser.add_argument("--csv", type=Path, default=None, help="ملف CSV يومي")
    parser.add_argument(
        "--weekly-csv", type=Path, default=None, help="ملف CSV يحتوي صفًا لكل أسبوع"
    )
    parser.add_argument("--symbol", default=None, help="اسم يظهر في التقرير")
    parser.add_argument("--name", default=None, help="الاسم العربي للسهم")
    parser.add_argument("--date-format", default=None, help="صيغة التاريخ داخل CSV")
    parser.add_argument("--demo", action="store_true", help="بيانات صناعية بلا إنترنت")
    parser.add_argument("--days", type=int, default=760, help="أيام التاريخ المطلوبة")
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="تاريخ التقرير (افتراضي: اليوم) — مفيد لإعادة إنتاج لقطة قديمة",
    )
    parser.add_argument("--out", type=Path, default=Path("focus_report.html"), help="ملف HTML")
    parser.add_argument("--facts", type=Path, default=None, help="ملف نصي لملاحظات أساسية")
    parser.add_argument("--no-html", action="store_true", help="اطبع فقط بدون HTML")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    symbol = args.yahoo or DEFAULT_SYMBOL
    code = args.symbol or symbol.split(".")[0]
    name = args.name or (DEFAULT_NAME if code == "2222" else code)

    bars: list[DailyBar] | None = None
    weeks: list[WeeklyBar] | None = None
    try:
        if args.demo:
            bars = synthetic_series()
        elif args.weekly_csv:
            weeks = to_weekly(load_csv(args.weekly_csv, date_format=args.date_format))
        elif args.csv:
            bars = load_csv(args.csv, date_format=args.date_format)
        else:
            start = date.today() - timedelta(days=args.days)
            bars = load_yahoo(symbol, start=start)
    except (OSError, ValueError) as exc:
        print(f"تعذّر تحميل البيانات: {exc}", file=sys.stderr)
        print("تحقّق من الاتصال، أو استخدم --demo أو --csv.", file=sys.stderr)
        return 1

    if not bars and not weeks:
        print("لا توجد بيانات.", file=sys.stderr)
        return 1

    try:
        item = analyse(code, name, bars=bars, weeks=weeks, today=args.as_of)
    except ValueError as exc:
        print(f"تعذّر التحليل: {exc}", file=sys.stderr)
        return 1
    print(format_console(item))

    if not args.no_html:
        facts = ""
        if args.facts and args.facts.exists():
            facts = args.facts.read_text(encoding="utf-8")
        args.out.write_text(render_html(item, facts), encoding="utf-8")
        print(f"\nتم إنشاء التقرير: {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
