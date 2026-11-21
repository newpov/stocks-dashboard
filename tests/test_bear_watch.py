"""v3.10 Bear watch: short-side counterpart of the Value screen. See
temp/bear-watch-spec.md. No network, no build."""
from pathlib import Path

import numpy as np
import pandas as pd

import build

ROOT = Path(build.__file__).parent
SRC = Path(build.__file__).read_text(encoding="utf-8")
CSS = (ROOT / "assets" / "dashboard.css").read_text(encoding="utf-8")


def _row(**kw):
    base = {"name": "Co", "sector": "Tech", "industry": "Software", "ret_12m": 5.0,
            "range52w_pct": 60.0, "upside": 10.0, "recommendation": "buy",
            "rev_growth": 0.05, "fcf": 1e9, "debt_to_equity": 50.0, "roe": 0.15,
            "pe": 20.0, "market_cap": 5e10, "current_price": 100.0,
            "short_pct_float": np.nan, "short_ratio": np.nan,
            "shares_short": np.nan, "shares_short_prior": np.nan}
    base.update(kw)
    return base


def _uni(rows: dict):
    return pd.DataFrame.from_dict(rows, orient="index")


# weak on 3 signals incl. a market one
WEAK = dict(upside=-12.0, rev_growth=-0.04, fcf=-2e8)


def test_qualifies_on_three_signals_with_a_market_signal():
    rows = build.build_bear_watch(_uni({"WEAK": _row(**WEAK), "OK": _row()}))
    assert [r["ticker"] for r in rows] == ["WEAK"]
    assert set(rows[0]["signals"]) == {"target_below", "rev_shrinking", "cash_burn"}


def test_fundamentals_alone_never_qualify():
    # four fundamental strikes, no price/analyst signal -> not a short case
    junk = _row(rev_growth=-0.1, fcf=-1e9, debt_to_equity=400.0, roe=-0.2)
    assert build.build_bear_watch(_uni({"JUNK": junk})) == []


def test_each_signal_rule():
    r = _row(upside=-6, recommendation="sell", ret_12m=-30, range52w_pct=10,
             rev_growth=-0.01, fcf=-1, debt_to_equity=250.0, roe=-0.01, pe=80)
    sig = build.build_bear_watch(_uni({"X": r}))[0]["signals"]
    assert set(sig) == {"target_below", "bearish_rating", "downtrend", "rev_shrinking",
                        "cash_burn", "heavy_debt", "unprofitable", "very_expensive"}


def test_boundaries_are_exclusive_where_spec_says():
    r = _row(upside=-4.9, ret_12m=-14.9, range52w_pct=10, debt_to_equity=200.0, pe=60.0,
             rev_growth=-0.1, fcf=-1, roe=-0.1)
    # -4.9% target gap, -14.9% 12m, D/E exactly 2.0x, P/E exactly 60: none fire
    assert build.build_bear_watch(_uni({"X": r})) == []


def test_ranking_and_cap_and_held_exclusion():
    rows = {f"T{i}": _row(**{**WEAK, "upside": -5.0 - i}) for i in range(14)}
    rows["MORE"] = _row(**WEAK, roe=-0.1)                  # 4 strikes ranks first
    rows["HELD"] = _row(**{**WEAK, "upside": -50.0})
    out = build.build_bear_watch(_uni(rows), log_tickers={"HELD"})
    assert out[0]["ticker"] == "MORE"
    assert out[1]["ticker"] == "T13"                        # most bearish target next
    assert len(out) == build.BEAR_MAX_ROWS
    assert "HELD" not in [r["ticker"] for r in out]


def test_crowding_tiers():
    c = build.short_crowding
    assert c(0.18, 2.0)["tier"] == "crowded"
    assert c(0.03, 8.0)["tier"] == "crowded"
    assert c(0.08, 1.0)["tier"] == "elevated"
    assert c(0.02, 1.5)["tier"] == "light"
    assert c(np.nan, np.nan)["tier"] == "na"


def test_short_interest_change_direction():
    r = _row(**WEAK, short_pct_float=0.12, short_ratio=5.0,
             shares_short=1.2e7, shares_short_prior=1.0e7)
    row = build.build_bear_watch(_uni({"X": r}))[0]
    assert row["crowding"]["tier"] == "elevated"
    assert abs(row["crowding"]["change_pct"] - 20.0) < 1e-9


def test_counter_evidence_is_shown_not_filtered():
    # weak price + fundamentals but analysts still see big upside
    r = _row(ret_12m=-40, range52w_pct=5, rev_growth=-0.1, fcf=-1, upside=35.0)
    row = build.build_bear_watch(_uni({"X": r}))[0]
    assert row["counter"] and "35" in row["counter"]
    html = build.render_bear_watch([row], "2026-11-21")
    assert "analysts bullish" in html.lower()


def test_render_escapes_and_carries_risk_note():
    r = _row(**WEAK, name='<img src=x onerror="1">')
    html = build.render_bear_watch(build.build_bear_watch(_uni({"X": r})), "2026-11-21")
    assert "<img" not in html and "&lt;img" in html
    low = html.lower()
    assert "more than 100%" in low and "not advice" in low and "squeeze" in low
    assert 'class="ticker-clickable' in html or "ticker-clickable" in html


def test_empty_state_is_honest():
    html = build.render_bear_watch([], "2026-11-21")
    assert "Bear watch" in html and "no " in html.lower()
    assert "not advice" in html.lower()


def test_module_wiring_and_assets():
    order = SRC[SRC.index("_module_defs = ["):]
    assert order.index('("value_screen"') < order.index('("bear_watch", "Bear watch"') < order.index('("outlook"')
    assert ".bw-" in CSS


def test_fetch_carries_short_interest_fields():
    for k in ("shortPercentOfFloat", "shortRatio", "sharesShort", "sharesShortPriorMonth"):
        assert f'info.get("{k}")' in SRC
    for c in ("short_pct_float", "short_ratio", "shares_short", "shares_short_prior"):
        assert c in build.UNIVERSE_REQUIRED_COLS


def test_universe_cache_missing_new_columns_counts_as_stale(tmp_path):
    p = tmp_path / "u.parquet"
    pd.DataFrame({"ret_12m": [1.0], "cache_date": ["2099-01-01"]}, index=["A"]).to_parquet(p)
    assert build._universe_cache_has_schema(p) is False
    cols = {c: [np.nan] for c in build.UNIVERSE_REQUIRED_COLS}
    pd.DataFrame({**cols, "cache_date": ["2099-01-01"]}, index=["A"]).to_parquet(p)
    assert build._universe_cache_has_schema(p) is True


def test_universe_only_ticker_clicks_open_something():
    # The generic .ticker-clickable handler called openModal() for names with
    # no DATA payload -- a silent no-op for every universe-only name (Bear
    # watch, outlook rows). It must fall back to the universe-aware opener.
    js = (ROOT / "assets" / "dashboard.js").read_text(encoding="utf-8")
    i = js.index("document.querySelectorAll('.ticker-clickable[data-ticker]').forEach(el => {")
    handler = js[i:js.index("});\n});", i)]
    assert "_safeOpenTicker(t)" in handler and "openModal(t)" not in handler


def test_bear_heading_uses_an_alert_mark_not_an_emoji():
    assert '.bw-head h3::before{content:"!"' in CSS
