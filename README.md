# SD1SD2

VWAP-mean-reversion strategy on the NIFTY future: VWAP is treated as the market's equilibrium, so
price pierced `entry_sd_multiplier` (default 2) standard deviations away from it is the entry
signal, expecting reversion back toward VWAP. Single-stage entry -- no piercing/reclaim/confirm
funnel like `vwappiercing_options`; the pierce itself is the entry.

- **BUY**: price pierces below `VWAP - 2SD` (oversold) -> buy a CE, expecting reversion up.
- **SELL**: price pierces above `VWAP + 2SD` (overbought) -> buy a PE, expecting reversion down.
- **SL**: `sl_sd_multiplier` (default 4) SD beyond entry, in the adverse direction.
- **Exit1**: `exit1_sd_multiplier` (default 1) SD from VWAP, in the trade's favor.
- **Exit2**: VWAP itself (multiplier 0) -- full reversion.

SD here means the rolling standard deviation of `(Close - VWAP)` over `sd_period` main-interval
candles, **not** a classic Bollinger band (which is stdev of Close around its own SMA) -- see
`Logic/pattern_rules.py::compute_sd`.

**All three levels are dynamic**, unlike `vwappiercing_options`' SL/Exit-1..3 (fixed at entry):
SL/Exit1/Exit2 are recomputed from the CURRENT VWAP/SD on every check, for as long as the trade
stays open. Exit2 is always "wherever VWAP is right now", not a price frozen at entry time.

Any trade still open at `force_exit_time` is force-closed at the prevailing price regardless of
SL/Exit1/Exit2 state.

**Everything is checked on the main-interval candle series only** (5-min, per the `VWAPSD1SD2`
sheet's `Config` tab) -- there's no separate 1-min series anywhere, unlike `vwappiercing_options`.
In BACKTEST/LIVE-test-mode replay, entry and SL/Exit1/Exit2 hits are both range-checks (High/Low
touching the level) against that same candle series; in real LIVE they're point-checks against the
live LTP between candle closes. A level is always computed from the candles closed *strictly
before* the one being checked (see `__bands_at`), so a candle's own not-yet-closed VWAP/SD is never
used to judge itself.

### Option strike selection: closest-to-ATM-in-band, not cheapest

Within `option_premium_band_low/high`, the engine picks the strike **closest to ATM**, not the
cheapest one (`Logic/option_selection.py::select_closest_to_atm_in_band` for LIVE,
`__select_historical_option`'s outward-from-ATM scan for BACKTEST). A cheap/far-OTM contract is
low-delta -- its premium barely responds to the modest SD-sized moves this strategy targets on the
future, so a correct directional call can still show up as an option *loss*, dominated by theta
decay/IV noise instead of the underlying move. Closest-to-ATM-in-band trades some premium cheapness
for a contract whose price actually tracks the future. The BACKTEST scan tries candidates outward
from ATM (0, -1, +1, -2, +2, ...) and stops at the first in-band one it finds, which also tends to
be faster than scanning all ~41 for the cheapest.

A single strike's lookup failing (no data, or the broker reporting "Invalid symbol") is **not**
treated as proof the whole expiry is delisted -- confirmed in practice, a strike can come back
flaky while a neighboring strike of the identical contract resolves fine moments later in the same
run. Every candidate in the scan range is tried regardless of earlier misses; "no option found in
band" is only reported once the entire range comes up empty.

### Reentry cooldown after a stop-out

Without a cooldown, a direction that's stopped out (SL) while price is still sitting beyond the
entry band would re-enter on literally the next candle, since the 2SD pierce condition is still
true -- during a stretch that isn't actually reverting, this produces a rapid string of SL-after-SL
trades on the same side. To prevent that: once a direction is stopped out, it's marked
**reentry-blocked** and won't be checked for a new entry again until price first pulls back to
within `reentry_sd_threshold` SD of VWAP (default 1) -- see `pattern_rules.reentry_cleared()`. Only
an SL close sets this; a winning Exit1/Exit2 close re-arms the direction immediately, since there's
no whipsaw to guard against there. Each direction (BUY/SELL) tracks its own cooldown independently.
While blocked, the seeking-entry log line is replaced with an explicit
`"reentry blocked after SL -- waiting for price to pull back within <N>SD of VWAP"` line, and a
`"reentry cooldown cleared"` line marks the moment it re-arms.

## Configuration (`Config/strategy_config.json`)

The single source of truth for every tunable trading parameter, shared identically by LIVE and
BACKTEST (`Config/config_loader.py::load_config()`, applied via `pattern_rules.configure()`). A
missing file or missing keys fall back to hardcoded defaults -- it can never crash the strategy.

| Key | Meaning |
|---|---|
| `day_start_time` | Session start used for backtest/test-mode option lookups |
| `entry_start_delay_minutes` | Minutes after execution start before VWAP/SD are trusted enough to seek entries |
| `entry_cutoff_time` | Stop seeking new entries after this time |
| `force_exit_time` | Force-close any open trade at this time regardless of exit state |
| `sd_period` | Rolling window (in main-interval candles) for the stdev of (Close-VWAP) |
| `entry_sd_multiplier` | Entry band, in SD from VWAP |
| `exit1_sd_multiplier` | Exit1 band, in SD from VWAP |
| `sl_sd_multiplier` | SL band, in SD from VWAP (beyond entry, adverse direction) |
| `reentry_sd_threshold` | After an SL stop-out, how close (in SD from VWAP) price must pull back to before that direction re-arms for a new entry |
| `option_premium_band_low`/`high` | Target option premium range for strike selection |
| `index_name`, `strike_step` | Underlying index and its option strike spacing |
| `test_mode_start_time`, `test_mode_step_seconds` | LIVE test mode's simulated clock |
| `historical_option_lookup_delay_seconds` | Pacing between historical option-price lookups (rate-limit avoidance) |
| `live_trading_enabled` | Master safety switch for real order placement (default `false`) |
| `target_exit` | Which of `"exit1"`/`"exit2"` also closes the position for real, alongside SL. `null` = SL only |
| `order.*` | order_type/product_type/lot_size/lot_count for real orders |

### Real order placement

Off by default (`live_trading_enabled: false`) -- LIVE runs fully paper-trade, logging every
setup/trade to `PaperTradeData` exactly as before. Set it to `true` to place real entry BUY / exit
SELL orders. SL is always a real exit once enabled; `target_exit` additionally makes Exit1 or
Exit2 real too, so the position closes on whichever hits first. Every order attempt (entry or
exit, success or failure) is logged to `OrderLog` as its own row. LIVE test mode
(`executor.py --test_mode true --date YYYY-MM-DD`) never places real orders regardless of this
setting -- it's a safe rehearsal of the live code path against historical data.

## Layout

```
sd1sd2/
  Config/
    strategy_config.json
    config_loader.py
  DataTypes/
    trade_data.py       # sd1sd2_trade_row, candle_snapshot, exit_hit, eod_exit
    order_log_data.py
  Logic/
    pattern_rules.py     # SD-band math, entry window timing, front-month future resolution
    sd1sd2_engine.py      # the shared LIVE/BACKTEST state machine
    sd1sd2.py             # LIVE entry point
    backtest_engine.py    # BACKTEST entry point
    option_selection.py
  UserInterface/
    adapter/...           # login/config adapters (gsheet-backed)
    gsheet/...             # login, config, paper_trade, backtest, order_log sheet writers
  interfaces.py           # ILogicInterface registered in executor.py's LOGIC_REGISTRY as "sd1sd2"
  run_backtest.py
```

Google Sheet: `VWAPSD1SD2`, tabs `Config` / `BrokerData` / `PaperTradeData` / `BackTestData` /
`OrderLog` -- same tab layout convention as `vwappiercing_options`' `VWAPPiercingOptions` sheet.
