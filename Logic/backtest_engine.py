# -*- coding: utf-8 -*-
"""
backtest_engine.py

Thin entry point for the historical backtest replay. The actual state machine lives in
sd1sd2_engine.py's SD1SD2Engine, shared verbatim with the live engine (Logic/sd1sd2.py) so a rule
change can't require touching two places and drifting between them -- only this file's job is to
adapt that shared engine's BACKTEST mode to the run_backtest_for_day(...) function signature
run_backtest.py expects.
"""
from .sd1sd2_engine import (
    SD1SD2Engine, Mode, STATUS_NO_DATA, STATUS_OK,
    determine_best_case_exit, describe_exit_outcomes, _exit_pnl_points,
)
from .pattern_rules import resolve_front_month_future_symbol


def run_backtest_for_day(broker, index_name, trade_date_str, candle_interval_minutes, log_fn=None,
                         on_trade_closed=None):
    """
    Returns (list_of_sd1sd2_trade_row, future_symbol, status) for one trading day.
    log_fn, if given, is called with a string for each pattern event (seeking-entry/ENTRY/SL-close)
    -- pass print for a verbose dry-run trace.
    on_trade_closed, if given, is called with each sd1sd2_trade_row the moment it closes (not
    batched until the whole day finishes) -- a day with many entries can take a long time to fully
    replay (each entry re-scans ~41 strikes historically), so pass this if you want to see/write
    trades as they happen rather than waiting on the returned list.
    """
    engine = SD1SD2Engine(mode=Mode.BACKTEST, broker=broker, index_name=index_name,
                          trade_date_str=trade_date_str, candle_interval_minutes=candle_interval_minutes,
                          log_fn=log_fn, on_trade_closed=on_trade_closed)
    return engine.run_backtest_day()
