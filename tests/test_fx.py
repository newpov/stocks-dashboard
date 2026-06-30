"""convert_to_base FX handling (M4).

The leading-edge backfill must not silently fabricate a flat rate across a long
pre-series gap, but it also must never drop an early-bought non-base ticker
(build_positions consumes base prices, so a NaN there loses the position). The
contract: ffill over interior/trailing gaps; a BOUNDED bfill for short
start-of-series calendar misalignment; for a genuine long leading gap, fall back
to the earliest known rate WITH a warning (visible, not silent, not dropped).
"""
import numpy as np
import pandas as pd

import build


def _dates(n, start="2025-01-01"):
    return pd.bdate_range(start, periods=n)


def test_convert_basic_and_pence_divisor():
    dates = _dates(5)
    prices = pd.DataFrame({"AAA": [100.0] * 5, "LON.L": [500.0] * 5}, index=dates)
    meta = pd.DataFrame({"currency": ["USD", "GBp"]}, index=["AAA", "LON.L"])
    fx = pd.DataFrame({"USDGBP=X": [0.8] * 5}, index=dates)
    out = build.convert_to_base(prices, meta, fx, base="GBP")
    assert abs(out["AAA"].iloc[0] - 80.0) < 1e-9       # 100 USD * 0.8
    assert abs(out["LON.L"].iloc[0] - 5.0) < 1e-9      # 500 pence / 100 = 5 GBP (base, no FX)


def test_short_leading_gap_bounded_bfill():
    # A 2-day start-of-series misalignment is legitimately back-filled (<= limit).
    dates = _dates(6)
    prices = pd.DataFrame({"AAA": [100.0] * 6}, index=dates)
    meta = pd.DataFrame({"currency": ["USD"]}, index=["AAA"])
    fx = pd.DataFrame({"USDGBP=X": [np.nan, np.nan, 0.8, 0.8, 0.8, 0.8]}, index=dates)
    out = build.convert_to_base(prices, meta, fx, base="GBP")
    assert out["AAA"].notna().all()
    assert abs(out["AAA"].iloc[0] - 80.0) < 1e-9       # bfilled 0.8


def test_long_leading_gap_uses_earliest_rate_not_dropped(capsys):
    # 10 leading NaNs (> limit). The position must survive (no NaN where a price
    # exists), early dates use the earliest known rate, and it WARNS.
    dates = _dates(12)
    prices = pd.DataFrame({"AAA": [100.0] * 12}, index=dates)
    meta = pd.DataFrame({"currency": ["USD"]}, index=["AAA"])
    fx = pd.DataFrame({"USDGBP=X": [np.nan] * 10 + [0.9, 0.9]}, index=dates)
    out = build.convert_to_base(prices, meta, fx, base="GBP")
    assert out["AAA"].notna().all()                    # position NOT dropped
    assert abs(out["AAA"].iloc[0] - 90.0) < 1e-9       # earliest-rate fallback
    assert "before FX coverage" in capsys.readouterr().err


def test_interior_and_trailing_gaps_ffilled():
    dates = _dates(6)
    prices = pd.DataFrame({"AAA": [100.0] * 6}, index=dates)
    meta = pd.DataFrame({"currency": ["USD"]}, index=["AAA"])
    fx = pd.DataFrame({"USDGBP=X": [0.8, np.nan, 0.8, np.nan, np.nan, 0.8]}, index=dates)
    out = build.convert_to_base(prices, meta, fx, base="GBP")
    assert out["AAA"].notna().all()                    # carried forward over gaps
    assert abs(out["AAA"].iloc[1] - 80.0) < 1e-9       # ffilled


def test_missing_pair_left_in_native_not_dropped(capsys):
    # No JPYGBP=X series: leave the ticker in native units (its % return stays
    # correct and the position survives) rather than NaN it out of the basket.
    dates = _dates(4)
    prices = pd.DataFrame({"AAA": [100.0] * 4}, index=dates)
    meta = pd.DataFrame({"currency": ["JPY"]}, index=["AAA"])
    fx = pd.DataFrame({"USDGBP=X": [0.8] * 4}, index=dates)
    out = build.convert_to_base(prices, meta, fx, base="GBP")
    assert (out["AAA"] == 100.0).all()
    assert "no FX series JPYGBP=X" in capsys.readouterr().err
