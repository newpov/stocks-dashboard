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
