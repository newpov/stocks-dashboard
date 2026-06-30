"""build_industry_attribution robustness (M10).

A NaN weight or NaN return must not silently distort the basket/industry
averages: weight coerces to the equal-weight default (mirroring
compute_currency_exposure), and names with no computed return are dropped so
they can't render a phantom 0.0% industry row.
"""
import numpy as np
import pandas as pd

import build


def _returns(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows).set_index("ticker")
    if "total_invested" not in df.columns:
        df["total_invested"] = 100.0
    return df


def _meta(industries: dict) -> pd.DataFrame:
    idx = list(industries)
    return pd.DataFrame({"industry": [industries[t] for t in idx],
                         "sector": [industries[t] for t in idx]}, index=idx)


def test_nan_weight_coerced_to_equal_unit():
    # BBB has a NaN weight but a real +20% return — it must still count toward the
    # Tech average (as one equal unit), not be silently dropped.
    rets = _returns([
        {"ticker": "AAA", "status": "open", "weight": 1.0, "total_pct": 10.0},
        {"ticker": "BBB", "status": "open", "weight": np.nan, "total_pct": 20.0},
    ])
    rows, basket = build.build_industry_attribution(rets, _meta({"AAA": "Tech", "BBB": "Tech"}))
    assert len(rows) == 1
    assert rows[0]["n_holdings"] == 2
    assert abs(rows[0]["avg_return"] - 15.0) < 1e-9    # (10 + 20) / 2, BBB included
    assert abs(basket - 15.0) < 1e-9


def test_nan_return_name_dropped_not_phantom_zero():
    # CCC has no computed return -> it must not render an "Energy +0.0%" phantom row.
    rets = _returns([
        {"ticker": "AAA", "status": "open", "weight": 1.0, "total_pct": 10.0},
        {"ticker": "CCC", "status": "open", "weight": 1.0, "total_pct": np.nan},
    ])
    rows, basket = build.build_industry_attribution(rets, _meta({"AAA": "Tech", "CCC": "Energy"}))
    industries = {r["industry"] for r in rows}
    assert industries == {"Tech"}                       # no phantom Energy row
    assert abs(basket - 10.0) < 1e-9


def test_all_nan_returns_empty():
    rets = _returns([
        {"ticker": "AAA", "status": "open", "weight": 1.0, "total_pct": np.nan},
    ])
    rows, basket = build.build_industry_attribution(rets, _meta({"AAA": "Tech"}))
    assert rows == [] and basket == 0.0
