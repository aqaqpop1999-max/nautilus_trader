"""Event-driven version of the weekly dip system, running on NautilusTrader v2.

The rules are identical to ``vector_engine.py``:

* signal = the last ``n_down`` completed weeks are all down weeks;
* entry  = market buy at the open of the following week;
* exit   = market sell at the open of week ``entry + hold_weeks``, or earlier
           when a close-based stop / target is breached.

Requires the v2 package (Python >= 3.13)::

    uv venv --python 3.13 && source .venv/bin/activate
    uv pip install --pre nautilus_trader

The module imports cleanly without NautilusTrader installed; only
``run_nautilus_backtest`` needs it.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from typing import Sequence

from saudi_data import RIYADH_TZ
from saudi_data import WeeklyBar
from saudi_data import infer_price_increment_from_prices
from saudi_data import week_close_timestamp
from saudi_data import week_start


try:  # pragma: no cover - exercised only in a v2 environment
    from nautilus_trader.config import StrategyConfig
    from nautilus_trader.model import Bar
    from nautilus_trader.model import OrderSide
    from nautilus_trader.trading import Strategy

    NAUTILUS_AVAILABLE = True
except ImportError:  # pragma: no cover - offline / v1 environment
    StrategyConfig = object  # type: ignore[assignment,misc]
    Bar = object  # type: ignore[assignment,misc]
    OrderSide = None  # type: ignore[assignment]
    Strategy = object  # type: ignore[assignment,misc]
    NAUTILUS_AVAILABLE = False


class WeeklyDipConfig(StrategyConfig):
    """Configuration for :class:`WeeklyDipStrategy`."""

    def __init__(
        self,
        *,
        instrument_id: Any,
        bar_type: Any,
        n_down: int,
        hold_weeks: int,
        stop_pct: float,
        target_pct: float,
        notional: float,
        allowed_weeks: frozenset[date] | None = None,
        **_kwargs: object,
    ) -> None:
        super().__init__()
        self.instrument_id = instrument_id
        self.bar_type = bar_type
        self.n_down = n_down
        self.hold_weeks = hold_weeks
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.notional = notional
        self.allowed_weeks = allowed_weeks


class WeeklyDipStrategy(Strategy):
    """Buy after N consecutive down weeks and hold for a fixed number of weeks."""

    def __init__(self, config: WeeklyDipConfig) -> None:
        super().__init__(config)
        self._closes: list[float] = []
        self._entry_price: float | None = None
        self._entry_week: date | None = None
        self._exit_reason: str = ""
        self._exiting: bool = False
        self.trades: list[dict[str, Any]] = []

    # -- lifecycle ---------------------------------------------------------

    def on_start(self) -> None:
        self.subscribe_bars(self.config.bar_type)

    def on_stop(self) -> None:
        if not self.portfolio.is_net_flat(self.config.instrument_id):
            self._exit_reason = "end_of_data"
            self.close_all_positions(self.config.instrument_id)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _week_of(ts_event: int) -> date:
        from nautilus_trader.core.datetime import unix_nanos_to_dt

        return week_start(unix_nanos_to_dt(ts_event).astimezone(RIYADH_TZ).date())

    def _is_signal(self, index: int) -> bool:
        n_down = self.config.n_down
        first = index - n_down + 1
        if first - 1 < 0:
            return False
        for offset in range(n_down):
            current = self._closes[first + offset]
            previous = self._closes[first + offset - 1]
            if current >= previous:
                return False
        return True

    # -- bars --------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        close = float(bar.close)
        self._closes.append(close)
        index = len(self._closes) - 1
        instrument_id = self.config.instrument_id

        if not self.portfolio.is_net_flat(instrument_id):
            self._maybe_exit(bar, close)
            return

        if not self._is_signal(index):
            return

        if self.config.allowed_weeks is not None:
            if self._week_of(bar.ts_event) not in self.config.allowed_weeks:
                return

        quantity = int(self.config.notional // close)
        if quantity <= 0:
            self.log.info(f"Notional too small for one share at {close}")
            return

        from nautilus_trader.model import Quantity

        self.submit_order(
            self.order_factory.market(
                instrument_id=instrument_id,
                order_side=OrderSide.BUY,
                quantity=Quantity.from_int(quantity),
            ),
        )

    def _maybe_exit(self, bar: Bar, close: float) -> None:
        entry_price = self._entry_price
        entry_week = self._entry_week
        if entry_price is None or entry_week is None or self._exiting:
            return

        bar_week = self._week_of(bar.ts_event)
        weeks_held = (bar_week - entry_week).days // 7
        reason = ""

        if self.config.stop_pct > 0 and close <= entry_price * (1.0 - self.config.stop_pct):
            reason = "stop"
        elif self.config.target_pct > 0 and close >= entry_price * (1.0 + self.config.target_pct):
            reason = "target"
        elif weeks_held >= self.config.hold_weeks - 1:
            reason = "time"

        if reason:
            self._exit_reason = reason
            self._exiting = True
            self.close_all_positions(self.config.instrument_id)

    # -- fills -------------------------------------------------------------

    def on_order_filled(self, event: Any) -> None:
        price = float(event.last_px)
        if event.order_side == OrderSide.BUY:
            self._exiting = False
            self._entry_price = price
            self._entry_week = self._week_of(event.ts_event)
            return

        if self._entry_price is None:
            return

        self.trades.append(
            {
                "entry_week": self._entry_week,
                "exit_week": self._week_of(event.ts_event),
                "entry_price": self._entry_price,
                "exit_price": price,
                "reason": self._exit_reason or "time",
            },
        )
        self._entry_price = None
        self._entry_week = None
        self._exiting = False


def _build_bars(weekly: Sequence[WeeklyBar], bar_type: Any, instrument: Any) -> list[Any]:
    from nautilus_trader.core.datetime import dt_to_unix_nanos
    from nautilus_trader.model import Bar as NautilusBar
    from nautilus_trader.model import Quantity

    bars = []
    for bar in weekly:
        ts = dt_to_unix_nanos(week_close_timestamp(bar.last_trading_day))
        bars.append(
            NautilusBar(
                bar_type=bar_type,
                open=instrument.make_price(bar.open),
                high=instrument.make_price(bar.high),
                low=instrument.make_price(bar.low),
                close=instrument.make_price(bar.close),
                volume=Quantity.from_int(max(1, int(bar.volume))),
                ts_event=ts,
                ts_init=ts,
            ),
        )
    return bars


def run_nautilus_backtest(
    weekly: Sequence[WeeklyBar],
    *,
    symbol: str = "1120",
    venue_code: str = "TADAWUL",
    starting_balance: float = 100_000.0,
    notional: float = 25_000.0,
    n_down: int = 3,
    hold_weeks: int = 4,
    stop_pct: float = 0.07,
    target_pct: float = 0.0,
    fee_rate: float = 0.00155,
    allowed_weeks: frozenset[date] | None = None,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """Run the weekly dip system on the NautilusTrader v2 backtest engine."""
    if not NAUTILUS_AVAILABLE:
        raise RuntimeError(
            "nautilus_trader (v2) is not installed in this environment.\n"
            "Create a Python >= 3.13 environment and install the release candidate:\n"
            "  uv venv --python 3.13 && source .venv/bin/activate\n"
            "  uv pip install --pre nautilus_trader",
        )

    from decimal import Decimal

    from nautilus_trader.backtest import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig
    from nautilus_trader.execution import MakerTakerFeeModel
    from nautilus_trader.model import AccountType
    from nautilus_trader.model import BarType
    from nautilus_trader.model import Currency
    from nautilus_trader.model import Equity
    from nautilus_trader.model import InstrumentId
    from nautilus_trader.model import Money
    from nautilus_trader.model import OmsType
    from nautilus_trader.model import Price
    from nautilus_trader.model import Quantity
    from nautilus_trader.model import Symbol
    from nautilus_trader.model import TraderId
    from nautilus_trader.model import Venue

    if not weekly:
        raise ValueError("weekly bars are empty")

    sar = Currency.from_str("SAR")
    venue = Venue(venue_code)
    instrument_id = InstrumentId(Symbol(symbol), venue)
    bar_type = BarType.from_str(f"{instrument_id}-1-WEEK-LAST-EXTERNAL")

    precision, increment = infer_price_increment_from_prices(bar.close for bar in weekly)

    instrument = Equity(
        instrument_id=instrument_id,
        raw_symbol=Symbol(symbol),
        currency=sar,
        price_precision=precision,
        price_increment=Price.from_str(increment),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
    )

    engine = BacktestEngine(
        config=BacktestEngineConfig(trader_id=TraderId.from_str("SAUDI-DIP-001")),
    )
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        base_currency=sar,
        starting_balances=[Money.from_str(f"{starting_balance:.0f} SAR")],
        fee_model=MakerTakerFeeModel(
            maker_rate=Decimal(str(fee_rate)),
            taker_rate=Decimal(str(fee_rate)),
        ),
    )
    engine.add_instrument(instrument)
    engine.add_data(_build_bars(weekly, bar_type, instrument))

    strategy = WeeklyDipStrategy(
        WeeklyDipConfig(
            instrument_id=instrument_id,
            bar_type=bar_type,
            n_down=n_down,
            hold_weeks=hold_weeks,
            stop_pct=stop_pct,
            target_pct=target_pct,
            notional=notional,
            allowed_weeks=allowed_weeks,
        ),
    )
    engine.add_strategy(strategy)
    engine.run()

    if verbose:
        for trade in strategy.trades:
            entry_week = trade["entry_week"]
            exit_week = trade["exit_week"]
            held = (exit_week - entry_week).days // 7 if entry_week and exit_week else 0
            ret = trade["exit_price"] / trade["entry_price"] - 1.0
            print(
                f"{entry_week} -> {exit_week} | "
                f"{trade['entry_price']:8.2f} -> {trade['exit_price']:8.2f} | "
                f"{ret * 100:6.2f}% | {held}w | {trade['reason']}",
            )

    engine.dispose()
    return strategy.trades
