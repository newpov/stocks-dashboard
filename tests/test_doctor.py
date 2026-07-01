# tests/test_doctor.py
import numpy as np
import pandas as pd
import pytest
import build


def _series(vals, start="2025-01-01"):
    idx = pd.date_range(start, periods=len(vals), freq="D")
    return pd.Series(vals, index=idx, dtype=float)


def test_basket_beta_perfectly_correlated_unit_slope():
    # basket == bench (same daily returns) -> beta 1.0
    bench = _series([100, 101, 102, 101, 103, 104])
    basket = bench.copy()
    assert build.basket_beta(basket, bench) == pytest.approx(1.0, abs=1e-9)


def test_basket_beta_double_amplitude_slope_two():
    bench = _series([100, 110, 121, 108.9])          # +10%, +10%, -10%
    basket = _series([100, 120, 144, 115.2])          # +20%, +20%, -20%
    assert build.basket_beta(basket, bench) == pytest.approx(2.0, abs=1e-6)


def test_basket_beta_insufficient_or_flat_is_nan():
    assert np.isnan(build.basket_beta(_series([100]), _series([100])))
    flat = _series([100, 100, 100, 100])
    assert np.isnan(build.basket_beta(_series([100, 101, 102, 103]), flat))


def _returns_df(rows):
    # rows: list of (ticker, status, total_pct)
    return pd.DataFrame(
        [{"status": s, "total_pct": p} for (_, s, p) in rows],
        index=[t for (t, _, _) in rows],
    )


def test_pct_open_underwater_counts_only_open_below_zero():
    df = _returns_df([
        ("AAA", "open", -5.0),
        ("BBB", "open", 12.0),
        ("CCC", "open", -0.1),
        ("DDD", "closed", -99.0),   # closed: ignored
    ])
    # 2 of 3 open names underwater -> 66.67%
    assert build.pct_open_underwater(df) == pytest.approx(200.0 / 3.0, abs=1e-6)


def test_pct_open_underwater_no_open_is_nan():
    df = _returns_df([("DDD", "closed", -99.0)])
    assert np.isnan(build.pct_open_underwater(df))
