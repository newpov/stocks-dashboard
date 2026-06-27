"""v2.4 equal-weight position model.

Each position (ticker) is normalized to a single 1-unit holding regardless of
how many transaction rows it has, so a stock split / heavy scale-in (e.g. KLAC
logged as 10 share-grants) can no longer inflate its weight or drag the basket
return down. The basket MTM line is the equal-weight mean of per-position
returns. See CHANGELOG v2.4.
"""
import numpy as np
import pandas as pd

import build


def _bdates(n, start="2025-01-02"):
    return pd.bdate_range(start, periods=n)


def test_txn_prices_vectorized_matches_loop():
    """C2: the vectorized _txn_prices must be byte-identical to mapping the scalar
    _txn_price over each date — including exact matches, non-trading days, dates
    before the series starts, after it ends, and duplicate same-day rows."""
    idx = pd.bdate_range("2025-01-06", periods=20)              # Mon-start business days
    s = pd.Series(np.linspace(100.0, 200.0, 20), index=idx)
    s.iloc[5] = np.nan                                          # a gap to skip over
    dates = pd.Series(pd.to_datetime([
        "2024-12-31",        # before series start -> NaN
        "2025-01-06",        # exact first trading day
        "2025-01-11",        # a Saturday -> nearest prior close
        "2025-01-13",        # exact match near the NaN gap
        "2025-01-13",        # duplicate same-day row
        "2025-03-01",        # after series end -> last close
    ]))
    want = dates.apply(lambda d: build._txn_price(d, s))
    got = build._txn_prices(dates, s)
    pd.testing.assert_series_equal(got, want, check_names=False)


def test_txn_prices_empty_series_all_nan():
    dates = pd.Series(pd.to_datetime(["2025-01-06", "2025-02-01"]))
    got = build._txn_prices(dates, pd.Series(dtype=float))
    assert got.isna().all()


def test_many_buys_collapse_to_one_unit():
    """10 buy rows -> 1 unit held, weight 1, but raw buy count preserved."""
    dates = _bdates(30)
    px = pd.DataFrame({"KLAC": np.linspace(100.0, 120.0, 30)}, index=dates)
    txns = pd.DataFrame({
        "ticker": ["KLAC"] * 10,
        "date": list(dates[:10]),
        "action": ["BUY"] * 10,
        "shares": [1.0] * 10,
    })
    pos = build.build_positions(txns, px)
    r = pos.loc["KLAC"]
    assert r["status"] == "open"
    assert r["shares_held"] == 1.0
    assert r["weight"] == 1.0           # equal weight, NOT 10x
    assert r["total_bought"] == 1.0
    assert r["total_sold"] == 0.0
    assert r["n_buys"] == 10            # raw count kept for the tooltip
    assert r["n_sells"] == 0


def test_closed_position_is_one_unit_sold():
    dates = _bdates(30)
    px = pd.DataFrame({"AAA": np.linspace(100.0, 110.0, 30)}, index=dates)
    txns = pd.DataFrame({
        "ticker": ["AAA", "AAA", "AAA"],
        "date": [dates[0], dates[5], dates[20]],
        "action": ["BUY", "BUY", "SELL"],
        "shares": [1.0, 1.0, 1.0],
    })
    pos = build.build_positions(txns, px)
    r = pos.loc["AAA"]
    assert r["status"] == "closed"
    assert r["shares_held"] == 0.0
    assert r["weight"] == 0.0
    assert r["total_sold"] == 1.0
    assert r["n_sells"] == 1
    avg_buy = float(np.mean([px["AAA"].iloc[0], px["AAA"].iloc[5]]))
    avg_sell = float(px["AAA"].iloc[20])
    # realized P&L is per-unit (sold exactly 1 unit)
    assert abs(r["realized_pnl"] - (avg_sell - avg_buy)) < 1e-6


def test_total_invested_is_per_unit_cost_basis():
    """total_invested is the avg buy price of a single unit (not summed rows)."""
    dates = _bdates(20)
    px = pd.DataFrame({"BBB": np.full(20, 50.0)}, index=dates)
    txns = pd.DataFrame({
        "ticker": ["BBB"] * 4,
        "date": list(dates[:4]),
        "action": ["BUY"] * 4,
        "shares": [1.0] * 4,
    })
    pos = build.build_positions(txns, px)
    assert abs(pos.loc["BBB"]["total_invested"] - 50.0) < 1e-9


def test_basket_mtm_is_equal_weight_mean():
    """A 10-buy flat name must NOT dominate; basket = mean of per-name returns."""
    dates = _bdates(20)
    px = pd.DataFrame({
        "WIN":  np.linspace(100.0, 200.0, 20),   # +100% by end
        "FLAT": np.full(20, 100.0),              # 0%
        "BIG":  np.full(20, 100.0),              # 0%, but bought 10x
    }, index=dates)
    txns = pd.DataFrame({
        "ticker": ["WIN", "FLAT"] + ["BIG"] * 10,
        "date":   [dates[0], dates[0]] + list(dates[:10]),
        "action": ["BUY", "BUY"] + ["BUY"] * 10,
        "shares": [1.0] * 12,
    })
    basket = build.compute_basket_mtm_series(txns, px)
    # equal-weight mean of (+100, 0, 0) = +33.33, regardless of BIG's row count
    assert abs(basket.iloc[-1] - (100.0 / 3.0)) < 0.5


def test_basket_mtm_freezes_closed_position_at_realized():
    """After a position is sold, its contribution freezes at the realized return."""
    dates = _bdates(20)
    win = np.concatenate([np.linspace(100.0, 150.0, 11),   # +50% at sell (idx 10)
                          np.linspace(150.0, 300.0, 9)])    # keeps ripping after
    px = pd.DataFrame({"SOLD": win, "HODL": np.full(20, 100.0)}, index=dates)
    txns = pd.DataFrame({
        "ticker": ["SOLD", "SOLD", "HODL"],
        "date":   [dates[0], dates[10], dates[0]],
        "action": ["BUY", "SELL", "BUY"],
        "shares": [1.0, 1.0, 1.0],
    })
    basket = build.compute_basket_mtm_series(txns, px)
    # SOLD frozen at +50, HODL at 0 -> mean 25, even though SOLD's price doubled
    assert abs(basket.iloc[-1] - 25.0) < 0.5


# --- v2.7 cost-basis reset on a full exit + re-entry -------------------------

def test_rebuy_after_full_exit_resets_baseline():
    """Bought, fully sold, rebought later: baseline = the RE-BUY price, not the
    blend of the long-closed original and the re-buy. (CSCO bug)"""
    dates = _bdates(60)
    px = pd.DataFrame({"CSCO": np.concatenate([
        np.full(20, 100.0),               # original era ~100
        np.linspace(100.0, 200.0, 20),    # ramp
        np.full(20, 200.0),               # re-buy era ~200, flat after
    ])}, index=dates)
    txns = pd.DataFrame({
        "ticker": ["CSCO"] * 3,
        "date": [dates[0], dates[2], dates[45]],     # buy@100, SELL all@100, rebuy@200
        "action": ["BUY", "SELL", "BUY"],
        "shares": [10.0, 10.0, 10.0],                # full exit, then re-entry
    })
    r = build.build_positions(txns, px).loc["CSCO"]
    assert r["status"] == "open"
    assert abs(r["baseline"] - 200.0) < 1e-6         # re-buy price (NOT mean(100,200)=150)
    assert abs(r["total_pct"]) < 1e-6                # flat since re-buy
    assert pd.Timestamp(r["baseline_date"]) == dates[45]   # chart starts at the re-buy
    assert abs(r["unrealized_pnl"]) < 1e-6           # held flat -> no paper gain


def test_partial_trim_does_not_reset_basis():
    """A partial sell (net stays > 0) is NOT a full exit, so both buys keep
    contributing to the cost basis."""
    dates = _bdates(40)
    px = pd.DataFrame({"AAA": np.concatenate([np.full(20, 100.0),
                                              np.full(20, 150.0)])}, index=dates)
    txns = pd.DataFrame({
        "ticker": ["AAA"] * 3,
        "date": [dates[0], dates[5], dates[25]],
        "action": ["BUY", "SELL", "BUY"],
        "shares": [10.0, 3.0, 5.0],                  # buy10, trim3 (net7), add5
    })
    r = build.build_positions(txns, px).loc["AAA"]
    assert r["status"] == "open"
    # one cycle -> qty-weighted avg of both buys: (10*100 + 5*150)/15
    assert abs(r["baseline"] - (1750.0 / 15.0)) < 1e-6


# --- v2.9.4: hero chart uses the SAME active-cycle basis as the table ---------

def test_basket_mtm_matches_table_for_rebought_name():
    """H5: a sold-then-rebought name must rebase the basket line at the RE-BUY
    price (active cycle), exactly like the holdings table baseline — not at the
    all-time mean of every buy. Pre-fix the chart said ~+33% while the table said
    flat (0%) for the same name."""
    dates = _bdates(60)
    px = pd.DataFrame({"CSCO": np.concatenate([
        np.full(20, 100.0),               # original era
        np.linspace(100.0, 200.0, 20),    # ramp
        np.full(20, 200.0),               # re-buy era, flat
    ])}, index=dates)
    txns = pd.DataFrame({
        "ticker": ["CSCO"] * 3,
        "date": [dates[0], dates[2], dates[45]],   # buy@100, full SELL@100, rebuy@200
        "action": ["BUY", "SELL", "BUY"],
        "shares": [10.0, 10.0, 10.0],
    })
    pos = build.build_positions(txns, px).loc["CSCO"]
    basket = build.compute_basket_mtm_series(txns, px)
    # single open position -> basket final == its table total_pct (both rebased at
    # the re-buy 200, flat). The all-time-mean bug gave ~+33%.
    assert abs(basket.iloc[-1] - pos["total_pct"]) < 0.5
    assert abs(basket.iloc[-1]) < 0.5


# --- v2.4 value-weight mode (opt-in, needs real share quantities) ------------

def test_value_mode_uses_real_shares():
    dates = _bdates(20)
    px = pd.DataFrame({"AAA": np.full(20, 100.0)}, index=dates)
    txns = pd.DataFrame({
        "ticker": ["AAA", "AAA"],
        "date": [dates[0], dates[1]],
        "action": ["BUY", "BUY"],
        "shares": [3.0, 7.0],            # real quantities, not 1-per-row
    })
    pos = build.build_positions(txns, px, mode="value")
    r = pos.loc["AAA"]
    assert r["shares_held"] == 10.0       # real net shares (not collapsed to 1)
    assert r["total_invested"] == 1000.0  # 10 shares x 100
    assert r["weight"] == 1000.0          # capital weight, not 1.0


def test_value_mode_basket_is_capital_weighted():
    dates = _bdates(10)
    px = pd.DataFrame({
        "BIG": np.linspace(100.0, 110.0, 10),   # +10%, big capital
        "SML": np.linspace(100.0, 200.0, 10),   # +100%, tiny capital
    }, index=dates)
    txns = pd.DataFrame({
        "ticker": ["BIG", "SML"],
        "date":   [dates[0], dates[0]],
        "action": ["BUY", "BUY"],
        "shares": [100.0, 1.0],          # BIG is ~100x the capital of SML
    })
    val = build.compute_basket_mtm_series(txns, px, mode="value")
    eq = build.compute_basket_mtm_series(txns, px, mode="equal")
    assert val.iloc[-1] < 20             # capital-weighted -> dominated by BIG (+10%)
    assert eq.iloc[-1] > 50              # equal-weighted -> mean of (+10, +100) ~ +55


def test_equal_mode_is_the_default():
    dates = _bdates(20)
    px = pd.DataFrame({"AAA": np.full(20, 100.0)}, index=dates)
    txns = pd.DataFrame({
        "ticker": ["AAA", "AAA"], "date": [dates[0], dates[1]],
        "action": ["BUY", "BUY"], "shares": [3.0, 7.0],
    })
    # No mode arg -> WEIGHT_MODE default ("equal") -> collapses to 1 unit.
    assert build.build_positions(txns, px).loc["AAA"]["shares_held"] == 1.0


# --- v2.4 broker-agnostic CSV + watchlist-only synthesis ---------------------

def test_normalize_action_variants():
    assert build._normalize_action("Market buy") == "BUY"
    assert build._normalize_action("Bought") == "BUY"
    assert build._normalize_action("B") == "BUY"
    assert build._normalize_action("SELL") == "SELL"
    assert build._normalize_action("Sold") == "SELL"
    assert build._normalize_action("dividend") == ""        # ignored downstream


def test_normalize_txn_columns_aliases():
    df = pd.DataFrame({"Symbol": ["AAPL"], "Date": ["2025-01-02"],
                       "Side": ["Buy"], "Quantity": [3]})
    out = build._normalize_txn_columns(df)
    assert {"ticker", "date", "action", "shares"}.issubset(out.columns)


def test_synthesize_watchlist_transactions():
    dates = _bdates(10)
    px = pd.DataFrame({"AAA": np.arange(10.0) + 100, "BBB": np.arange(10.0) + 50},
                      index=dates)
    wl = pd.DataFrame({"ticker": ["AAA", "BBB", "ZZZ"]})   # ZZZ has no price -> skipped
    txns = build._synthesize_watchlist_transactions(wl, px)
    assert set(txns.ticker) == {"AAA", "BBB"}
    assert (txns.action == "BUY").all()
    assert (txns.shares == 1.0).all()
    assert (txns.date == dates[0]).all()                   # tracked from window start
