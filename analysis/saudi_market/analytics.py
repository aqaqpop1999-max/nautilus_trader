#!/usr/bin/env python3
"""أدوات تحليل شخصية للسوق السعودي: مقاييس + تقرير HTML برسم بياني (بلا مكتبات خارجية).

What it does
------------
For each symbol it downloads daily bars from Yahoo Finance (or reads a CSV, or
uses the synthetic series), then computes the numbers that actually drive a
decision: trend versus the 200-day average, volume-weighted prices, where the
volume is concentrated, whether the recent bounce came with real volume, and
what the dip rule in ``SKILLS.md`` says today.

The result is printed as an Arabic table and written to a self-contained HTML
page (inline SVG charts, no JavaScript, no matplotlib), so it opens in Chrome
on a phone.

Examples
--------
    python analytics.py --demo                      # offline smoke test
    python analytics.py                             # the six watchlist symbols
    python analytics.py --yahoo 1120.SR --yahoo 2010.SR
    python analytics.py --csv data/1120_daily.csv --symbol 1120
    python analytics.py --out report.html --no-html  # table only
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

from filters import FilterConfig
from filters import build_signal_mask
from saudi_data import DailyBar
from saudi_data import load_csv
from saudi_data import load_yahoo
from saudi_data import synthetic_series
from saudi_data import to_weekly
from signals import is_dip_signal

WATCHLIST: tuple[tuple[str, str], ...] = (
    ("1120.SR", "الراجحي"),
    ("1180.SR", "الأهلي"),
    ("1010.SR", "الرياض"),
    ("2010.SR", "سابك"),
    ("2222.SR", "أرامكو"),
    ("7010.SR", "stc"),
)

N_DOWN = 3
SMA_PERIOD = 200


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Analysis:
    """Every number the report shows for one symbol."""

    code: str
    name: str
    bars: list[DailyBar]

    price: float = 0.0
    change_pct: float = 0.0
    ma50: float | None = None
    ma200: float | None = None
    vwap20: float | None = None
    vwap60: float | None = None
    high_52w: float = 0.0
    low_52w: float = 0.0
    from_high_pct: float = 0.0
    from_low_pct: float = 0.0
    volume_last: float = 0.0
    volume_avg50: float = 0.0
    up_down_ratio: float | None = None
    profile: list[tuple[float, float]] = field(default_factory=list)
    bounce_volume_ratio: float | None = None
    week_in_progress: bool = False
    down_streak: int = 0
    signal_now: bool = False
    signal_blocked_regime: bool = False
    verdict: str = ""
    note: str = ""


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _vwap(bars: list[DailyBar], lookback: int) -> float | None:
    window = bars[-lookback:]
    total_volume = sum(bar.volume for bar in window)
    if total_volume <= 0:
        return None
    return sum(bar.close * bar.volume for bar in window) / total_volume


def volume_profile(
    bars: list[DailyBar], lookback: int = 120, bin_size: float = 0.5
) -> list[tuple[float, float]]:
    """Approximate a volume profile by binning each close and weighting by volume.

    This is not a footprint chart (that needs per-trade bid/ask data, which
    Tadawul does not publish), but it answers the same question: at which prices
    did the market actually change hands.
    """
    window = bars[-lookback:]
    bins: dict[float, float] = {}
    for bar in window:
        level = round(bar.close / bin_size) * bin_size
        bins[level] = bins.get(level, 0.0) + bar.volume
    total = sum(bins.values()) or 1.0
    ranked = sorted(bins.items(), key=lambda item: -item[1])[:8]
    return [(level, volume / total) for level, volume in sorted(ranked)]


def _streak(weeks: list, rising: bool) -> int:
    count = 0
    for index in range(len(weeks) - 1, 0, -1):
        if rising and weeks[index].close > weeks[index - 1].close:
            count += 1
        elif not rising and weeks[index].close < weeks[index - 1].close:
            count += 1
        else:
            break
    return count


def analyse(code: str, name: str, bars: list[DailyBar], today: date | None = None) -> Analysis:
    today = today or date.today()
    bars = sorted(bars, key=lambda bar: bar.ts)
    closes = [bar.close for bar in bars]
    weeks = to_weekly(bars)

    result = Analysis(code=code, name=name, bars=bars)
    result.price = closes[-1]
    if len(closes) > 1 and closes[-2]:
        result.change_pct = (closes[-1] / closes[-2] - 1) * 100

    result.ma50 = _mean(closes[-50:]) if len(closes) >= 50 else None
    result.ma200 = _mean(closes[-SMA_PERIOD:]) if len(closes) >= SMA_PERIOD else None
    result.vwap20 = _vwap(bars, 20)
    result.vwap60 = _vwap(bars, 60)

    window = bars[-250:]
    result.high_52w = max(bar.high for bar in window)
    result.low_52w = min(bar.low for bar in window)
    result.from_high_pct = (result.price / result.high_52w - 1) * 100
    result.from_low_pct = (result.price / result.low_52w - 1) * 100

    result.volume_last = bars[-1].volume
    result.volume_avg50 = _mean([bar.volume for bar in bars[-51:-1]]) or 0.0

    window = bars[-21:]
    up_volume = sum(
        window[i].volume for i in range(1, len(window)) if window[i].close > window[i - 1].close
    )
    down_volume = sum(
        window[i].volume for i in range(1, len(window)) if window[i].close < window[i - 1].close
    )
    result.up_down_ratio = up_volume / down_volume if down_volume else None

    result.profile = volume_profile(bars)

    last10 = _mean([bar.volume for bar in bars[-10:]]) or 0.0
    prev20 = _mean([bar.volume for bar in bars[-30:-10]]) or 0.0
    result.bounce_volume_ratio = last10 / prev20 if prev20 else None

    if weeks:
        last_week_end = weeks[-1].week_start + timedelta(days=4)
        result.week_in_progress = today <= last_week_end and today >= weeks[-1].week_start

    completed = weeks[:-1] if result.week_in_progress and len(weeks) > 1 else weeks
    result.down_streak = _streak(completed, rising=False)

    if len(completed) > 1:
        mask = build_signal_mask(
            completed,
            bars,
            n_down=N_DOWN,
            config=FilterConfig(use_sma=True, sma_period=SMA_PERIOD, use_volume=False),
        )
        last_index = len(completed) - 1
        result.signal_now = is_dip_signal(completed, last_index, N_DOWN)
        if result.signal_now:
            allowed = mask.allowed[last_index] if mask.allowed else False
            result.signal_blocked_regime = not allowed

    above = result.ma200 is not None and result.price > result.ma200
    if result.signal_now and not result.signal_blocked_regime:
        result.verdict = "إشارة كاملة"
        result.note = "اجتمعت القاعدة والفلتر — راجع الخطة قبل أي أمر"
    elif result.signal_now:
        result.verdict = "إشارة مرفوضة بالفلتر"
        result.note = "3 أسابيع هبوط، لكن السهم تحت متوسط 200 يوم"
    elif result.down_streak:
        result.verdict = f"هبوط {result.down_streak} من {N_DOWN}"
        result.note = "لم تكتمل موجة الهبوط بعد"
    elif above:
        result.verdict = "فوق المتوسط"
        result.note = "لا إشارة — السهم في اتجاه صاعد"
    else:
        result.verdict = "تحت المتوسط"
        result.note = "لا إشارة — لا اتجاه ولا موجة هبوط"
    return result


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------


def format_table(results: list[Analysis]) -> str:
    header = (
        f"{'السهم':<14}{'السعر':>8}{'200 يوم':>10}{'بعده':>8}"
        f"{'VWAP60':>9}{'هبوط':>6}{'حجم/معدل':>10}  الحالة"
    )
    lines = [header, "-" * len(header) + "-" * 12]
    for item in results:
        ma200 = f"{item.ma200:.2f}" if item.ma200 else "—"
        away = f"{(item.price / item.ma200 - 1) * 100:+.1f}%" if item.ma200 else "—"
        vwap = f"{item.vwap60:.2f}" if item.vwap60 else "—"
        vol = f"{item.volume_last / item.volume_avg50:.2f}x" if item.volume_avg50 else "—"
        lines.append(
            f"{item.name + ' ' + item.code.split('.')[0]:<14}{item.price:>8.2f}{ma200:>10}"
            f"{away:>8}{vwap:>9}{item.down_streak:>6}{vol:>10}  {item.verdict}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML report (inline SVG, no external assets)
# ---------------------------------------------------------------------------

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 20px; background: #0e1116; color: #e8eaed;
       font-family: system-ui, -apple-system, "Segoe UI", Tahoma, sans-serif; direction: rtl; }
h1 { font-size: 24px; margin: 0 0 4px; }
.sub { color: #9aa0a6; font-size: 14px; margin-bottom: 18px; }
.cards { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); }
.card { background: #171b22; border-radius: 16px; padding: 16px; }
.head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.name { font-size: 18px; font-weight: 600; }
.px { font-size: 18px; font-variant-numeric: tabular-nums; }
.up { color: #34a853; } .down { color: #ea4335; } .dim { color: #9aa0a6; font-size: 13px; }
table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
td { padding: 3px 0; }
td:last-child { text-align: left; font-variant-numeric: tabular-nums; }
.badge { display: inline-block; padding: 3px 10px; border-radius: 999px;
         font-size: 12px; margin-top: 8px; }
.b-ok { background: #1e5c34; color: #8ff0b0; }
.b-warn { background: #5c4a1e; color: #f0dc8f; }
.b-no { background: #3a1f22; color: #f0a0a0; }
footer { color: #7c8288; font-size: 12px; margin-top: 22px; line-height: 1.7; }
"""


def _svg_line_chart(item: Analysis, width: int = 520, height: int = 200) -> str:
    bars = item.bars[-90:]
    if len(bars) < 5:
        return ""
    pad_l, pad_r, pad_t, pad_b = 6, 54, 10, 26
    plot_h = height - pad_t - pad_b
    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars]

    series = list(closes)
    ma_series: list[float | None] = [None] * len(closes)
    for index in range(len(closes)):
        start = max(0, len(item.bars) - len(bars) + index - 49)
        window = [bar.close for bar in item.bars[start : len(item.bars) - len(bars) + index + 1]]
        if len(window) >= 50:
            ma_series[index] = statistics.fmean(window)

    low = min(closes)
    high = max(closes)
    span = (high - low) or 1.0
    max_volume = max(volumes) or 1.0

    def x_of(index: int) -> float:
        return pad_l + index * (width - pad_l - pad_r) / max(1, len(series) - 1)

    def y_of(value: float) -> float:
        return pad_t + (high - value) / span * (plot_h - 40)

    points = " ".join(f"{x_of(i):.1f},{y_of(v):.1f}" for i, v in enumerate(series))
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}">']
    parts.append(f'<rect x="0" y="0" width="{width}" height="{height}" fill="#0b0f14" rx="10"/>')

    for i, volume in enumerate(volumes):
        bar_h = (volume / max_volume) * 34
        parts.append(
            f'<rect x="{x_of(i) - 1.4:.1f}" y="{height - pad_b - bar_h:.1f}" width="2.8" '
            f'height="{bar_h:.1f}" fill="#2a3a52"/>'
        )

    parts.append(f'<polyline points="{points}" fill="none" stroke="#8ab4f8" stroke-width="1.6"/>')

    ma_points = " ".join(
        f"{x_of(i):.1f},{y_of(value):.1f}" for i, value in enumerate(ma_series) if value is not None
    )
    if ma_points:
        parts.append(
            f'<polyline points="{ma_points}" fill="none" stroke="#f0a05a" stroke-width="1.2" '
            f'stroke-dasharray="4 3"/>'
        )

    if item.ma200:
        parts.append(
            f'<text x="{width - pad_r + 4}" y="{y_of(item.price) + 4:.1f}" fill="#e8eaed" '
            f'font-size="11" direction="ltr">{item.price:.2f}</text>'
        )
    parts.append(
        f'<text x="{pad_l}" y="{height - 8}" fill="#7c8288" font-size="10" direction="ltr">'
        f"{bars[0].ts.isoformat()} → {bars[-1].ts.isoformat()}"
        "  (أزرق: السعر · برتقالي متقطع: متوسط 50)</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


def _profile_bars(item: Analysis) -> str:
    if not item.profile:
        return ""
    peak = max(share for _, share in item.profile) or 1.0
    rows = []
    for level, share in sorted(item.profile, key=lambda pair: -pair[0]):
        width = int(share / peak * 100)
        colour = "#34a853" if level >= item.price else "#5f6368"
        rows.append(
            f'<div style="display:flex;align-items:center;gap:6px;margin:2px 0;font-size:11px;'
            f'color:#9aa0a6;direction:ltr">'
            f'<span style="width:38px;text-align:right">{level:.1f}</span>'
            f'<span style="flex:1;background:#20252e;border-radius:3px;height:8px">'
            f'<span style="display:block;width:{width}%;height:8px;background:{colour};'
            f'border-radius:3px"></span></span>'
            f'<span style="width:34px">{share * 100:.1f}%</span></div>'
        )
    return "".join(rows)


def _card(item: Analysis) -> str:
    change_class = "up" if item.change_pct >= 0 else "down"
    above = item.ma200 is not None and item.price > item.ma200
    badge_class = "b-ok" if item.verdict == "إشارة كاملة" else (
        "b-warn" if item.signal_now else "b-no"
    )
    bounce = (
        f"{item.bounce_volume_ratio:.2f}x"
        if item.bounce_volume_ratio
        else "—"
    )
    ratio = f"{item.up_down_ratio:.2f}" if item.up_down_ratio else "—"
    vol_mult = f"{item.volume_last / item.volume_avg50:.2f}x" if item.volume_avg50 else "—"
    rows = [
        ("متوسط 50 يوم", f"{item.ma50:.2f}" if item.ma50 else "—"),
        ("متوسط 200 يوم", f"{item.ma200:.2f}" if item.ma200 else "—"),
        ("VWAP 20 يوم", f"{item.vwap20:.2f}" if item.vwap20 else "—"),
        ("VWAP 60 يوم", f"{item.vwap60:.2f}" if item.vwap60 else "—"),
        ("أعلى/أدنى 52 أسبوعًا", f"{item.low_52w:.2f} – {item.high_52w:.2f}"),
        ("من القمة / من القاع", f"{item.from_high_pct:+.1f}% / {item.from_low_pct:+.1f}%"),
        ("الحجم اليوم / المعدل", vol_mult),
        ("حجم الصعود ÷ الهبوط (20ي)", ratio),
        ("حجم آخر 10 أيام ÷ ما قبلها", bounce),
        ("أسابيع هبوط متتالية", str(item.down_streak)),
    ]
    table_rows = "".join(
        f"<tr><td class='dim'>{label}</td><td>{value}</td></tr>" for label, value in rows
    )
    return f"""
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
      {_svg_line_chart(item)}
      <table>{table_rows}</table>
      <div class="dim" style="margin-top:10px">تكدّس الحجم (بروفايل تقريبي)</div>
      {_profile_bars(item)}
      <span class="badge {badge_class}">{item.verdict}</span>
      <div class="dim" style="margin-top:6px">{item.note}</div>
      <div class="dim" style="margin-top:4px">
        {'السهم فوق متوسط 200 يوم ✅' if above else 'السهم تحت متوسط 200 يوم ⛔'}
      </div>
    </div>
    """


def render_html(results: list[Analysis], generated: date) -> str:
    cards = "".join(_card(item) for item in results)
    signals = [item for item in results if item.signal_now]
    closest = min(
        (item for item in results if item.ma200),
        key=lambda item: abs(item.price / item.ma200 - 1),
        default=None,
    )
    summary = (
        f"إشارات اليوم: {len(signals)}"
        if signals
        else "إشارات اليوم: لا يوجد"
    )
    if closest is not None:
        gap = (closest.price / closest.ma200 - 1) * 100
        summary += f" · الأقرب لمتوسطه: {closest.name} ({gap:+.1f}%)"
    return f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>تحليل السوق السعودي — {generated.isoformat()}</title>
<style>{CSS}</style>
</head>
<body>
  <h1>تحليل السوق السعودي</h1>
  <div class="sub">{generated.isoformat()} · القاعدة: {N_DOWN} أسابيع هبوط
    + فوق متوسط {SMA_PERIOD} يوم · {summary}</div>
  <div class="cards">{cards}</div>
  <footer>
    المصدر: Yahoo Finance (بيانات يومية). بروفايل الحجم وVWAP تقريب مبني على الإغلاق والحجم اليومي،
    وليس فوتبرنت حقيقيًا (الفوتبرنت يحتاج بيانات تنفيذ لكل صفقة،
    وهي غير متاحة علنًا للسوق السعودي).<br>
    هذه الأداة للقياس والتوثيق فقط — ليست توصية استثمارية، والقرار والتنفيذ لك وحدك.
  </footer>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_symbol(args: argparse.Namespace, code: str) -> list[DailyBar]:
    if args.demo:
        return synthetic_series()
    if args.csv:
        return load_csv(args.csv, date_format=args.date_format)
    start = date.today() - timedelta(days=args.days)
    return load_yahoo(code, start=start)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="تحليل شخصي للسوق السعودي: مقاييس + تقرير HTML برسم بياني.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--yahoo", action="append", metavar="SYMBOL", help="رمز Yahoo، مثال 1120.SR (يتكرر)"
    )
    parser.add_argument("--csv", type=Path, help="ملف CSV واحد (يُستخدم مع --symbol)")
    parser.add_argument("--symbol", default="CSV", help="اسم يظهر في التقرير عند استخدام --csv")
    parser.add_argument("--date-format", default=None, help="صيغة التاريخ داخل ملف CSV")
    parser.add_argument(
        "--demo", action="store_true", help="بيانات صناعية بلا إنترنت (للتجربة فقط)"
    )
    parser.add_argument(
        "--days", type=int, default=420, help="عدد الأيام التاريخية المطلوبة (افتراضي 420)"
    )
    parser.add_argument(
        "--out", type=Path, default=Path("analytics_report.html"), help="مسار تقرير HTML"
    )
    parser.add_argument("--no-html", action="store_true", help="اطبع الجدول فقط بدون ملف HTML")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    codes: list[tuple[str, str]] = []

    if args.demo or args.csv:
        codes.append((args.symbol, "تجريبي"))
    elif args.yahoo:
        names = dict(WATCHLIST)
        codes.extend((code, names.get(code, code.split(".")[0])) for code in args.yahoo)
    else:
        codes.extend(WATCHLIST)

    results: list[Analysis] = []
    for code, name in codes:
        try:
            bars = load_symbol(args, code)
        except OSError as exc:
            print(f"تعذّر تحميل {code}: {exc}", file=sys.stderr)
            print("تحقّق من الاتصال، أو استخدم --demo أو --csv.", file=sys.stderr)
            return 1
        if not bars:
            print(f"لا توجد بيانات لـ {code}", file=sys.stderr)
            continue
        results.append(analyse(code, name, bars))

    if not results:
        print("لا شيء لتحليله.", file=sys.stderr)
        return 1

    print(format_table(results))
    signals = [item for item in results if item.signal_now]
    print()
    if signals:
        for item in signals:
            print(f"  إشارة: {item.name} — {item.note}")
    else:
        print("  إشارات اليوم: لا يوجد")
    print("\nتنبيه: تحليل قياسي فقط — ليست توصية استثمارية.")

    if not args.no_html:
        args.out.write_text(render_html(results, date.today()), encoding="utf-8")
        print(f"\nتم إنشاء التقرير: {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
