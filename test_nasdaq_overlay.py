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
