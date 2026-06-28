"""v2.7 Watchlist enrich — entry-signal layer for watched (non-held) names.

Reuses Big Brain thresholds on already-computed quant/analyst/news data.
Technical chips come from quant_metrics (range52w_pct, rsi14, vol_ratio,
sma200_dist_pct); analyst upside from target_mean (pence-divisor aware);
news cite from ticker_news via _bb_news_cite. Graceful degradation is required:
any missing source omits that chip, never raises.
"""
import json
import numpy as np
import pandas as pd

import build


def _payload(ticker="AAA", native_latest=100.0, currency="USD", name="Aco"):
    # Mirrors the relevant subset of build_watchlist_payload output.
    return {ticker: {"name": name, "currency": currency, "native_latest": native_latest,
                     "latest": native_latest, "note": ""}}


def _quant(rows):
    cols = ["range52w_pct", "rsi14", "vol_ratio", "sma200_dist_pct"]
    df = pd.DataFrame(rows).set_index("ticker")
    for c in cols:
        if c not in df.columns:
            df[c] = np.nan
    return df


def _analyst(rows):
    cols = ["target_mean", "current_price", "recommendation", "num_analysts"]
    df = pd.DataFrame(rows).set_index("ticker")
    for c in cols:
        if c not in df.columns:
            df[c] = np.nan
    return df


def _news(ticker, title="Aco beats", publisher="Reuters", link="http://x"):
    # Real items_json shape: title/link/publisher/published(ISO). Recent so it
    # falls inside _bb_news_cite's recency window.
    items = [{"title": title, "publisher": publisher, "link": link,
              "published": pd.Timestamp.now(tz="UTC").isoformat()}]
    return pd.DataFrame({"items_json": [json.dumps(items)]}, index=[ticker])


def _meta(ticker="AAA", currency="USD", name="Aco"):
    return pd.DataFrame({"currency": [currency], "name": [name]}, index=[ticker])


def _labels(entry):
    return {t["label"] for t in entry["triggers"]}


def test_near_low_and_oversold_make_buy_zone():
    pl = _payload()
    q = _quant([{"ticker": "AAA", "range52w_pct": 6, "rsi14": 25,
                 "vol_ratio": 1.0, "sma200_dist_pct": -8}])
    out = build.build_watchlist_signals(pl, q, build._analyst_empty(), None, _meta())
    e = out["AAA"]
    assert "Near low" in _labels(e) and "Oversold" in _labels(e)
    assert "Below 200-day" in _labels(e)
    assert e["verdict"]["label"] == "Buy zone" and e["verdict"]["tone"] == "buy"


def test_extended_overbought_is_cooling():
    pl = _payload()
    q = _quant([{"ticker": "AAA", "range52w_pct": 95, "rsi14": 78,
                 "vol_ratio": 1.0, "sma200_dist_pct": 22}])
    out = build.build_watchlist_signals(pl, q, build._analyst_empty(), None, _meta())
    e = out["AAA"]
    assert "Overbought" in _labels(e) and "Extended" in _labels(e)
    assert e["verdict"]["label"] == "Cooling off" and e["verdict"]["tone"] == "caution"


def test_neutral_default_is_watching():
    pl = _payload()
    q = _quant([{"ticker": "AAA", "range52w_pct": 50, "rsi14": 50,
                 "vol_ratio": 1.0, "sma200_dist_pct": 3}])
    out = build.build_watchlist_signals(pl, q, build._analyst_empty(), None, _meta())
    assert out["AAA"]["verdict"]["label"] == "Watching"
    assert out["AAA"]["triggers"] == [] or "Near low" not in _labels(out["AAA"])


def test_volume_chip_formats_multiple():
    pl = _payload()
    q = _quant([{"ticker": "AAA", "vol_ratio": 2.34}])
    out = build.build_watchlist_signals(pl, q, build._analyst_empty(), None, _meta())
    assert any(t["label"].startswith("Vol 2.3") for t in out["AAA"]["triggers"])


def test_analyst_upside_chip():
    # Uses the analyst frame's own current_price (preferred path).
    pl = _payload(native_latest=999.0)   # ignored: current_price wins
    a = _analyst([{"ticker": "AAA", "target_mean": 130.0, "current_price": 100.0,
                   "recommendation": "buy", "num_analysts": 12}])
    out = build.build_watchlist_signals(pl, _quant([{"ticker": "AAA"}]), a, None, _meta())
    labels = _labels(out["AAA"])
    assert any(l.startswith("+30%") for l in labels)   # (130/100 - 1) = +30%


def test_no_analyst_upside_chip_when_target_below_price():
    pl = _payload(native_latest=100.0)
    a = _analyst([{"ticker": "AAA", "target_mean": 90.0, "recommendation": "hold",
                   "num_analysts": 4}])
    out = build.build_watchlist_signals(pl, _quant([{"ticker": "AAA"}]), a, None, _meta())
    assert not any("target" in t["label"] for t in out["AAA"]["triggers"])


def test_pence_quoted_upside_cancels():
    # GBp (pence): target 1300p / current 1000p -> +30%. Both come from yfinance
    # in the SAME native unit, so the pence factor cancels (no divisor) — matching
    # the main holdings table (build.py:2972). Here current_price is absent, so the
    # native_latest fallback (also raw pence) is used.
    pl = _payload(native_latest=1000.0, currency="GBp")
    a = _analyst([{"ticker": "AAA", "target_mean": 1300.0, "recommendation": "buy",
                   "num_analysts": 6}])
    out = build.build_watchlist_signals(pl, _quant([{"ticker": "AAA"}]), a,
                                        None, _meta(currency="GBp"))
    assert any(l.startswith("+30%") for l in _labels(out["AAA"]))


def test_news_cite_attached_and_optional():
    pl = _payload()
    out = build.build_watchlist_signals(pl, _quant([{"ticker": "AAA"}]),
                                        build._analyst_empty(), _news("AAA"), _meta())
    assert out["AAA"]["news_cite"] and out["AAA"]["news_cite"]["title"] == "Aco beats"
    out2 = build.build_watchlist_signals(_payload(), _quant([{"ticker": "AAA"}]),
                                         build._analyst_empty(), None, _meta())
    assert out2["AAA"]["news_cite"] is None


def test_graceful_when_no_quant_row():
    pl = _payload("ZZZ")
    out = build.build_watchlist_signals(pl, _quant([{"ticker": "AAA"}]),
                                        build._analyst_empty(), None, _meta("ZZZ"))
    e = out["ZZZ"]
    assert e["triggers"] == [] and e["verdict"]["label"] == "Watching"
    assert e["news_cite"] is None


# --- render -----------------------------------------------------------------

def _enriched(ticker="AAA", verdict=("Buy zone", "buy"),
              triggers=(("Near low", "buy"), ("Oversold", "buy")),
              cite=True):
    pl = {ticker: {
        "name": "Aco", "currency": "USD", "ccy_symbol": "$",
        "latest": 100.0, "native_latest": 100.0, "total": -5.0,
        "prices": [100, 98, 95], "note": "",
        "triggers": [{"label": l, "tone": t} for (l, t) in triggers],
        "verdict": {"label": verdict[0], "tone": verdict[1]},
        "news_cite": ({"title": "Aco beats", "publisher": "Reuters",
                       "link": "http://x"} if cite else None),
    }}
    return pl


def test_render_shows_verdict_pill_with_tone():
    html = build.render_watchlist(_enriched(), pd.DataFrame())
    assert "wl-verdict" in html
    assert "wl-v-buy" in html
    assert "Buy zone" in html


def test_render_shows_chip_row():
    html = build.render_watchlist(_enriched(), pd.DataFrame())
    assert "wl-chip" in html
    assert "Near low" in html and "Oversold" in html


def test_render_news_cite_present_and_optional():
    html = build.render_watchlist(_enriched(cite=True), pd.DataFrame())
    assert "wl-cite" in html and "Aco beats" in html
    html2 = build.render_watchlist(_enriched(cite=False), pd.DataFrame())
    assert "wl-cite" not in html2


def test_render_empty_payload_returns_blank():
    assert build.render_watchlist({}, pd.DataFrame()) == ""


# --- v3.0 #4: arrow pager (measured-columns; JS sizes the page to the row) ----

def _many(n):
    """n minimally-valid watchlist cards, tickers T0..T{n-1}."""
    return {f"T{i}": {"name": f"Co{i}", "currency": "USD", "ccy_symbol": "$",
                      "latest": 100.0, "native_latest": 100.0, "total": 1.0,
                      "prices": [100, 101, 102], "note": ""} for i in range(n)}


def test_render_pager_appears_above_mobile_cols():
    # > mobile column count -> paging could be needed, so nav + marker render.
    n = build.WATCH_COLS_MOBILE + 4
    html = build.render_watchlist(_many(n), pd.DataFrame())
    assert 'data-wl-pageable="1"' in html
    assert "wl-nav" in html and "wl-prev" in html and "wl-next" in html
    assert "wl-page-cur" in html and "wl-page-total" in html
    # every card is rendered; the JS windows them client-side (page size = cols)
    assert html.count('class="wl-card"') == n
    # the page size is NOT baked into the server markup (measured live in JS)
    assert "data-wl-page=" not in html and "data-wl-pages=" not in html


def test_render_no_pager_at_or_below_mobile_cols():
    n = build.WATCH_COLS_MOBILE          # fits one mobile row -> never needs paging
    html = build.render_watchlist(_many(n), pd.DataFrame())
    assert "data-wl-pageable" not in html
    assert "wl-nav" not in html
    assert html.count('class="wl-card"') == n
