"""v3.4 #3: short-term (1M/3M) rebased hero view. Pure + offline."""
import inspect

import pandas as pd
from pytest import approx as pytest_approx

import build


# --- ST-T1: rebase helper + payload short slice ------------------------------

def test_rebase_window_start_is_zero():
    out = build.rebase_cumulative_returns([10.0, 21.0, 33.1])
    assert out[0] == 0.0
    # (1.21/1.10 - 1)*100 = 10.0 ; (1.331/1.10 - 1)*100 = 21.0
    assert out[1] == pytest_approx(10.0)
    assert out[2] == pytest_approx(21.0)


def test_rebase_mid_series_start_idx():
    out = build.rebase_cumulative_returns([5.0, 10.0, 21.0], start_idx=1)
    assert out[1] == 0.0
    assert out[2] == pytest_approx(10.0)


def test_rebase_empty_and_single_safe():
    assert build.rebase_cumulative_returns([]) == []
    assert build.rebase_cumulative_returns([7.0]) == [0.0]


def test_payload_has_short_daily_slice():
    idx = pd.date_range("2025-01-01", periods=200, freq="D")
    basket = pd.Series(range(200), index=idx, dtype=float)
    spy = pd.Series([i * 0.5 for i in range(200)], index=idx, dtype=float)
    pay = build.build_portfolio_payload(basket, spy, idx[0])
    assert "short" in pay
    assert pay["short"]["dates"] and pay["short"]["basket"] and pay["short"]["spy"]
    assert len(pay["short"]["dates"]) <= build.SHORT_TERM_DAYS + 2
    assert len(pay["short"]["basket"]) == len(pay["short"]["dates"])


def test_payload_short_empty_safe():
    pay = build.build_portfolio_payload(pd.Series(dtype=float), pd.Series(dtype=float),
                                        pd.Timestamp("2025-01-01"))
    assert pay["short"]["dates"] == []


# --- ST-T2 / ST-T3: control markup + short-mode JS ---------------------------

def test_range_control_in_page_source():
    src = inspect.getsource(build.render_html) + build._read_asset("dashboard.css") + build._read_asset("dashboard.js")  # v3.5: page source includes the inlined assets
    assert 'data-range="3m"' in src and 'data-range="1m"' in src and 'data-range="all"' in src
    assert "hero-range" in src


def test_short_mode_js_present():
    src = inspect.getsource(build.render_html) + build._read_asset("dashboard.css") + build._read_asset("dashboard.js")  # v3.5: page source includes the inlined assets
    assert "heroRange" in src
    assert "PORTFOLIO.short" in src
    assert "rebaseWindow" in src

