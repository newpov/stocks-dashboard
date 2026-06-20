"""v2.5 #1: industry-outlook group return is a MARKET-CAP-weighted average.

A straight mean over-weights a small, volatile name. Weighting by market cap
makes the bigger company dominate the group's headline number (the standard
index-weighting convention).
"""
import numpy as np
import pandas as pd

import build


def _outlook(rows: list[dict]) -> pd.DataFrame:
    """rows: [{ticker, industry, ret_12m, market_cap, ...}] -> indexed df."""
    df = pd.DataFrame(rows).set_index("ticker")
    for col, default in (("upside", np.nan), ("recommendation", ""),
                         ("num_analysts", 0), ("cap_tier", ""),
                         ("current_price", np.nan)):
        if col not in df.columns:
            df[col] = default
    return df


def test_industry_outlook_is_market_cap_weighted():
    groups = build.build_industry_outlook(_outlook([
        {"ticker": "SMALL", "industry": "Tech", "ret_12m": 100.0, "market_cap": 1e9},
        {"ticker": "BIG1", "industry": "Tech", "ret_12m": 10.0, "market_cap": 100e9},
        {"ticker": "BIG2", "industry": "Tech", "ret_12m": 10.0, "market_cap": 100e9},
    ]), min_holdings=3)
    assert len(groups) == 1
    # straight mean would be (100+10+10)/3 = 40.0; market-cap-weighted:
    # (1*100 + 100*10 + 100*10) / (1+100+100) = 2100/201 = 10.45
    assert abs(groups[0]["avg_ret_12m"] - (2100.0 / 201.0)) < 0.01
    assert groups[0]["avg_ret_12m"] < 15.0   # nowhere near the straight-mean 40


def test_industry_outlook_falls_back_to_straight_mean_without_caps():
    """If no ticker in a group has a usable market cap, don't divide by zero —
    fall back to the simple average so the group still renders."""
    groups = build.build_industry_outlook(_outlook([
        {"ticker": "A", "industry": "Tech", "ret_12m": 30.0, "market_cap": np.nan},
        {"ticker": "B", "industry": "Tech", "ret_12m": 10.0, "market_cap": np.nan},
        {"ticker": "C", "industry": "Tech", "ret_12m": 20.0, "market_cap": 0.0},
    ]), min_holdings=3)
    assert len(groups) == 1
    assert abs(groups[0]["avg_ret_12m"] - 20.0) < 0.01   # (30+10+20)/3
