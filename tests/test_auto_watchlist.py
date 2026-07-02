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


def test_select_auto_watchlist_default_is_uncapped():
    rows = [_vrow(t, True) for t in ("A", "B", "C", "D", "E", "F")]
    out = build.select_auto_watchlist(rows, manual_tickers=set())
    assert out == ["A", "B", "C", "D", "E", "F"]   # v3.3: no cap, all two-signal names
    assert build.AUTO_WATCH_MAX is None


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


# --- Task 3: payload passthrough + render ---

def _combined(*triples):
    return pd.DataFrame([{"ticker": t, "note": n, "wl_kind": k} for (t, n, k) in triples])


def _prices(tickers):
    end = pd.Timestamp.now().normalize() - pd.Timedelta(days=1)
    start = end - pd.Timedelta(days=59)
    idx = pd.date_range(start, end, freq="D")
    return pd.DataFrame({t: [100 + i for i in range(len(idx))] for t in tickers}, index=idx)


def _meta(tickers):
    return pd.DataFrame({"name": {t: f"{t} Inc" for t in tickers},
                         "sector": {t: "Tech" for t in tickers},
                         "industry": {t: "Software" for t in tickers},
                         "currency": {t: "USD" for t in tickers}})


def test_payload_carries_wl_kind():
    df = _combined(("AAA", "", "auto"), ("MMM", "n", "manual"))
    px = _prices(["AAA", "MMM"])
    pay = build.build_watchlist_payload(df, px, px, _meta(["AAA", "MMM"]))
    assert pay["AAA"]["wl_kind"] == "auto"
    assert pay["MMM"]["wl_kind"] == "manual"


def test_payload_wl_kind_defaults_manual_when_column_absent():
    # Back-compat: a plain [ticker, note] frame (no wl_kind) -> "manual".
    df = pd.DataFrame([{"ticker": "AAA", "note": ""}])
    px = _prices(["AAA"])
    pay = build.build_watchlist_payload(df, px, px, _meta(["AAA"]))
    assert pay["AAA"]["wl_kind"] == "manual"


def _render_payload(kind):
    return {"AAA": {"name": "Aco", "currency": "USD", "ccy_symbol": "$",
                    "latest": 100.0, "native_latest": 100.0, "total": -3.0,
                    "prices": [100, 99, 98], "note": "", "wl_kind": kind}}


def test_render_auto_card_shaded_and_badged():
    html = build.render_watchlist(_render_payload("auto"), pd.DataFrame())
    assert "wl-auto" in html and "wl-auto-tag" in html and "Value + BB" in html


def test_render_manual_validated_badge_no_shading():
    html = build.render_watchlist(_render_payload("manual_validated"), pd.DataFrame())
    assert "wl-auto-tag" in html and "Value + BB" in html
    assert "wl-card wl-auto" not in html         # no shading class


def test_render_plain_manual_no_badge_no_shading():
    html = build.render_watchlist(_render_payload("manual"), pd.DataFrame())
    # The intro paragraph always contains the example badge; check card-level HTML only.
    cards_html = html.split('class="wl-grid"', 1)[-1]
    assert "wl-auto-tag" not in cards_html and "wl-card wl-auto" not in cards_html
