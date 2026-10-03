# -*- coding: utf-8 -*-
"""
option_selection.py

Pure premium-matching logic shared between the live SD1SD2Engine and offline testing scripts:
given a list of candidate option contracts and their quotes, pick the one whose premium is closest
to (or cheapest within) the target band.
"""


def select_by_premium(lst_contracts, dict_quotes, target_low=100.0, target_high=110.0):
    """
    lst_contracts: list of (tradingsymbol, strike_price, option_type) tuples.
    dict_quotes: {tradingsymbol: quote_data} as returned by broker.get_quotes().
    Returns (best_symbol, best_price) or (None, 0.0) if nothing usable is found.
    """
    best_symbol = None
    best_diff = None
    best_price = 0.0
    for tsym, strike, opt in lst_contracts:
        if tsym not in dict_quotes:
            continue
        ltp = dict_quotes[tsym].ltp
        if ltp <= 0:
            continue
        if target_low <= ltp <= target_high:
            diff = 0.0
        else:
            diff = min(abs(ltp - target_low), abs(ltp - target_high))
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_symbol = tsym
            best_price = ltp
    return best_symbol, best_price


def select_cheapest_in_band(chain_df, option_type, band_low=80.0, band_high=130.0,
                            symbol_col="symbol", type_col="option_type", ltp_col="ltp"):
    """
    chain_df: a DataFrame as returned by a broker's getOptionChain() (Fyers-style) -- one row per
    contract, plus a non-option underlying/index row (option_type=="" there) that gets excluded by
    the option_type filter below. Only ever considers contracts strictly inside [band_low, band_high]
    and picks the lowest-premium (cheapest) one among them. Returns (symbol, price), or (None, 0.0)
    if the chain is empty/missing or nothing in the band qualifies.

    NOTE: deliberately unused by SD1SD2Engine (see select_closest_to_atm_in_band below) --
    "cheapest" biases toward far-OTM, low-delta contracts whose premium barely responds to the
    modest SD-sized moves this strategy targets, so a correct directional call on the future can
    still lose money on the option (theta/IV noise dominates a low-delta contract's P&L). Kept here
    since it's still a reasonable choice for a strategy that doesn't care about delta/moneyness.
    """
    if chain_df is None or len(chain_df) == 0:
        return None, 0.0
    if type_col not in chain_df.columns or ltp_col not in chain_df.columns or symbol_col not in chain_df.columns:
        return None, 0.0

    candidates = chain_df[(chain_df[type_col] == option_type) &
                          (chain_df[ltp_col] >= band_low) & (chain_df[ltp_col] <= band_high)]
    if candidates.empty:
        return None, 0.0

    best_row = candidates.loc[candidates[ltp_col].idxmin()]
    return str(best_row[symbol_col]), float(best_row[ltp_col])


def select_closest_to_atm_in_band(chain_df, option_type, atm_strike, band_low=80.0, band_high=130.0,
                                  symbol_col="symbol", type_col="option_type", ltp_col="ltp",
                                  strike_col="strike_price"):
    """
    Same candidate filtering as select_cheapest_in_band (option_type match, premium inside
    [band_low, band_high]), but picks the contract whose strike is CLOSEST to atm_strike instead of
    the cheapest one -- i.e. the highest-delta contract the premium band still allows. A strike
    nearer the money responds much more to the underlying's actual move, which matters here because
    this strategy's SD-based exits are sized off the FUTURE's price action, not the option's --
    a far-OTM "cheap" contract can see its premium dominated by theta/IV noise even when the
    underlying move is exactly right. Returns (symbol, price), or (None, 0.0) if the chain is
    missing/empty, lacks a strike column, or nothing in the band qualifies.
    """
    if chain_df is None or len(chain_df) == 0:
        return None, 0.0
    required = (type_col, ltp_col, symbol_col, strike_col)
    if any(col not in chain_df.columns for col in required):
        return None, 0.0

    candidates = chain_df[(chain_df[type_col] == option_type) &
                          (chain_df[ltp_col] >= band_low) & (chain_df[ltp_col] <= band_high)]
    if candidates.empty:
        return None, 0.0

    distance = (candidates[strike_col] - atm_strike).abs()
    best_row = candidates.loc[distance.idxmin()]
    return str(best_row[symbol_col]), float(best_row[ltp_col])
