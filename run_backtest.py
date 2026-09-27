# Run: python -m BusinessLogic.sd1sd2.run_backtest --key <path> [--days 30 | --start-date YYYY-MM-DD --end-date YYYY-MM-DD] [--interval 5]
# -*- coding: utf-8 -*-
"""
run_backtest.py

Replays a range of trading days of NIFTY future candles through the SD1SD2 pattern (single-stage
entry at the 2SD band -> SL(3SD)/Exit1(1SD)/Exit2(VWAP), entry-window gated, multiple trades/day)
and writes one row per detected trade to the BackTestData tab of the VWAPSD1SD2 Google Sheet. The
actual replay logic lives in Logic/backtest_engine.py, shared with the live engine so the two tools
can't drift apart.

Date range: either --days N (last N calendar days up to yesterday, default 30) or an explicit
--start-date/--end-date pair (both required together, inclusive). Only actual NSE trading days in
that range are replayed.

Two extra columns are appended at the end, BackTestData-only: Interval (the candle interval this
run used) and Best Case Exit -- profit/loss in points for whichever of Exit1/Exit2 was hit before
the trade closed, or "SL" if neither was ever hit.
"""
import argparse
from datetime import datetime, timedelta

import pandas_market_calendars as mcal

from BusinessLogic.sd1sd2.UserInterface.gsheet.login.login import UserInterfaceLogin
from BusinessLogic.sd1sd2.UserInterface.gsheet.config.config import UserInterfaceConfig
from BusinessLogic.sd1sd2.UserInterface.gsheet.backtest.backtest import UserInterfaceBackTest
from BusinessLogic.sd1sd2.Logic.backtest_engine import run_backtest_for_day, describe_exit_outcomes, STATUS_NO_DATA
from BrokerUtility.broker_platform.zebu.zebumynt_utility import zebumynt_utitlity
from BrokerUtility.pal.utility_manager import *

def get_trading_days_lookback(lookback_days):
    end_date = datetime.now().date() - timedelta(days=1)
    start_date = end_date - timedelta(days=lookback_days)
    return get_trading_days_between(start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))


def get_trading_days_between(start_date_str, end_date_str):
    schedule = mcal.get_calendar('NSE').schedule(start_date=start_date_str, end_date=end_date_str)
    return [ts.strftime("%Y-%m-%d") for ts in schedule.index]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest the SD1SD2 pattern over a range of trading days")
    parser.add_argument("--key", type=str, required=True, help="Path to the service account json file")
    parser.add_argument("--days", type=int, default=None,
                        help="Calendar-day lookback from yesterday (default: 30 if --start-date/--end-date not given)")
    parser.add_argument("--start-date", type=str, default=None, help="Range start, YYYY-MM-DD (inclusive)")
    parser.add_argument("--end-date", type=str, default=None, help="Range end, YYYY-MM-DD (inclusive)")
    parser.add_argument("--interval", type=int, default=None,
                        help="Candle interval in minutes (default: read from Config.candle_interval)")
    parser.add_argument("--index", type=str, default="NIFTY", help="Index name (default: NIFTY)")
    args = parser.parse_args()

    if bool(args.start_date) != bool(args.end_date):
        parser.error("--start-date and --end-date must be given together")
    if args.days is not None and args.start_date is not None:
        parser.error("use either --days or --start-date/--end-date, not both")

    login = UserInterfaceLogin(args.key).get_data()
    obj_utility_manager: utility_manager = utility_manager()
    broker = obj_utility_manager.get_utility_object(login).get_broker_utility()

    candle_interval = args.interval
    if candle_interval is None:
        candle_interval = int(UserInterfaceConfig(args.key).get_data().candle_interval)
    print("Using candle interval:", candle_interval, "minutes")

    if args.start_date:
        trading_days = get_trading_days_between(args.start_date, args.end_date)
    else:
        trading_days = get_trading_days_lookback(args.days if args.days is not None else 30)

    if not trading_days:
        print("No trading days found in the given range.")
        raise SystemExit(1)
    print(f"Backtesting {len(trading_days)} trading days: {trading_days[0]} -> {trading_days[-1]}\n")

    writer = UserInterfaceBackTest(args.key)

    written, no_entry_days, no_data_days = 0, [], []

    for trade_date in trading_days:
        # write each trade to the sheet the moment it closes, instead of waiting for the whole
        # day's replay to finish -- a volatile day with many entries can take a long time to fully
        # replay (each entry re-scans ~41 strikes historically), so this is the only way progress
        # is visible before the day completes.
        def _on_trade_closed(result, trade_date=trade_date):
            global written
            exit_outcomes = describe_exit_outcomes(result)
            writer.write_trade(result, candle_interval, exit_outcomes)
            written += 1
            sl_note = "SL hit" if result.sl_hit.is_hit else "still open at EOD"
            print(f"{trade_date}: {result.trade_type} @ {result.entry_future_price} "
                 f"({sl_note}, exits: {exit_outcomes}) -- written to BackTestData")

        try:
            results, future_symbol, status = run_backtest_for_day(broker, args.index, trade_date, candle_interval,
                                                                   log_fn=print, on_trade_closed=_on_trade_closed)
        except Exception as e:
            print(f"{trade_date}: exception during backtest -- {e}")
            no_data_days.append(trade_date)
            continue

        if status == STATUS_NO_DATA:
            print(f"{trade_date} ({future_symbol}): no candle data -- contract likely expired/delisted, skipping")
            no_data_days.append(trade_date)
        elif not results:
            print(f"{trade_date} ({future_symbol}): no valid entry")
            no_entry_days.append(trade_date)

    print(f"\nDone. {written} trade(s) written to BackTestData, "
         f"{len(no_entry_days)} day(s) with no entry, {len(no_data_days)} day(s) skipped (no data).")
    if no_data_days:
        print("Skipped (no data):", ", ".join(no_data_days))
