# -*- coding: utf-8 -*-
"""
config_loader.py

Loads Config/strategy_config.json once and hands back a plain dict, with hardcoded fallback
defaults (DEFAULTS below) for every key the file omits, and for the file itself if it's missing
entirely. A missing or partial config file can never crash the strategy or silently disable it --
it just runs with these defaults.

This is the single source of truth for every tunable trading parameter shared by LIVE and
BACKTEST (SD-band period/multipliers, entry window timing, the option premium band, real
order-placement settings). Pure implementation details that aren't meant to be tuned by someone
editing a config file (state name strings, internal helper constants) stay as plain code constants
and are NOT in here.
"""
import json
import os

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG_PATH = os.path.join(_CONFIG_DIR, "strategy_config.json")

DEFAULTS = {
    "day_start_time": "09:15:00",
    "entry_start_delay_minutes": 15,
    "entry_cutoff_time": "14:00:00",
    "force_exit_time": "14:50:00",
    # SD bands: rolling stdev of (Close - VWAP) over sd_period main-interval candles, added to/
    # subtracted from the CURRENT VWAP -- i.e. VWAP is the moving equilibrium, and all three levels
    # below float with it and with the SD estimate for as long as a trade is open (see "dynamic
    # exits" in sd1sd2_engine.py).
    "sd_period": 20,
    "entry_sd_multiplier": 2.0,
    "exit1_sd_multiplier": 1.0,
    "sl_sd_multiplier": 4.0,
    # After a stop-out, a direction won't re-arm for a new entry until price has pulled back to
    # within this many SD of VWAP -- prevents "stopped out, still beyond 2SD, immediately re-enters"
    # whipsaw during a stretch that isn't actually reverting. Does not apply to a real (winning)
    # target-exit close, only to SL.
    "reentry_sd_threshold": 1.0,
    "option_premium_band_low": 80.0,
    "option_premium_band_high": 130.0,
    "index_name": "NIFTY",
    "strike_step": 50,
    "test_mode_start_time": "09:16:15",
    "test_mode_step_seconds": 60,
    "historical_option_lookup_delay_seconds": 0.5,
    # safety: real orders are placed ONLY when this is explicitly true. Absent/false keeps LIVE
    # fully paper-trade.
    "live_trading_enabled": False,
    # which of Exit1(1SD)/Exit2(VWAP) ALSO closes the position for real, alongside SL (SL is
    # always real regardless of this once live_trading_enabled is true). None/null = SL is the
    # only real exit; the other stays a logged hypothesis only.
    "target_exit": None,
    "order": {
        "order_type": "MARKET",
        "product_type": "MIS",
        "lot_size": 65,  # NSE-fixed NIFTY lot size
        "lot_count": 1,
    },
}


def load_config(path=None):
    """
    Returns the strategy config dict: DEFAULTS with strategy_config.json's values overlaid on top.
    Missing keys, a missing "order" sub-key, or a missing file entirely all fall back to DEFAULTS.
    """
    cfg = dict(DEFAULTS)
    cfg["order"] = dict(DEFAULTS["order"])

    config_path = path or _DEFAULT_CONFIG_PATH
    if not os.path.exists(config_path):
        return cfg

    with open(config_path) as f:
        loaded = json.load(f)

    for key, value in loaded.items():
        if key == "order" and isinstance(value, dict):
            cfg["order"].update(value)
        else:
            cfg[key] = value
    return cfg
