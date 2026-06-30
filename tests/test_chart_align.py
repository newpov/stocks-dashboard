"""v2.7 #6 — sparkline calendar alignment.

The alpha + drawdown sparklines used to map x by index over their own (different)
lengths, so a calendar date landed at a different x in each and in the main chart.
_date_fraction maps a date to its [0,1] position across a shared [start,end]
domain (the basket's full date span), which both sparklines now use.
"""
import pandas as pd

import build


def test_date_fraction_endpoints():
    s = pd.Timestamp("2024-10-01")
    e = pd.Timestamp("2026-06-21")
    assert build._date_fraction(s, s, e) == 0.0
    assert build._date_fraction(e, s, e) == 1.0
    mid = s + (e - s) / 2
    assert abs(build._date_fraction(mid, s, e) - 0.5) < 1e-6


def test_date_fraction_clamps_out_of_range():
    s = pd.Timestamp("2025-01-01")
    e = pd.Timestamp("2025-12-31")
    assert build._date_fraction(pd.Timestamp("2024-01-01"), s, e) == 0.0   # before start
    assert build._date_fraction(pd.Timestamp("2026-06-01"), s, e) == 1.0   # after end


def test_date_fraction_zero_span_is_zero():
    s = pd.Timestamp("2025-01-01")
    assert build._date_fraction(s, s, s) == 0.0


def test_date_fraction_alpha_starts_partway():
    # An alpha series that begins 30 days into a ~600-day basket should map its
    # first point to a small-but-nonzero fraction (not 0), unlike index mapping.
    start = pd.Timestamp("2024-10-01")
    end = pd.Timestamp("2026-06-21")
    alpha_first = start + pd.Timedelta(days=30)
    f = build._date_fraction(alpha_first, start, end)
    assert 0.0 < f < 0.1
