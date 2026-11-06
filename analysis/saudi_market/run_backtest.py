#!/usr/bin/env python3
"""Backtest the "buy after N consecutive down weeks" idea on Saudi equities.

Examples
--------
Offline smoke test (synthetic 2026 data, no internet, no NautilusTrader):

    python analysis/saudi_market/run_backtest.py --demo

Real 2026 data for Al Rajhi (needs internet; instrument symbol is the Yahoo one):

    python analysis/saudi_market/run_backtest.py --yahoo 1120.SR --start 2026-01-01

Your own Tadawul CSV export:

    python analysis/saudi_market/run_backtest.py --csv data/1120_daily.csv --symbol 1120

Event-driven run on the NautilusTrader v2 engine (Python >= 3.13, `uv pip install --pre nautilus_trader`):

    python analysis/saudi_market/run_backtest.py --yahoo 1120.SR --mode nautilus
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

import vector_engine
from saudi_data import DailyBar
from saudi_data import WeeklyBar
from saudi_data import load_csv
from saudi_data import load_yahoo
from saudi_data import save_csv
from saudi_data import synthetic_2026
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


def load_bars(args: argparse.Namespace) -> tuple[list[DailyBar], str, str]:
    if args.demo:
        bars = synthetic_2026()
        return bars, "synthetic", args.symbol or "DEMO"
    if args.csv is not None:
        bars = load_csv(args.csv, date_format=args.date_format)
        return bars, str(args.csv), args.symbol or args.csv.stem.split("_")[0]
    bars = load_yahoo(args.yahoo, start=args.start or date(2026, 1, 1), end=args.end)
    return bars, f"yahoo:{args.yahoo}", args.symbol or args.yahoo.split(".")[0]


def filter_range(bars: list[DailyBar], start: date | None, end: date | None) -> list[DailyBar]:
    selected = bars
    if start is not None:
        selected = [bar for bar in selected if bar.ts >= start]
    if end is not None:
        selected = [bar for bar in selected if bar.ts <= end]
    return selected


def print_header(
    *,
    symbol: str,
    source: str,
    daily: list[DailyBar],
    weekly: list[WeeklyBar],
    n_down: int,
) -> None:
    signals = vector_engine.signal_indices(weekly, n_down)
    first, last = daily[0], daily[-1]
    print("\n=== البيانات ===")
    print(f"  الرمز              : {symbol}")
    print(f"  المصدر             : {source}")
    print(f"  الفترة             : {first.ts} -> {last.ts}")
    print(f"  الأيام / الأسابيع   : {len(daily)} / {len(weekly)}")
    print(f"  أول إغلاق / آخره    : {first.close:.2f} / {last.close:.2f}")
    print(f"  أعلى / أدنى إغلاق   : {max(b.close for b in daily):.2f} / {min(b.close for b in daily):.2f}")
    print(f"  إشارات {n_down} أسابيع هابطة : {len(signals)}")


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
    *,
    fee_rate: float,
    stop_pct: float,
    target_pct: float,
) -> None:
    """Print a small grid of (n_down, hold_weeks) results."""
    print("\n=== مسح المعاملات (n_down x hold_weeks) — العائد التراكمي ===")
    header = "  n_down | " + " | ".join(f"hold {weeks}w" for weeks in range(2, 9))
    print(header)
    print("  " + "-" * (len(header) - 2))
    for n_down in range(1, 6):
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
            )
            stats = vector_engine.summarize(result)
            cells.append(f"{stats['cumulative_return'] * 100:7.2f}%")
        print(f"  {n_down:6d} | " + " | ".join(cells))
    print("  (الأرقام: العائد التراكمي بعد العمولات)")


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
        verbose=not args.quiet,
    )
    stats = vector_engine.summarize(result, buy_hold=buy_hold)
    print(vector_engine.format_stats(stats, title="نتائج محرك المتجهات (Vector)"))

    if args.sweep:
        print_sweep(weekly, fee_rate=fee_rate, stop_pct=stop_pct, target_pct=target_pct)

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
                    print(
                        "  ملاحظة: الفرق طبيعي عند حدود البيانات أو عند إغلاق المركز النهائي.",
                    )

    if args.export_trades is not None:
        trades_to_write = nautilus_trades if nautilus_trades else result.trades
        write_trades(args.export_trades, list(trades_to_write))
        print(f"  تم حفظ الصفقات -> {args.export_trades}")

    print("\nتنبيه: النتائج تعليمية فقط — ليست توصية استثمارية، والباكتيست لا يضمن الأداء المستقبلي.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
