"""v2.9.2 remainder: fetch robustness (M2 benchmark shape, M5 currency/retry safety)."""
import pandas as pd

import build


# --- M2: benchmark Close extraction robust to yfinance column-shape changes ---

def _idx(n=3):
    return pd.date_range("2024-01-01", periods=n)


def test_benchmark_close_single_level():
    df = pd.DataFrame({"Open": [1, 2, 3], "Close": [10, 11, 12]}, index=_idx())
    assert list(build._benchmark_close_from_df(df)) == [10, 11, 12]


def test_benchmark_close_field_ticker_multiindex():
    df = pd.DataFrame([[1, 10], [2, 11], [3, 12]], index=_idx(),
                      columns=pd.MultiIndex.from_tuples([("Open", "SPY"), ("Close", "SPY")]))
    assert list(build._benchmark_close_from_df(df)) == [10, 11, 12]


def test_benchmark_close_ticker_field_multiindex():
    # The shape the old `df["Close"]` raised KeyError on -> silent empty SPY.
    df = pd.DataFrame([[1, 10], [2, 11], [3, 12]], index=_idx(),
                      columns=pd.MultiIndex.from_tuples([("SPY", "Open"), ("SPY", "Close")]))
    assert list(build._benchmark_close_from_df(df)) == [10, 11, 12]


def test_benchmark_close_missing_or_empty():
    assert build._benchmark_close_from_df(pd.DataFrame({"Open": [1, 2]})).empty
    assert build._benchmark_close_from_df(pd.DataFrame()).empty


# --- M5: a blank failure-currency safely normalizes to USD at use-time ---
# (so leaving currency blank on a meta-fetch failure can't break FX, while it
#  still re-triggers a refetch next build instead of permanently masking as USD)

def test_blank_currency_normalizes_to_usd():
    assert build.normalize_currency("") == ("USD", 1.0)
    assert build.normalize_currency(None) == ("USD", 1.0)
    assert build.normalize_currency("GBp") == ("GBP", 100.0)   # pence still handled
