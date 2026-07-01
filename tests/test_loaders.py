"""v3.0 L-TESTS: coverage for the transaction loaders / ticker resolvers and a
public-surface guard for the upcoming v2.9.7 module refactor. Pure + offline."""
import inspect

import pandas as pd
import pytest

import build


# --- Group 1: _resolve_yf_ticker (ISIN country prefix -> exchange suffix) -----

def test_resolve_empty_ticker_returns_blank():
    assert build._resolve_yf_ticker("", "GB00B0SWJX34") == ""
    assert build._resolve_yf_ticker("   ", "GB00B0SWJX34") == ""


def test_resolve_gb_isin_gets_london_suffix():
    assert build._resolve_yf_ticker("TSCO", "GB00B0SWJX34") == "TSCO.L"


def test_resolve_us_isin_no_suffix():
    assert build._resolve_yf_ticker("AAPL", "US0378331005") == "AAPL"


def test_resolve_europe_suffixes():
    assert build._resolve_yf_ticker("SAP", "DE0007164600") == "SAP.DE"
    assert build._resolve_yf_ticker("MC", "FR0000121014") == "MC.PA"
    assert build._resolve_yf_ticker("ASML", "NL0010273215") == "ASML.AS"


def test_resolve_ie_us_listed_kept_without_london_suffix():
    # Irish-domiciled operating cos that primarily trade on NYSE (IE_US_LISTED).
    assert build._resolve_yf_ticker("ACN", "IE00B4BNMY34") == "ACN"


def test_resolve_ie_other_gets_london_suffix():
    assert build._resolve_yf_ticker("RYA", "IE00BYTBXV33") == "RYA.L"


def test_resolve_explicit_override_wins():
    # Airbus: Dutch ISIN but yfinance primary listing is Paris (TICKER_OVERRIDES).
    assert build._resolve_yf_ticker("AIR", "NL0000235190") == "AIR.PA"


def test_resolve_blank_isin_defaults_to_us():
    # len(isin) < 2 -> country defaults to "US" -> no suffix.
    assert build._resolve_yf_ticker("FOO", "") == "FOO"


def test_resolve_unknown_country_returns_ticker_unchanged():
    assert build._resolve_yf_ticker("BAR", "ZZ1234567890") == "BAR"


# --- Group 2: _resolve_fund_by_name (free-text Name -> .L ticker) -------------

def test_resolve_fund_by_name_matches_known_pattern_case_insensitive():
    name = "Invesco Markets III plc EQQQ Nasdaq 100 UCITS ETF"
    assert build._resolve_fund_by_name(name) == "EQQQ.L"


def test_resolve_fund_by_name_no_match_returns_blank():
    assert build._resolve_fund_by_name("Some Totally Unknown Fund") == ""


def test_resolve_fund_by_name_empty_or_none_returns_blank():
    assert build._resolve_fund_by_name("") == ""
    assert build._resolve_fund_by_name(None) == ""


# --- Group 3: manual-fund exclusion path in load_transactions_from_log --------

def _fake_log(rows, columns=("Action", "Time", "ISIN", "Ticker", "Name")):
    return pd.DataFrame(rows, columns=list(columns))


def test_log_splits_tracked_vs_untracked(monkeypatch):
    rows = [
        ["Market buy", "2024-10-21 10:00:00", "GB00B0SWJX34", "TSCO", "Tesco"],
        ["Market buy", "2024-10-22 10:00:00", "US0378331005", "AAPL", "Apple"],
        # name-resolvable manual fund (no ISIN/ticker) -> promoted to tracked
        ["Market buy", "21/10/2024", "", "", "Invesco Markets III plc EQQQ Nasdaq 100 UCITS ETF"],
        # unresolvable manual fund -> stays untracked
        ["Market buy", "21/10/2024", "", "", "Legal & General International Index Trust"],
    ]
    monkeypatch.setattr(build.pd, "read_excel", lambda *a, **k: _fake_log(rows))
    out, untracked = build.load_transactions_from_log()
    assert set(out["ticker"]) == {"TSCO.L", "AAPL", "EQQQ.L"}
    assert list(out.columns) == ["ticker", "date", "action", "shares"]
    assert (out["action"] == "BUY").all()
    assert (out["shares"] == 1.0).all()
    # exactly the unresolvable fund row is untracked
    assert len(untracked) == 1
    assert "Legal & General" in untracked.iloc[0]["name"]


def test_log_missing_required_column_raises(monkeypatch):
    bad = _fake_log([["Market buy", "2024-10-21", "US0378331005", "AAPL"]],
                    columns=("Action", "Time", "ISIN", "Ticker"))   # no Name
    monkeypatch.setattr(build.pd, "read_excel", lambda *a, **k: bad)
    with pytest.raises(ValueError, match="name"):
        build.load_transactions_from_log()


# --- Group 6: public-surface guard for the v2.9.7 module refactor -------------
# These names will move across modules during the refactor; this test fails loudly
# if a reference breaks, without exercising the network.

def test_public_pipeline_surface_exists():
    expected = [
        "main", "render_html",
        "load_transactions", "load_transactions_from_snapshot",
        "load_transactions_from_log", "resolve_basket_source",
        "build_positions", "compute_basket_mtm_series", "compute_benchmark_series",
        "download_ohlcv", "download_benchmark", "fetch_analyst_data",
        "fetch_ticker_news", "compute_quant_metrics",
        "build_value_screen", "select_auto_watchlist", "build_combined_watchlist",
        "build_portfolio_payload", "export_basket_snapshot",
        # v3.2 Doctor public surface
        "compute_doctor_report", "render_doctor", "basket_beta",
        "pct_open_underwater", "sector_effective_n", "basket_vol_trend",
        "evaluate_health",
    ]
    missing = [n for n in expected if not callable(getattr(build, n, None))]
    assert missing == [], f"missing/renamed public functions: {missing}"


def test_render_html_and_main_signatures_stable():
    # The refactor must keep these call sites working.
    rh = inspect.signature(build.render_html).parameters
    for p in ("returns", "prices", "meta", "value_rows", "auto_tickers", "nasdaq_series"):
        assert p in rh, f"render_html lost param {p}"
    mn = inspect.signature(build.main).parameters
    for p in ("demo", "watchlist_only", "from_snapshot"):
        assert p in mn, f"main lost param {p}"
