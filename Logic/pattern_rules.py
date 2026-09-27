# -*- coding: utf-8 -*-
"""
pattern_rules.py

Pure, stateless SD1SD2 pattern predicates shared between the live SD1SD2Engine state machine and
offline testing/dry-run scripts. VWAP is treated as the market's equilibrium: price piercing
ENTRY_SD_MULTIPLIER standard deviations away from it is the entry signal (expecting reversion back
toward VWAP), Exit1/Exit2/SL are all levels of the same "current VWAP +/- k * current SD" shape,
just with different k -- see compute_sd_bands.
"""
import calendar
from datetime import datetime, timedelta

from DataTypes.defines import *
from Utility.utility import generate_monthly_expiry_dates

# New entries are only looked for once VWAP/SD have had time to settle (entry_start_delay_minutes
# after execution start) and stop being looked for at ENTRY_CUTOFF_TIME. Unlike vwappiercing_options
# there's no multi-stage setup to abandon here (entry is single-stage) -- SEEK_ENTRY just idles
# once the window closes, until the next session.
ENTRY_START_DELAY_MINUTES = 15
ENTRY_CUTOFF_TIME = "14:00:00"

# Any trade still open at this time is force-closed at the prevailing price, regardless of
# SL/Exit1/Exit2 state.
FORCE_EXIT_TIME = "14:50:00"

# SD band shape: current VWAP +/- multiplier * rolling stdev of (Close - VWAP) over SD_PERIOD
# main-interval candles. All three (entry, Exit1, SL) share this shape, just with a different
# multiplier -- Exit2 is simply "current VWAP" (multiplier 0).
SD_PERIOD = 20
ENTRY_SD_MULTIPLIER = 2.0
EXIT1_SD_MULTIPLIER = 1.0
SL_SD_MULTIPLIER = 4.0

# After a stop-out (SL), a direction won't re-arm for a new entry until price has pulled back to
# within this many SD of VWAP -- see SD1SD2Engine's reentry-cooldown handling. Does not apply after
# a winning target-exit close.
REENTRY_SD_THRESHOLD = 1.0


def configure(cfg):
    """
    Overrides this module's tunable constants from the strategy config dict (see
    Config/config_loader.py::load_config()). Called once by SD1SD2Engine.__init__, for both LIVE
    and BACKTEST, so a config change can never apply to only one of them.
    """
    global ENTRY_START_DELAY_MINUTES, ENTRY_CUTOFF_TIME, FORCE_EXIT_TIME, \
        SD_PERIOD, ENTRY_SD_MULTIPLIER, EXIT1_SD_MULTIPLIER, SL_SD_MULTIPLIER, REENTRY_SD_THRESHOLD
    ENTRY_START_DELAY_MINUTES = cfg["entry_start_delay_minutes"]
    ENTRY_CUTOFF_TIME = cfg["entry_cutoff_time"]
    FORCE_EXIT_TIME = cfg["force_exit_time"]
    SD_PERIOD = cfg["sd_period"]
    ENTRY_SD_MULTIPLIER = cfg["entry_sd_multiplier"]
    EXIT1_SD_MULTIPLIER = cfg["exit1_sd_multiplier"]
    SL_SD_MULTIPLIER = cfg["sl_sd_multiplier"]
    REENTRY_SD_THRESHOLD = cfg["reentry_sd_threshold"]


def compute_entry_start_time(execution_start_time):
    """execution_start_time: 'HH:MM:SS'. Returns 'HH:MM:SS', ENTRY_START_DELAY_MINUTES after it."""
    return (datetime.strptime(execution_start_time, "%H:%M:%S")
            + timedelta(minutes=ENTRY_START_DELAY_MINUTES)).strftime("%H:%M:%S")


def is_entry_window_open(check_time, entry_start_time):
    """check_time/entry_start_time as zero-padded 'HH:MM:SS' strings (safe to compare lexically)."""
    return entry_start_time <= check_time < ENTRY_CUTOFF_TIME


def is_force_exit_time_reached(check_time):
    """check_time as a zero-padded 'HH:MM:SS' string (safe to compare lexically)."""
    return check_time >= FORCE_EXIT_TIME


def time_of_day(date_time_str):
    """Extracts 'HH:MM:SS' from a full 'DD-MM-YYYY HH:MM:SS' (or similar) timestamp string."""
    return date_time_str.split(" ")[-1]


def compute_sd(candle_data, period=None):
    """
    Rolling standard deviation of (Close - VWAP) over `period` main-interval candles (candle_data
    must already carry a VWAP column, e.g. via Utility.utility.compute_vwap). Returns a pandas
    Series aligned with candle_data's index; NaN for the first (period-1) rows.
    """
    p = period or SD_PERIOD
    return (candle_data[CLOSE_PRICE] - candle_data[VWAP]).rolling(window=p).std()


def sd_bands_at(vwap, sd):
    """
    (entry_upper, entry_lower, exit1_upper, exit1_lower, sl_upper, sl_lower) for a given VWAP/SD
    pair -- "upper" is the level a SELL setup (price above VWAP, expecting reversion down) uses,
    "lower" is what a BUY setup (price below VWAP, expecting reversion up) uses.
    """
    return (
        vwap + ENTRY_SD_MULTIPLIER * sd, vwap - ENTRY_SD_MULTIPLIER * sd,
        vwap + EXIT1_SD_MULTIPLIER * sd, vwap - EXIT1_SD_MULTIPLIER * sd,
        vwap + SL_SD_MULTIPLIER * sd, vwap - SL_SD_MULTIPLIER * sd,
    )


def reentry_cleared(direction, price, vwap, sd):
    """
    True once `price` has pulled back to within REENTRY_SD_THRESHOLD SD of VWAP on the side
    `direction`'s setup pierced from -- BUY (pierced below VWAP): price has risen back above
    VWAP - REENTRY_SD_THRESHOLD*sd; SELL (pierced above): price has fallen back below
    VWAP + REENTRY_SD_THRESHOLD*sd. Used to re-arm a direction after an SL stop-out instead of
    letting it re-enter immediately while price is still sitting beyond the entry band.
    """
    if direction == "BUY":
        return price >= vwap - REENTRY_SD_THRESHOLD * sd
    return price <= vwap + REENTRY_SD_THRESHOLD * sd


def _is_in_last_week_of_month(trade_date):
    """True if trade_date falls within the last 7 calendar days of its month."""
    last_day = calendar.monthrange(trade_date.year, trade_date.month)[1]
    return trade_date.day > last_day - 7


def resolve_front_month_future_symbol(broker, index_name, trade_date_str):
    """
    The monthly future contract that was front-month on trade_date_str (no network call). Same
    resolution vwappiercing_options uses (see its Logic/pattern_rules.py for the full rationale) --
    duplicated here rather than imported cross-submodule so sd1sd2 stays runnable on its own.
    """
    trade_date = datetime.strptime(trade_date_str, "%Y-%m-%d")

    lookup_date = trade_date
    if _is_in_last_week_of_month(trade_date):
        lookup_date = (trade_date.replace(day=28) + timedelta(days=4)).replace(day=1)

    for expiry_str in generate_monthly_expiry_dates(lookup_date, 1):
        if datetime.strptime(expiry_str, "%d-%b-%Y") >= lookup_date:
            return broker.get_future_name(index_name, expiry_str), expiry_str
    last_expiry = generate_monthly_expiry_dates(lookup_date, 1)[-1]
    return broker.get_future_name(index_name, last_expiry), last_expiry
