# -*- coding: utf-8 -*-
"""
sd1sd2_engine.py

Single engine for the SD1SD2 pattern (single-stage entry at the 2SD band -> SL/Exit1/Exit2/EOD),
used identically by both the live paper-trade logger and the historical backtest replay -- same
LIVE/BACKTEST-share-one-engine design as vwappiercing_options' piercing_engine.py (see its
docstring for the full rationale), simplified here for a pattern that has no piercing/reclaim/
confirm funnel -- the "piercing" of the entry band IS the entry, single-stage.

VWAP is treated as the market's moving equilibrium. Entry, Exit1 and SL are all the same shape --
"current VWAP +/- k * current SD [of (Close-VWAP)]" -- just with a different k
(pattern_rules.sd_bands_at); Exit2 is simply "current VWAP" (k=0). Because VWAP/SD keep evolving
for as long as new candles close, ALL THREE stay dynamic for the life of an open trade too (unlike
vwappiercing_options' SL/Exit1-3, which are fixed at entry) -- Exit2 is always "wherever VWAP is
right now", not a price frozen at entry.

BUY direction: price pierces below -2SD (oversold vs VWAP) -> buy a CE, expecting reversion up
toward VWAP; SL sits further below at -3SD if the reversion never happens.
SELL direction: price pierces above +2SD (overbought vs VWAP) -> buy a PE, expecting reversion
down toward VWAP; SL sits further above at +3SD.

Any trade still open at FORCE_EXIT_TIME is force-closed at the prevailing price regardless of
SL/Exit1/Exit2 state.
"""
import threading
import time
import traceback
from datetime import datetime, date, timedelta
from enum import Enum

import pandas as pd

from BusinessLogic.interfaces.ILogic import *
from BrokerUtility.pal.utility_manager import *
from Utility.quotes_utility import *
from Utility.utility import compute_vwap, generate_weekly_expiry_dates, generate_monthly_expiry_dates
from DataTypes.defines import *
from ..DataTypes.trade_data import sd1sd2_trade_row, candle_snapshot, exit_hit
from ..DataTypes.order_log_data import order_log_row
from ..UserInterface.adapter.login.login import *
from ..UserInterface.adapter.config.config import *
from ..UserInterface.gsheet.paper_trade.paper_trade import *
from ..UserInterface.gsheet.order_log.order_log import *
from ..Config.config_loader import load_config
from . import pattern_rules
from .option_selection import select_closest_to_atm_in_band

# Which sd1sd2_trade_row exit_hit field + display label each Config/strategy_config.json
# "target_exit" value maps to. None/unrecognized -> SL is the only real exit.
TARGET_EXIT_FIELD_MAP = {
    "exit1": ("exit1_hit", "Exit1"),
    "exit2": ("exit2_hit", "Exit2"),
}


class Mode(Enum):
    LIVE = "LIVE"
    BACKTEST = "BACKTEST"


STATE_SEEK_ENTRY = "SEEK_ENTRY"
STATE_IN_TRADE = "IN_TRADE"

STATUS_NO_DATA = "no_data"
STATUS_OK = "ok"

# rolling-stdev-of-(Close-VWAP) column, added to the main-interval candle series alongside VWAP.
SD_COL = "sd1sd2_sd"

CHAIN_UNDERLYING_SYMBOL = {"NIFTY": "NIFTY50-INDEX", "BANKNIFTY": "NIFTYBANK-INDEX"}


class _DirectionState:
    """All the mutable pattern/trade-tracking state for one direction (BUY or SELL)."""

    def __init__(self, direction):
        self.direction = direction
        self.state = STATE_SEEK_ENTRY
        # LIVE only: suppress a repeated per-candle status line until the candle actually advances.
        self.last_logged_candle_ts = None
        # replay (BACKTEST / LIVE test mode): last 1-min bar this direction has processed, used
        # both while seeking entry and while in-trade (a direction is only ever one or the other).
        self.last_processed_bar_ts = None

        self.current_trade: sd1sd2_trade_row = None
        self.option_symbol = ""
        self.order_quantity = 0
        self.mae = 0.0
        self.mfe = 0.0
        self.mae_time = ""
        self.mfe_time = ""

        # set True after an SL stop-out; cleared once price pulls back to within
        # reentry_sd_threshold SD of VWAP (see pattern_rules.reentry_cleared) -- prevents
        # immediately re-entering while price is still sitting beyond the entry band during a
        # stretch that isn't actually reverting. Never set after a winning target-exit close.
        self.reentry_blocked = False


class SD1SD2Engine(ILogic):

    STATE_SEEK_ENTRY = STATE_SEEK_ENTRY
    STATE_IN_TRADE = STATE_IN_TRADE

    FUTURE_MARKET_TYPE = "FUT"
    OPTION_MARKET_TYPE = "OPT"

    def __init__(self, mode: Mode, **kwargs):
        self.mode = mode
        self.logic_name = "LogicSD1SD2"

        # Config/strategy_config.json -- the single source of truth for every tunable trading
        # parameter, shared by LIVE and BACKTEST alike (see Config/config_loader.py).
        self.cfg = load_config()
        pattern_rules.configure(self.cfg)

        self.index_name = self.cfg["index_name"]
        self.strike_step = self.cfg["strike_step"]
        self.target_premium_low = self.cfg["option_premium_band_low"]
        self.target_premium_high = self.cfg["option_premium_band_high"]
        self.day_start_time = self.cfg["day_start_time"]
        self.sd_period = self.cfg["sd_period"]
        self.test_mode_start_time = self.cfg["test_mode_start_time"]
        self.test_mode_step_seconds = self.cfg["test_mode_step_seconds"]
        self.historical_option_lookup_delay_seconds = self.cfg["historical_option_lookup_delay_seconds"]
        self.order_cfg = self.cfg["order"]

        # Real order placement (LIVE only, never test mode/BACKTEST): live_trading_enabled is a
        # master safety switch -- absent/false keeps LIVE fully paper-trade. SL is ALWAYS a real
        # exit once live trading is enabled; target_exit additionally makes ONE of Exit1/Exit2
        # real too, so the position closes on whichever of the two actually hits first.
        self.live_trading_enabled = bool(self.cfg.get("live_trading_enabled", False))
        self.target_exit_field, self.target_exit_label = TARGET_EXIT_FIELD_MAP.get(
            (self.cfg.get("target_exit") or "").lower(), (None, None))
        self.exit_labels = {"exit1_hit": "Exit1 (1SD)", "exit2_hit": "Exit2 (VWAP)"}

        self.future_symbol = ""
        self.current_expiry = ""
        self.directions = {"BUY": _DirectionState("BUY"), "SELL": _DirectionState("SELL")}
        self.last_candle_data = None  # main-interval candle series (with VWAP + SD_COL columns)
        self.test_mode = False

        if mode == Mode.LIVE:
            self.__init_live(**kwargs)
        else:
            self.__init_backtest(**kwargs)

    # ------------------------------------------------------------------
    # mode-specific setup
    # ------------------------------------------------------------------
    def __init_live(self, args, broker_utility_manager: utility_manager, quotes_utility: QuoteUtility):
        self.obj_utility_manager = broker_utility_manager
        self.obj_ui_adapter_login: UserInterfaceAdapterLogin = UserInterfaceAdapterLogin(args)
        self.obj_ui_adapter_config: UserInterfaceAdapterConfig = UserInterfaceAdapterConfig(args)
        self.trade_utility = self.obj_utility_manager.get_utility_object(self.obj_ui_adapter_login.get_data())
        self.quotes_utility: QuoteUtility = quotes_utility
        self.quotes_utility.set_trade_utility(self.trade_utility)
        self.config_data = self.obj_ui_adapter_config.get_data()
        self.obj_paper_trade_writer = UserInterfacePaperTrade(args.key)
        self.obj_order_log_writer = UserInterfaceOrderLog(args.key)

        self.test_mode = bool(getattr(args, "test_mode", False))
        self.session_date_str = args.date if self.test_mode else date.today().strftime("%Y-%m-%d")
        self.test_clock = datetime.strptime(f"{self.session_date_str} {self.test_mode_start_time}",
                                            "%Y-%m-%d %H:%M:%S")
        if self.test_mode:
            print(self.logic_name, ": TEST MODE -- using candles for", self.session_date_str,
                  "with the clock starting at", self.test_mode_start_time)
            self.broker = self.trade_utility.get_broker_utility()
            self.trade_date_str = self.session_date_str
            self.log_fn = lambda msg: print(self.logic_name, ":", msg)
            self.test_symbol_candles = {}

        self.pre_requisite_complete_event = threading.Event()

        self.pre_requisite_start_time = (self.__now() + timedelta(seconds=10)).strftime('%H:%M:%S')
        self.execution_start_time = self.config_data.start_time
        self.execution_stop_time = self.config_data.end_time
        self.candle_interval_minutes = int(self.config_data.candle_interval)
        self.entry_start_time = pattern_rules.compute_entry_start_time(self.execution_start_time)

        self.processed_candle_count = 0
        self.test_day_candles = {}

        self.pre_requisite_thread = threading.Thread(target=self.pre_requisite_thread_handler)
        self.execute_thread = threading.Thread(target=self.execute)
        self.exit_thread = threading.Thread(target=self.exit_execution_thread)

        self.pre_requisite_thread.start()
        self.execute_thread.start()
        self.exit_thread.start()

    def __init_backtest(self, broker, index_name, trade_date_str, candle_interval_minutes, log_fn=None,
                        on_trade_closed=None):
        self.broker = broker
        self.index_name = index_name
        self.trade_date_str = trade_date_str
        self.candle_interval_minutes = candle_interval_minutes
        self.log_fn = log_fn or (lambda msg: None)
        # called with each trade the moment it closes (SL/target-exit/force-exit/EOD), not batched
        # until the whole day finishes -- a day with many entries can take a long time to fully
        # replay (each entry re-scans ~41 strikes historically), so callers that want to see
        # progress as it happens (e.g. writing straight to a sheet) should pass this instead of
        # waiting on run_backtest_day()'s returned list.
        self.on_trade_closed = on_trade_closed or (lambda trade: None)
        self.entry_start_time = pattern_rules.compute_entry_start_time(self.day_start_time)
        self.results = []
        self.session_date_str = trade_date_str
        self.processed_candle_count = 0
        self.test_day_candles = {}

    def get_broker_utility(self):
        return self.trade_utility

    def get_thread_info(self):
        return self.exit_thread

    # ------------------------------------------------------------------
    # LIVE thread handlers
    # ------------------------------------------------------------------
    def pre_requisite_thread_handler(self):
        print(self.logic_name, ": Inside pre-requisite thread")
        broker = self.trade_utility.get_broker_utility()
        self.future_symbol, self.current_expiry = pattern_rules.resolve_front_month_future_symbol(
            broker, self.index_name, self.session_date_str)
        print(self.logic_name, ": Future symbol resolved: ", self.future_symbol)
        self.quotes_utility.add_stocks([self.future_symbol], [self.FUTURE_MARKET_TYPE])
        if self.test_mode:
            self.__load_test_day_candles(broker, self.candle_interval_minutes)
        self.pre_requisite_complete_event.set()
        print(self.logic_name, ": Exiting pre-requisite thread")

    def execute(self):
        self.pre_requisite_complete_event.wait()
        print(self.logic_name, ": Execution Started", self.execution_start_time)
        has_started = False
        entry_window_announced = False

        while not self.__is_time_reached(self.execution_stop_time):
            if not self.__is_time_reached(self.execution_start_time):
                self.__advance_or_sleep(2)
                continue

            if not has_started:
                print(self.logic_name, f": [{self.__now().strftime('%H:%M:%S')}] execution start time reached, beginning candle/tick processing")
                has_started = True
            if not entry_window_announced and self.__is_entry_window_open():
                print(self.logic_name, f": [{self.__now().strftime('%H:%M:%S')}] entry window open (>= {self.entry_start_time})")
                entry_window_announced = True

            self.__run_pass(self.__now().strftime("%Y-%m-%d %H:%M:%S"))
            self.__advance_or_sleep(3)

        for ds in self.directions.values():
            if ds.state == STATE_IN_TRADE:
                self.__finalize_trade_at_eod_live(ds)

        print(self.logic_name, ": Execution loop ended")

    def __run_pass(self, now_str):
        """
        One pass of the engine at now_str ("YYYY-MM-DD HH:MM:SS"), shared by the LIVE execute loop
        and the BACKTEST replay (run_backtest_day), so both walk the day through the exact same
        steps in the same order.
        """
        self.__process_new_candles_live(now_str)

        for ds in self.directions.values():
            if self.__is_replay():
                self.__check_replay_bars(ds, now_str)
            elif ds.state == STATE_SEEK_ENTRY:
                self.__check_seek_entry_live(ds)
            elif ds.state == STATE_IN_TRADE:
                self.__check_exit_hits_live(ds)

    def __is_replay(self):
        return self.mode == Mode.BACKTEST or self.test_mode

    def __broker(self):
        return self.trade_utility.get_broker_utility() if self.mode == Mode.LIVE else self.broker

    def exit_execution_thread(self):
        print(self.logic_name, ": Start of Exiting Thread")
        self.execute_thread.join()
        self.quotes_utility.stop()
        self.quotes_utility.get_thread_info().join()
        print(self.logic_name, ": End of Exiting Thread")

    # ------------------------------------------------------------------
    # LIVE candle/tick sourcing
    # ------------------------------------------------------------------
    def __process_new_candles_live(self, now_str):
        broker = self.__broker()
        str_from_date = f"{self.session_date_str} 09:15:00"
        candle_data = self.__fetch_candles_live(broker, self.candle_interval_minutes, str_from_date, now_str)
        if candle_data is not None and len(candle_data) > 0:
            if not self.__is_replay():
                self.__add_session_vwap(candle_data)
                self.__add_sd(candle_data)
            self.last_candle_data = candle_data
            self.processed_candle_count = len(candle_data)

    def __fetch_candles_live(self, broker, interval_minutes, str_from_date, now_str):
        if not self.__is_replay():
            return broker.fetchOHLC(self.future_symbol, str_from_date, now_str,
                                    interval=f"{interval_minutes}minute",
                                    all_data=True, market_type=self.FUTURE_MARKET_TYPE)
        day = self.test_day_candles.get(interval_minutes)
        if day is None:
            day = self.__load_test_day_candles(broker, interval_minutes)
        return self.__closed_by(day, interval_minutes, now_str)

    def __closed_by(self, day, interval_minutes, now_str):
        if day is None or len(day) == 0:
            return day
        starts = pd.to_datetime(self.session_date_str + " "
                                + day[DATE_TIME].astype(str).map(pattern_rules.time_of_day))
        closes = starts + pd.Timedelta(minutes=interval_minutes)
        now = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
        return day[(closes <= now).values].reset_index(drop=True).copy()

    def __ltp(self, symbol):
        if not symbol:
            return None
        if not self.test_mode:
            quote = self.quotes_utility.get_quote_data().get(symbol)
            return quote.ltp if quote is not None else None
        now_str = self.__now().strftime("%Y-%m-%d %H:%M:%S")
        broker = self.trade_utility.get_broker_utility()
        if symbol == self.future_symbol:
            rows = self.__fetch_candles_live(broker, self.candle_interval_minutes, None, now_str)
        else:
            if symbol not in self.test_symbol_candles:
                self.test_symbol_candles[symbol] = broker.fetchOHLC(
                    symbol, f"{self.session_date_str} 09:15:00", f"{self.session_date_str} 15:30:00",
                    interval=f"{self.candle_interval_minutes}minute", all_data=True, market_type=self.__option_market_type())
            rows = self.__closed_by(self.test_symbol_candles[symbol], self.candle_interval_minutes, now_str)
        if rows is None or len(rows) == 0:
            return None
        return float(rows[CLOSE_PRICE].iloc[-1])

    def __load_test_day_candles(self, broker, interval_minutes):
        data = broker.fetchOHLC(self.future_symbol, f"{self.session_date_str} 09:15:00",
                                f"{self.session_date_str} 15:30:00",
                                interval=f"{interval_minutes}minute",
                                all_data=True, market_type=self.FUTURE_MARKET_TYPE)
        if interval_minutes == self.candle_interval_minutes and data is not None and len(data) > 0:
            self.__add_session_vwap(data)
            self.__add_sd(data)
        self.test_day_candles[interval_minutes] = data
        msg = f"loaded {0 if data is None else len(data)} {interval_minutes}-min candles for {self.session_date_str}"
        if self.mode == Mode.LIVE:
            print(self.logic_name, ": TEST MODE --", msg)
        else:
            self.__log(msg)
        return data

    def __add_session_vwap(self, candle_data):
        candle_data[VWAP] = [compute_vwap(candle_data, last_loc=i + 1) for i in range(len(candle_data))]

    def __add_sd(self, candle_data):
        candle_data[SD_COL] = pattern_rules.compute_sd(candle_data, self.sd_period)

    def __current_bands(self):
        # LIVE: VWAP/SD from the latest closed main-interval candle. None until sd_period candles
        # have closed (rolling stdev needs that many points).
        if self.last_candle_data is None or len(self.last_candle_data) == 0:
            return None
        row = self.last_candle_data.iloc[-1]
        sd = row[SD_COL]
        if sd != sd:  # NaN check without importing pandas/numpy here
            return None
        return float(row[VWAP]), float(sd)

    def __bands_at(self, moment):
        # replay: VWAP/SD from the main-interval candles closed by `moment`.
        closed = self.__closed_by(self.last_candle_data, self.candle_interval_minutes,
                                  moment.strftime("%Y-%m-%d %H:%M:%S"))
        if closed is None or len(closed) == 0:
            return None
        row = closed.iloc[-1]
        sd = row[SD_COL]
        if sd != sd:
            return None
        return float(row[VWAP]), float(sd)

    def __session_time(self, candle_ts):
        return datetime.strptime(f"{self.session_date_str} {pattern_rules.time_of_day(str(candle_ts))}",
                                 "%Y-%m-%d %H:%M:%S")

    def __log_once_per_candle(self, ds: _DirectionState, message):
        candle_ts = None if self.last_candle_data is None or len(self.last_candle_data) == 0 \
            else str(self.last_candle_data[DATE_TIME].iloc[-1])
        if candle_ts is not None and candle_ts == ds.last_logged_candle_ts:
            return
        if candle_ts is not None:
            ds.last_logged_candle_ts = candle_ts
        if self.mode == Mode.LIVE:
            print(self.logic_name, message)
        else:
            self.__log(message.lstrip(": "))

    def __is_entry_window_open(self):
        return pattern_rules.is_entry_window_open(self.__now().strftime("%H:%M:%S"), self.entry_start_time)

    # ------------------------------------------------------------------
    # replay (BACKTEST / LIVE test mode): driven by the main-interval candle series only -- entry
    # and exit hits are both checked on that same candle's High/Low, no separate 1-min series.
    # ------------------------------------------------------------------
    def __check_replay_bars(self, ds: _DirectionState, now_str):
        bars = self.__fetch_candles_live(self.__broker(), self.candle_interval_minutes, None, now_str)
        if bars is None or len(bars) == 0:
            return
        if ds.last_processed_bar_ts is not None:
            starts = bars[DATE_TIME].astype(str).map(self.__session_time)
            bars = bars[(starts > self.__session_time(ds.last_processed_bar_ts)).values]
        for _, bar in bars.iterrows():
            bar_ts = ds.last_processed_bar_ts = str(bar[DATE_TIME])
            if ds.state == STATE_SEEK_ENTRY:
                if not pattern_rules.is_entry_window_open(pattern_rules.time_of_day(bar_ts), self.entry_start_time):
                    continue
                self.__check_seek_entry_bar(ds, bar, bar_ts)
                if ds.state == STATE_IN_TRADE:
                    return  # entered this pass -- resume from the next bar on the next __run_pass
            elif ds.state == STATE_IN_TRADE:
                outcome = self.__in_trade_minute(ds, bar)
                self.__log_once_per_candle(ds, f": [{bar_ts}] ({ds.direction}) in-trade {self.candle_interval_minutes}-min "
                                               f"O={bar[OPEN_PRICE]} H={bar[HIGH_PRICE]} L={bar[LOW_PRICE]} "
                                               f"C={bar[CLOSE_PRICE]}")
                if outcome is not None:
                    self.__close_trade(ds, outcome)
                    return

    def __check_seek_entry_bar(self, ds: _DirectionState, bar, ts):
        bands = self.__bands_at(self.__session_time(ts))
        if bands is None:
            if self.mode == Mode.BACKTEST:
                self.__log(f"[{ts}] ({ds.direction}) seeking entry -- not enough candles yet for SD")
            return
        vwap, sd = bands
        high, low = float(bar[HIGH_PRICE]), float(bar[LOW_PRICE])

        if ds.reentry_blocked:
            clearing_price = high if ds.direction == "BUY" else low
            if pattern_rules.reentry_cleared(ds.direction, clearing_price, vwap, sd):
                ds.reentry_blocked = False
                self.__log(f"[{ts}] ({ds.direction}) reentry cooldown cleared -- price back within "
                          f"{pattern_rules.REENTRY_SD_THRESHOLD}SD of VWAP")
            else:
                self.__log(f"[{ts}] ({ds.direction}) reentry blocked after SL -- waiting for price to "
                          f"pull back within {pattern_rules.REENTRY_SD_THRESHOLD}SD of VWAP "
                          f"(VWAP={vwap:.2f} SD={sd:.2f})")
                return

        entry_upper, entry_lower, _, _, _, _ = pattern_rules.sd_bands_at(vwap, sd)
        msg = (f"[{ts}] ({ds.direction}) seeking entry: H={high} L={low} VWAP={vwap:.2f} SD={sd:.2f} "
              f"entry@{entry_lower:.2f}/{entry_upper:.2f}")
        if ds.direction == "BUY" and low <= entry_lower:
            self.__log(msg + " -> TRIGGERED")
            self.__enter_trade(ds, entry_lower, ts, bar, vwap, sd)
        elif ds.direction == "SELL" and high >= entry_upper:
            self.__log(msg + " -> TRIGGERED")
            self.__enter_trade(ds, entry_upper, ts, bar, vwap, sd)
        else:
            self.__log(msg)

    def __in_trade_minute(self, ds: _DirectionState, bar):
        """
        One main-interval future candle of an open trade: MAE/MFE over its High/Low, the
        FORCE_EXIT_TIME force-exit at its Close, then SL/Exit1/Exit2 hit if its High/Low touches
        the (dynamic, VWAP/SD-based) level -- computed from the candles closed strictly before this
        one (see __bands_at), so this candle's own not-yet-closed VWAP/SD is never used to judge
        itself. Returns "FORCE", "SL", or the configured target_exit's label if the trade closed on
        this candle, else None.
        """
        trade = ds.current_trade
        ts = str(bar[DATE_TIME])
        self.__update_mae_mfe_range(ds, float(bar[HIGH_PRICE]), float(bar[LOW_PRICE]), ts)

        if pattern_rules.is_force_exit_time_reached(pattern_rules.time_of_day(ts)):
            trade.exit3_eod.future_price = float(bar[CLOSE_PRICE])
            trade.exit3_eod.option_price = self.__historical_option_close_near(ds.option_symbol, ts) or 0.0
            return "FORCE"

        bands = self.__bands_at(self.__session_time(ts))
        if bands is None:
            return None
        vwap, sd = bands
        sl_level, exit1_level = self.__levels_for(ds.direction, vwap, sd)

        was_hit = self.__snapshot_exit_hits(trade)
        self.__mark_exit_if_hit_range(ds, trade.sl_hit, sl_level, bar, ds.direction, ts, is_stop=True)
        self.__mark_exit_if_hit_range(ds, trade.exit1_hit, exit1_level, bar, ds.direction, ts)
        self.__mark_exit_if_hit_range(ds, trade.exit2_hit, vwap, bar, ds.direction, ts)
        self.__log_exit_breaches(ds, was_hit)
        if trade.sl_hit.is_hit:
            return "SL"
        if self.__configured_real_exit_hit(trade):
            return self.target_exit_label
        return None

    def __close_trade(self, ds: _DirectionState, outcome):
        self.__stamp_mae_mfe(ds)
        if self.mode == Mode.LIVE:
            reason = "Force Exit" if outcome == "FORCE" else outcome
            self.__finalize_and_reset_live(ds, reason)
            return
        trade = ds.current_trade
        self.results.append(trade)
        self.on_trade_closed(trade)
        if outcome == "SL":
            ds.reentry_blocked = True
            self.__log(f"[{trade.sl_hit.timestamp}] ({ds.direction}) SL hit @ {trade.sl_hit.future_price} "
                      f"({self.__fmt_option_price(trade.sl_hit.option_price)}) -- trade closed, resuming scan")
        elif outcome == "FORCE":
            self.__log(f"[{ds.last_processed_bar_ts}] ({ds.direction}) force-exit (cutoff) @ "
                      f"{trade.exit3_eod.future_price} ({self.__fmt_option_price(trade.exit3_eod.option_price)}) "
                      f"-- trade closed, resuming scan")
        else:
            hit_obj = getattr(trade, self.target_exit_field)
            self.__log(f"[{hit_obj.timestamp}] ({ds.direction}) {outcome} hit @ {hit_obj.future_price} "
                      f"({self.__fmt_option_price(hit_obj.option_price)}) -- REAL exit, trade closed, resuming scan")
        self.__reset_direction_backtest(ds)

    # ------------------------------------------------------------------
    # LIVE (non-replay) per-tick entry/exit checking
    # ------------------------------------------------------------------
    def __check_seek_entry_live(self, ds: _DirectionState):
        if not self.__is_entry_window_open():
            return
        bands = self.__current_bands()
        if bands is None:
            return
        vwap, sd = bands
        future_ltp = self.__ltp(self.future_symbol)
        if future_ltp is None:
            return
        now_str = self.__now().strftime("%H:%M:%S")

        if ds.reentry_blocked:
            if pattern_rules.reentry_cleared(ds.direction, future_ltp, vwap, sd):
                ds.reentry_blocked = False
                print(self.logic_name, f": ({ds.direction}) reentry cooldown cleared -- price back "
                     f"within {pattern_rules.REENTRY_SD_THRESHOLD}SD of VWAP")
            else:
                self.__log_once_per_candle(ds, f": [{now_str}] ({ds.direction}) reentry blocked after SL "
                     f"-- waiting for price to pull back within {pattern_rules.REENTRY_SD_THRESHOLD}SD "
                     f"of VWAP (LTP={future_ltp} VWAP={vwap:.2f} SD={sd:.2f})")
                return

        entry_upper, entry_lower, _, _, _, _ = pattern_rules.sd_bands_at(vwap, sd)
        self.__log_once_per_candle(ds, f": [{now_str}] ({ds.direction}) seeking entry LTP={future_ltp} "
             f"VWAP={vwap:.2f} SD={sd:.2f} entry@{entry_lower:.2f}/{entry_upper:.2f}")
        if ds.direction == "BUY" and future_ltp <= entry_lower:
            self.__enter_trade(ds, future_ltp, now_str, None, vwap, sd)
        elif ds.direction == "SELL" and future_ltp >= entry_upper:
            self.__enter_trade(ds, future_ltp, now_str, None, vwap, sd)

    def __check_exit_hits_live(self, ds: _DirectionState):
        future_ltp = self.__ltp(self.future_symbol)
        if future_ltp is None:
            return
        option_ltp = self.__ltp(ds.option_symbol)
        if option_ltp is None:
            option_ltp = ds.current_trade.entry_option_price
        now_str = self.__now().strftime("%H:%M:%S")

        self.__update_mae_mfe_point(ds, future_ltp, now_str)

        if pattern_rules.is_force_exit_time_reached(now_str):
            self.__finalize_trade_at_eod_live(ds, "Force Exit")
            return

        bands = self.__current_bands()
        if bands is None:
            return
        vwap, sd = bands
        sl_level, exit1_level = self.__levels_for(ds.direction, vwap, sd)

        was_hit = self.__snapshot_exit_hits(ds.current_trade)
        self.__mark_exit_if_hit_point(ds, ds.current_trade.sl_hit, sl_level, future_ltp, option_ltp, now_str, is_stop=True)
        self.__mark_exit_if_hit_point(ds, ds.current_trade.exit1_hit, exit1_level, future_ltp, option_ltp, now_str)
        self.__mark_exit_if_hit_point(ds, ds.current_trade.exit2_hit, vwap, future_ltp, option_ltp, now_str)
        self.__log_exit_breaches(ds, was_hit)

        self.__log_once_per_candle(ds, f": [{now_str}] ({ds.direction}) in-trade LTP={future_ltp} "
             f"({self.__fmt_option_price(option_ltp)}) VWAP={vwap:.2f} SD={sd:.2f} "
             f"SL={sl_level:.2f} Exit1={exit1_level:.2f} Exit2={vwap:.2f}")

        if ds.current_trade.sl_hit.is_hit:
            self.__stamp_mae_mfe(ds)
            self.__finalize_and_reset_live(ds, "SL")
        elif self.__configured_real_exit_hit(ds.current_trade):
            self.__stamp_mae_mfe(ds)
            self.__finalize_and_reset_live(ds, self.target_exit_label)

    def __levels_for(self, direction, vwap, sd):
        """(sl_level, exit1_level) for `direction` given the current VWAP/SD -- Exit2 is always
        just `vwap` itself, so callers use that directly rather than a third return value."""
        entry_upper, entry_lower, exit1_upper, exit1_lower, sl_upper, sl_lower = \
            pattern_rules.sd_bands_at(vwap, sd)
        if direction == "BUY":
            return sl_lower, exit1_lower
        return sl_upper, exit1_upper

    def __mark_exit_if_hit_point(self, ds: _DirectionState, exit_hit_obj: exit_hit, level, future_ltp, option_ltp, now_str, is_stop=False):
        if exit_hit_obj.is_hit or level == 0.0:
            return
        if is_stop:
            hit = (future_ltp <= level) if ds.direction == "BUY" else (future_ltp >= level)
        else:
            hit = (future_ltp >= level) if ds.direction == "BUY" else (future_ltp <= level)
        if hit:
            exit_hit_obj.future_price = future_ltp
            exit_hit_obj.option_price = option_ltp
            exit_hit_obj.timestamp = now_str
            exit_hit_obj.is_hit = True

    def __mark_exit_if_hit_range(self, ds: _DirectionState, exit_hit_obj: exit_hit, level, row, direction, ts, is_stop=False):
        if exit_hit_obj.is_hit:
            return
        high = float(row[HIGH_PRICE])
        low = float(row[LOW_PRICE])
        if is_stop:
            hit = (low <= level) if direction == "BUY" else (high >= level)
        else:
            hit = (high >= level) if direction == "BUY" else (low <= level)
        if hit:
            exit_hit_obj.future_price = level
            exit_hit_obj.option_price = self.__historical_option_close_near(ds.option_symbol, ts) or 0.0
            exit_hit_obj.timestamp = ts
            exit_hit_obj.is_hit = True

    def __finalize_trade_at_eod_live(self, ds: _DirectionState, reason="EOD"):
        future_ltp = self.__ltp(self.future_symbol)
        if future_ltp is None:
            future_ltp = ds.current_trade.entry_future_price
        option_ltp = self.__ltp(ds.option_symbol)
        if option_ltp is None:
            option_ltp = ds.current_trade.entry_option_price

        ds.current_trade.exit3_eod.future_price = future_ltp
        ds.current_trade.exit3_eod.option_price = option_ltp
        self.__stamp_mae_mfe(ds)

        self.__finalize_and_reset_live(ds, reason)

    def __finalize_and_reset_live(self, ds: _DirectionState, reason="EOD"):
        if reason == "SL":
            close_option_price = ds.current_trade.sl_hit.option_price
        elif reason == self.target_exit_label:
            close_option_price = getattr(ds.current_trade, self.target_exit_field).option_price
        else:
            close_option_price = ds.current_trade.exit3_eod.option_price

        if self.live_trading_enabled and not self.test_mode and ds.option_symbol and ds.order_quantity:
            order_placed, _ = self.__place_real_order(ds.option_symbol, "SELL", ds.order_quantity,
                                                       trade_type=ds.direction, order_type="Exit",
                                                       reason=reason, reference_price=close_option_price)
            if not order_placed:
                print(self.logic_name, f": ({ds.direction}) REAL EXIT ORDER FAILED for {ds.option_symbol} "
                     f"({reason}) -- NOT resetting, will retry closing on the next check")
                return

        self.obj_paper_trade_writer.write_trade(ds.current_trade, self.candle_interval_minutes,
                                                describe_exit_outcomes(ds.current_trade))
        print(self.logic_name, f": ({ds.direction}) Trade closed ({reason}) @ {self.__fmt_option_price(close_option_price)}, logged to PaperTradeData:",
             ds.current_trade.trade_type, ds.current_trade.option_name)
        if reason == "SL":
            ds.reentry_blocked = True
        ds.current_trade = None
        ds.option_symbol = ""
        ds.order_quantity = 0
        ds.last_logged_candle_ts = None
        ds.state = STATE_SEEK_ENTRY

    def __option_market_type(self):
        broker = self.trade_utility.get_broker_utility() if self.mode == Mode.LIVE else self.broker
        return getattr(broker, "OPTION_MARKET_TYPE", self.OPTION_MARKET_TYPE)

    def __configured_real_exit_hit(self, trade: sd1sd2_trade_row):
        if not self.target_exit_field:
            return False
        return getattr(trade, self.target_exit_field).is_hit

    def __order_quantity(self):
        return int(self.order_cfg["lot_size"]) * int(self.order_cfg["lot_count"])

    def __place_real_order(self, symbol, transaction_type, quantity, trade_type="", order_type="",
                            reason="", reference_price=0.0):
        """Places a real MARKET order via the broker -- LIVE mode only, and only when
        live_trading_enabled is true. Every call -- success or failure -- is logged to OrderLog as
        its own row. See vwappiercing_options' piercing_engine.py for the full rationale (identical
        mechanism, duplicated here since sd1sd2 is a separate submodule)."""
        broker = self.trade_utility.get_broker_utility()
        try:
            order_id = broker.place_order(
                tradingsymbol=symbol,
                transaction_type=transaction_type,
                quantity=quantity,
                product=self.order_cfg["product_type"],
                order_type=self.order_cfg["order_type"],
                market_type=self.__option_market_type(),
            )
        except Exception:
            print(self.logic_name, f": REAL ORDER EXCEPTION placing {transaction_type} {quantity} x {symbol}")
            traceback.print_exc()
            order_id = ""
        success = bool(order_id)
        print(self.logic_name, f": REAL ORDER {'PLACED' if success else 'FAILED'} -- "
             f"{transaction_type} {quantity} x {symbol}", f"order_id={order_id!r}" if success else "")
        self.obj_order_log_writer.write_order(order_log_row(
            timestamp=datetime.now().strftime("%H:%M:%S"),
            date=date.today().strftime("%Y-%m-%d"),
            future=self.future_symbol,
            option_name=symbol,
            trade_type=trade_type,
            order_type=order_type,
            transaction_type=transaction_type,
            quantity=quantity,
            order_id=order_id,
            status="PLACED" if success else "FAILED",
            reason=reason,
            reference_price=reference_price,
        ))
        return success, order_id

    def __select_option_by_premium(self, broker, option_type, entry_future_price):
        # Closest-to-ATM-in-band, not cheapest-in-band: a cheap/far-OTM contract's premium is
        # dominated by theta/IV noise and barely responds to the modest SD-sized moves this
        # strategy targets on the future, so a correct directional call can still lose money on the
        # option. The strike nearest the money (while still inside the premium band) has more
        # delta, so its premium actually tracks the underlying move that triggered the trade.
        underlying = CHAIN_UNDERLYING_SYMBOL.get(self.index_name, self.index_name)
        chain_df, _, _ = broker.getOptionChain(underlying)
        atm_strike = round(entry_future_price / self.strike_step) * self.strike_step
        return select_closest_to_atm_in_band(chain_df, option_type, atm_strike,
                                             self.target_premium_low, self.target_premium_high)

    def __select_historical_option(self, entry_future_price, option_type, ts):
        """
        BACKTEST only: scans candidate strikes OUTWARD from ATM (0, -1, +1, -2, +2, ...) and stops
        at the first one whose historical close falls inside the premium band -- i.e. the
        closest-to-ATM (highest-delta) contract the band allows, same selection criterion LIVE uses
        via select_closest_to_atm_in_band, not "cheapest" (see that function's docstring for why
        cheapest is the wrong criterion for this strategy).

        A miss on any ONE strike (no data, or the broker's own "Invalid symbol" response) is NOT
        treated as proof the whole expiry is delisted -- confirmed in practice: a single strike can
        come back with a flaky/invalid response while neighboring strikes of the identical contract
        resolve fine moments later in the same run. So every candidate is tried; only after the
        entire range comes up empty is "no option data" reported.
        """
        atm_strike = round(entry_future_price / self.strike_step) * self.strike_step
        trade_date = datetime.strptime(self.trade_date_str, "%Y-%m-%d")
        weekly_expiry = generate_weekly_expiry_dates(trade_date, 1)[0]
        is_month_expiry = weekly_expiry in generate_monthly_expiry_dates(trade_date, 1)

        # A heartbeat, not a per-request dump: each lookup below can silently retry several times
        # against the broker (rate-limit backoff) before returning, so with no progress signal at
        # all a healthy-but-slow scan is indistinguishable from a hung one.
        self.__log(f"[{ts}] ({option_type}) resolving option for entry -- scanning outward from ATM "
                  f"{atm_strike} for the first in-band strike (expiry {weekly_expiry})")
        scan_start = time.time()

        offsets = [0] + [offset for d in range(1, 21) for offset in (-d, d)]
        for i, offset in enumerate(offsets, start=1):
            strike = atm_strike + offset * self.strike_step
            symbol = self.broker.get_option_name(self.index_name, weekly_expiry, is_month_expiry, str(strike), option_type)
            price = self.__historical_option_close_near(symbol, ts)
            if i % 10 == 0:
                self.__log(f"[{ts}] ...still scanning ({i}/41 strikes checked, "
                          f"{time.time() - scan_start:.0f}s elapsed)")
            if price is None or price <= 0:
                continue
            if self.target_premium_low <= price <= self.target_premium_high:
                return symbol, price
        self.__log(f"[{ts}] No option found in {self.target_premium_low:.0f}-{self.target_premium_high:.0f} band "
                  f"for expiry {weekly_expiry} after checking all {len(offsets)} candidate strikes "
                  f"(contract may be delisted, or none traded in that band yet)")
        return None, 0.0

    def __historical_option_close_near(self, option_symbol, ts):
        if not option_symbol:
            return None
        str_from = f"{self.trade_date_str} {self.day_start_time}"
        time.sleep(self.historical_option_lookup_delay_seconds)
        data = self.broker.fetchOHLC(option_symbol, str_from, ts, interval=f"{self.candle_interval_minutes}minute",
                                     all_data=True, market_type=self.__option_market_type())
        if data is None or len(data) == 0:
            return None
        filtered = data[data[DATE_TIME].astype(str) <= ts]
        if len(filtered) == 0:
            return None
        return float(filtered.iloc[-1][CLOSE_PRICE])

    def __is_time_reached(self, str_time):
        target_time = datetime.strptime(str_time, "%H:%M:%S").time()
        return self.__now().time() >= target_time

    def __now(self):
        return self.test_clock if self.test_mode else datetime.now()

    def __advance_or_sleep(self, seconds):
        if self.test_mode:
            self.test_clock += timedelta(seconds=self.test_mode_step_seconds)
        else:
            time.sleep(seconds)

    # ------------------------------------------------------------------
    # BACKTEST driver
    # ------------------------------------------------------------------
    def run_backtest_day(self):
        """Returns (list_of_sd1sd2_trade_row, future_symbol, status) for one historical trading day."""
        broker = self.broker
        future_symbol, _ = pattern_rules.resolve_front_month_future_symbol(broker, self.index_name, self.trade_date_str)
        self.future_symbol = future_symbol

        candle_data = self.__load_test_day_candles(broker, self.candle_interval_minutes)
        if candle_data is None or len(candle_data) == 0:
            return [], future_symbol, STATUS_NO_DATA

        clock = datetime.strptime(f"{self.trade_date_str} {self.test_mode_start_time}", "%Y-%m-%d %H:%M:%S")
        day_end = datetime.strptime(f"{self.trade_date_str} 15:30:00", "%Y-%m-%d %H:%M:%S") \
            + timedelta(seconds=self.test_mode_step_seconds)
        while clock <= day_end:
            self.__run_pass(clock.strftime("%Y-%m-%d %H:%M:%S"))
            clock += timedelta(seconds=self.test_mode_step_seconds)

        for direction, ds in self.directions.items():
            if ds.state == STATE_IN_TRADE and ds.current_trade is not None:
                trade = ds.current_trade
                last_ts = str(candle_data.iloc[-1][DATE_TIME])
                last_close = float(candle_data.iloc[-1][CLOSE_PRICE])
                trade.exit3_eod.future_price = last_close
                trade.exit3_eod.option_price = self.__historical_option_close_near(ds.option_symbol, last_ts) or 0.0
                trade.mae, trade.mae_time = ds.mae, ds.mae_time
                trade.mfe, trade.mfe_time = ds.mfe, ds.mfe_time
                self.results.append(trade)
                self.on_trade_closed(trade)
                self.__log(f"End of day ({direction}): trade still open (SL not hit), closed at last price "
                          f"{last_close} ({self.__fmt_option_price(trade.exit3_eod.option_price)})")

        return self.results, future_symbol, STATUS_OK

    def __reset_direction_backtest(self, ds: _DirectionState):
        ds.current_trade = None
        ds.option_symbol = ""
        ds.order_quantity = 0
        ds.state = STATE_SEEK_ENTRY

    def __log(self, message):
        self.log_fn(message)

    # ------------------------------------------------------------------
    # entry
    # ------------------------------------------------------------------
    def __enter_trade(self, ds: _DirectionState, entry_future_price, ts, entry_row, vwap, sd):
        option_symbol, option_price = "", 0.0
        order_quantity = 0
        option_type = "CE" if ds.direction == "BUY" else "PE"
        if self.mode == Mode.LIVE and not self.test_mode:
            broker = self.trade_utility.get_broker_utility()
            option_symbol, option_price = self.__select_option_by_premium(broker, option_type, entry_future_price)
            if option_symbol is None:
                print(self.logic_name, f": ({ds.direction}) No option found near target premium band; dropping setup")
                return

            order_quantity = self.__order_quantity()
            if self.live_trading_enabled:
                order_placed, _ = self.__place_real_order(option_symbol, "BUY", order_quantity,
                                                           trade_type=ds.direction, order_type="Entry",
                                                           reference_price=option_price)
                if not order_placed:
                    print(self.logic_name, f": ({ds.direction}) Real BUY order FAILED for {option_symbol} "
                         f"-- dropping setup, no position was opened")
                    return
        else:
            option_symbol, option_price = self.__select_historical_option(entry_future_price, option_type, ts)
            if option_symbol is None:
                option_symbol, option_price = "", 0.0
                self.__log(f"[{ts}] ({ds.direction}) No option found in "
                          f"{self.target_premium_low:.0f}-{self.target_premium_high:.0f} band at entry -- "
                          f"option price data unavailable for this trade")

        trade = sd1sd2_trade_row()
        trade.date = self.session_date_str if self.mode == Mode.LIVE else self.trade_date_str
        trade.future = self.future_symbol
        trade.option_name = option_symbol
        trade.trade_type = ds.direction
        trade.entry_candle = self.__to_snapshot(entry_row, vwap) if entry_row is not None \
            else candle_snapshot(timestamp=ts, open=entry_future_price, high=entry_future_price,
                                 low=entry_future_price, close=entry_future_price, vwap=vwap)
        trade.entry_future_price = entry_future_price
        trade.entry_option_price = option_price
        trade.entry_timestamp = ts

        ds.current_trade = trade
        ds.option_symbol = option_symbol
        ds.order_quantity = order_quantity
        ds.mae = 0.0
        ds.mfe = 0.0
        ds.mae_time = ts
        ds.mfe_time = ts
        ds.state = STATE_IN_TRADE

        sl_level, exit1_level = self.__levels_for(ds.direction, vwap, sd)
        if self.mode == Mode.LIVE:
            if option_symbol and not self.test_mode:
                self.quotes_utility.add_stocks([option_symbol], [self.__option_market_type()])
            ds.last_logged_candle_ts = None
            print(self.logic_name, ": Entered paper trade", ds.direction, option_symbol, "@", option_price,
                 f"VWAP={vwap:.2f} SD={sd:.2f} SL={sl_level:.2f} Exit1={exit1_level:.2f} Exit2={vwap:.2f}")
        else:
            option_str = f"{option_symbol} @{option_price:.2f}" if option_symbol else "none found"
            self.__log(f"[{ts}] ({ds.direction}) ENTRY @ {entry_future_price} VWAP={vwap:.2f} SD={sd:.2f} "
                      f"SL={sl_level:.2f} Exit1={exit1_level:.2f} Exit2={vwap:.2f} Option={option_str}")

    def __update_mae_mfe_point(self, ds: _DirectionState, future_ltp, ts):
        excursion = (future_ltp - ds.current_trade.entry_future_price) if ds.direction == "BUY" \
            else (ds.current_trade.entry_future_price - future_ltp)
        if excursion < ds.mae:
            ds.mae, ds.mae_time = excursion, ts
        if excursion > ds.mfe:
            ds.mfe, ds.mfe_time = excursion, ts

    def __update_mae_mfe_range(self, ds: _DirectionState, high, low, ts):
        trade = ds.current_trade
        worst = (low - trade.entry_future_price) if ds.direction == "BUY" else (trade.entry_future_price - high)
        best = (high - trade.entry_future_price) if ds.direction == "BUY" else (trade.entry_future_price - low)
        if worst < ds.mae:
            ds.mae, ds.mae_time = worst, ts
        if best > ds.mfe:
            ds.mfe, ds.mfe_time = best, ts

    def __stamp_mae_mfe(self, ds: _DirectionState):
        ds.current_trade.mae = ds.mae
        ds.current_trade.mae_time = ds.mae_time
        ds.current_trade.mfe = ds.mfe
        ds.current_trade.mfe_time = ds.mfe_time

    def __snapshot_exit_hits(self, trade: sd1sd2_trade_row):
        return {field: getattr(trade, field).is_hit for field in ("exit1_hit", "exit2_hit")}

    def __fmt_option_price(self, price):
        return f"opt@{price:.2f}" if price and price > 0 else "opt@n/a"

    def __log_exit_breaches(self, ds: _DirectionState, was_hit):
        trade = ds.current_trade
        for field in ("exit1_hit", "exit2_hit"):
            hit_obj = getattr(trade, field)
            if was_hit[field] or not hit_obj.is_hit:
                continue
            label = self.exit_labels[field]
            opt_str = self.__fmt_option_price(hit_obj.option_price)
            note = "(REAL exit -- this closes the position)" if field == self.target_exit_field \
                else "(hypothesis only -- trade continues, only SL/the configured target exit closes it)"
            if self.mode == Mode.LIVE:
                print(self.logic_name, f": ({ds.direction}) {label} target BREACHED @ {hit_obj.future_price} ({opt_str})",
                     note)
            else:
                self.__log(f"[{hit_obj.timestamp}] ({ds.direction}) {label} target BREACHED @ {hit_obj.future_price} ({opt_str}) "
                          f"{note}")

    def __to_snapshot(self, row, vwap=None):
        v = row[VWAP] if (vwap is None and VWAP in row.index) else vwap
        return candle_snapshot(timestamp=str(row[DATE_TIME]), open=float(row[OPEN_PRICE]),
                               high=float(row[HIGH_PRICE]), low=float(row[LOW_PRICE]),
                               close=float(row[CLOSE_PRICE]), vwap=float(v) if v is not None else 0.0)


def _exit_pnl_points(trade: sd1sd2_trade_row, hit_obj: exit_hit):
    """Signed profit/loss in future-price points for a hit exit, relative to entry."""
    if trade.trade_type == "BUY":
        return hit_obj.future_price - trade.entry_future_price
    return trade.entry_future_price - hit_obj.future_price


def determine_best_case_exit(trade: sd1sd2_trade_row):
    candidates = [(label, _exit_pnl_points(trade, hit_obj))
                 for label, hit_obj in (("Exit1", trade.exit1_hit), ("Exit2", trade.exit2_hit))
                 if hit_obj.is_hit]
    if not candidates:
        return "SL"
    best_label, _ = max(candidates, key=lambda c: c[1])
    return best_label


def describe_exit_outcomes(trade: sd1sd2_trade_row):
    """
    Full description for the Best Case Exit column: profit/loss in points for every Exit1/Exit2
    that was hit, plus which one was best, each annotated with the option's own premium at that
    exit. "SL" if neither Exit1/Exit2 was ever hit before the trade closed.
    """
    parts = []
    best_label, best_pnl = None, None
    for label, hit_obj in (("Exit1", trade.exit1_hit), ("Exit2", trade.exit2_hit)):
        if not hit_obj.is_hit:
            continue
        pnl = _exit_pnl_points(trade, hit_obj)
        opt_str = f"(opt@{hit_obj.option_price:.2f})" if hit_obj.option_price > 0 else "(opt@n/a)"
        parts.append(f"{label}:{pnl:+.2f}pts{opt_str}")
        if best_pnl is None or pnl > best_pnl:
            best_label, best_pnl = label, pnl

    exits_desc = f"{', '.join(parts)} (Best: {best_label})" if parts else "SL"

    if trade.option_name:
        return f"{trade.option_name} entry@{trade.entry_option_price:.2f} | {exits_desc}"
    return f"{exits_desc} [No option data found]"
