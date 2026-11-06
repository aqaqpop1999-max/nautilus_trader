#!/usr/bin/env python3
"""Backtest the "buy after N consecutive down weeks" idea on Saudi equities.

Examples
--------
Offline smoke test (synthetic 2025-2026 data, no internet, no NautilusTrader):

    python analysis/saudi_market/run_backtest.py --demo

Real 2026 data for Al Rajhi (needs internet; the symbol is the Yahoo one):

    python analysis/saudi_market/run_backtest.py --yahoo 1120.SR --start 2026-01-01

Compare the raw system against the context filters:

    python analysis/saudi_market/run_backtest.py --yahoo 1120.SR --compare

Regime filter using a real index series (CSV export from Tadawul/Argaam):

    python analysis/saudi_market/run_backtest.py --yahoo 1120.SR \
        --filter-sma --index-csv analysis/saudi_market/data/tasi_daily.csv

Event-driven run on the NautilusTrader v2 engine (Python >= 3.13):

    python analysis/saudi_market/run_backtest.py --yahoo 1120.SR --mode nautilus
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

import filters
import signals
import vector_engine
from saudi_data import DailyBar
from saudi_data import WeeklyBar
from saudi_data import load_csv
from saudi_data import load_yahoo
from saudi_data import save_csv
from saudi_data import synthetic_series
from saudi_data import to_weekly


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
    bars = load_yahoo(args.yahoo, start=args.start or date(2026, 1, 1), end=args.end)
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

    bars, source, symbol = load_bars(args)
    bars = filter_range(bars, args.start, args.end)
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

    filter_config = filters.FilterConfig(
        use_sma=args.filter_sma,
        sma_period=args.sma_period,
        use_volume=args.filter_volume,
        volume_mult=args.volume_mult,
        volume_lookback=args.volume_lookback,
    )
    mask = None
    if filter_config.use_sma or filter_config.use_volume:
        mask = filters.build_signal_mask(
            weekly,
            bars,
            n_down=args.n_down,
            config=filter_config,
            benchmark_daily=benchmark_daily,
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
            benchmark_daily=benchmark_daily,
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
            benchmark_daily=benchmark_daily,
            fee_rate=fee_rate,
            stop_pct=stop_pct,
            target_pct=target_pct,
        )

    allowed = filters.allowed_weeks(weekly, mask) if mask is not None else None

    nautilus_trades = None
    if args.mode in ("nautilus", "auto"):
        try:
            from nautilus_engine import run_nautilus_backtest
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


if __name__ == "__main__":
    raise SystemExit(main())
