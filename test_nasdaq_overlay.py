"""v3.0 #3: Nasdaq (QQQ) optional hero overlay."""
import pandas as pd

import build


def _fake_yf(ticker, **kwargs):
    idx = pd.date_range("2024-01-01", periods=3)
    return pd.DataFrame({"Open": [1, 2, 3], "Close": [10, 11, 12]}, index=idx)


# --- Task 1: parametrized benchmark fetch ---

def test_download_benchmark_accepts_ticker(monkeypatch):
    monkeypatch.setattr(build.yf, "download", _fake_yf)
    s = build.download_benchmark("QQQ")
    assert s.name == "QQQ"
    assert list(s) == [10, 11, 12]


def test_download_benchmark_default_is_spy(monkeypatch):
    monkeypatch.setattr(build.yf, "download", _fake_yf)
    s = build.download_benchmark()
    assert s.name == build.BENCHMARK == "SPY"


def test_download_benchmark_empty_on_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(build.yf, "download", _boom)
    assert build.download_benchmark("QQQ").empty


def test_benchmark2_constants_present():
    assert build.BENCHMARK2 == "QQQ"
    assert build.BENCHMARK2_CCY == "USD"


# --- Task 2: payload carries an (optional) nasdaq series ---

def _wk(step):
    idx = pd.date_range("2024-01-05", periods=6, freq="W-FRI")
    return pd.Series([i * step for i in range(6)], index=idx)


def test_payload_includes_nasdaq_when_passed():
    basket, bench, nq = _wk(2.0), _wk(1.0), _wk(1.5)
    out = build.build_portfolio_payload(basket, bench, basket.index[0], nasdaq=nq)
    assert "nasdaq" in out
    assert out["nasdaq"]["values"]
    assert len(out["nasdaq"]["values"]) == len(out["nasdaq"]["dates"])


def test_payload_nasdaq_empty_when_omitted():
    basket, bench = _wk(2.0), _wk(1.0)
    out = build.build_portfolio_payload(basket, bench, basket.index[0])
    assert out["nasdaq"] == {"dates": [], "values": []}


# --- Task 3: legend helper renders the toggle only when nasdaq present ---

def test_hero_legend_includes_nasdaq_toggle_when_present():
    html = build._hero_legend_html(True)
    assert 'data-series="nasdaq"' in html
    assert 'aria-pressed="false"' in html
    assert "leg-swatch nasdaq" in html
    assert "Basket" in html and "SPY" in html and "GBP/USD" in html


def test_hero_legend_omits_nasdaq_when_absent():
    html = build._hero_legend_html(False)
    assert "nasdaq" not in html
    assert "Basket" in html and "SPY" in html and "GBP/USD" in html
