"""v3.0 #5: auto-watchlist for Value ∩ Big Brain names."""
import pandas as pd

import build


def _vrow(ticker, is_bb):
    return {"ticker": ticker, "is_bb_idea": is_bb}


# --- Task 1: selection ---

def test_two_signal_tickers_filters_and_orders():
    rows = [_vrow("AAA", True), _vrow("BBB", False), _vrow("CCC", True)]
    assert build.two_signal_tickers(rows) == ["AAA", "CCC"]


def test_two_signal_tickers_empty():
    assert build.two_signal_tickers(None) == []
    assert build.two_signal_tickers([]) == []


def test_select_auto_watchlist_excludes_manual_and_caps():
    rows = [_vrow(t, True) for t in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")]
    out = build.select_auto_watchlist(rows, manual_tickers={"BBB"}, max_n=3)
    assert out == ["AAA", "CCC", "DDD"]          # BBB excluded, capped at 3, order kept


def test_select_auto_watchlist_default_cap_is_constant():
    rows = [_vrow(t, True) for t in ("A", "B", "C", "D", "E", "F")]
    out = build.select_auto_watchlist(rows, manual_tickers=set())
    assert out == ["A", "B", "C", "D"]           # AUTO_WATCH_MAX == 4
    assert build.AUTO_WATCH_MAX == 4


def test_select_auto_watchlist_no_bb_returns_empty():
    rows = [_vrow("AAA", False), _vrow("BBB", False)]
    assert build.select_auto_watchlist(rows, manual_tickers=set()) == []


# --- Task 2: combined frame ---

def _manual(*pairs):
    return pd.DataFrame([{"ticker": t, "note": n} for (t, n) in pairs])


def test_combined_auto_first_then_manual():
    manual = _manual(("MAN", "my note"))
    df = build.build_combined_watchlist(manual, ["AAA"], two_signal_set={"AAA"})
    assert list(df["ticker"]) == ["AAA", "MAN"]            # auto first
    assert list(df["wl_kind"]) == ["auto", "manual"]
    assert df.iloc[1]["note"] == "my note"                 # manual note preserved


def test_combined_manual_validated_when_two_signal():
    manual = _manual(("MAN", ""))
    df = build.build_combined_watchlist(manual, [], two_signal_set={"MAN"})
    assert list(df["wl_kind"]) == ["manual_validated"]


def test_combined_auto_excluded_from_manual_no_dupes():
    # A name that is both auto AND in the manual frame appears once, as auto.
    manual = _manual(("AAA", "note"))
    df = build.build_combined_watchlist(manual, ["AAA"], two_signal_set={"AAA"})
    assert list(df["ticker"]) == ["AAA"]
    assert list(df["wl_kind"]) == ["auto"]


def test_combined_empty_when_no_auto_and_no_manual():
    df = build.build_combined_watchlist(None, [], two_signal_set=set())
    assert df.empty
    assert list(df.columns) == ["ticker", "note", "wl_kind"]
