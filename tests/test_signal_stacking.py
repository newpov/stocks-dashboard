"""v3.4 #2: Signal stacking — non-owned recurrence memory. Pure + offline."""
import inspect

import pandas as pd

import build


# --- Task 1: record_signal_history -------------------------------------------

def test_record_appends_and_is_idempotent_same_day(tmp_path):
    p = tmp_path / "sig.parquet"
    day = pd.Timestamp("2026-07-03")
    flagged = {"BB": ["AAA", "BBB"], "VS": ["AAA"]}
    out = build.record_signal_history(flagged, day, history_path=p)
    assert len(out) == 3
    assert set(out.columns) == {"date", "ticker", "source"}
    out2 = build.record_signal_history({"BB": ["AAA"]}, day, history_path=p)
    assert len(out2) == 1                       # same-day replace, not append


def test_record_prunes_old_rows(tmp_path):
    p = tmp_path / "sig.parquet"
    build.record_signal_history({"BB": ["OLD"]}, pd.Timestamp("2026-01-01"),
                                history_path=p, keep_days=90)
    out = build.record_signal_history({"BB": ["NEW"]}, pd.Timestamp("2026-07-03"),
                                      history_path=p, keep_days=90)
    assert "OLD" not in set(out["ticker"])
    assert "NEW" in set(out["ticker"])


def test_record_empty_flagged_safe(tmp_path):
    p = tmp_path / "sig.parquet"
    out = build.record_signal_history({}, pd.Timestamp("2026-07-03"), history_path=p)
    assert list(out.columns) == ["date", "ticker", "source"]
    assert out.empty


def test_is_rm_upgrade():
    assert build._is_rm_upgrade({"kind": "target", "pct_change": 8.0}) is True
    assert build._is_rm_upgrade({"kind": "target", "pct_change": -4.0}) is False
    assert build._is_rm_upgrade({"kind": "recommendation", "before": "hold", "after": "buy"}) is True
    assert build._is_rm_upgrade({"kind": "recommendation", "before": "buy", "after": "hold"}) is False


# --- Task 2: compute_signal_stacking -----------------------------------------

def _hist(rows):
    return pd.DataFrame([{"date": pd.Timestamp(d), "ticker": t, "source": s}
                        for (d, t, s) in rows])


def test_compute_counts_distinct_days_and_engines():
    h = _hist([("2026-07-01", "AAA", "BB"), ("2026-07-01", "AAA", "VS"),
               ("2026-07-02", "AAA", "BB"), ("2026-07-03", "AAA", "RE")])
    out = build.compute_signal_stacking(h, "2026-07-03")
    assert len(out) == 1
    r = out[0]
    assert r["ticker"] == "AAA"
    assert r["days"] == 3                        # distinct dates, not 4 rows
    assert r["engines"] == ["BB", "RE", "VS"]    # sorted set


def test_compute_min_days_drops_one_offs():
    h = _hist([("2026-07-01", "AAA", "BB"), ("2026-07-02", "AAA", "BB"),
               ("2026-07-02", "ONE", "VS")])
    out = build.compute_signal_stacking(h, "2026-07-03", min_days=2)
    assert [r["ticker"] for r in out] == ["AAA"]


def test_compute_calendar_month_filter():
    h = _hist([("2026-06-28", "OLD", "BB"), ("2026-06-29", "OLD", "BB"),
               ("2026-07-01", "NEW", "BB"), ("2026-07-02", "NEW", "BB")])
    out = build.compute_signal_stacking(h, "2026-07-03", min_days=2)
    assert [r["ticker"] for r in out] == ["NEW"]  # June excluded


def test_compute_hot_flag_boundary():
    h = _hist([("2026-07-04", "AAA", "BB"), ("2026-07-06", "AAA", "BB"),
               ("2026-07-10", "AAA", "BB"), ("2026-07-01", "AAA", "BB")])
    out = build.compute_signal_stacking(h, "2026-07-10", hot_window=7, hot_min=3)
    assert out[0]["hot"] is True
    out2 = build.compute_signal_stacking(h, "2026-07-10", hot_window=7, hot_min=4)
    assert out2[0]["hot"] is False


def test_compute_ranks_by_engines_then_days():
    # CCC: 3 engines on a single day. DDD: 1 engine across 3 days.
    # Breadth (agreement) outranks persistence.
    h = _hist([("2026-07-03", "CCC", "BB"), ("2026-07-03", "CCC", "VS"),
               ("2026-07-03", "CCC", "RE"),
               ("2026-07-01", "DDD", "BB"), ("2026-07-02", "DDD", "BB"),
               ("2026-07-03", "DDD", "BB")])
    out = build.compute_signal_stacking(h, "2026-07-03")
    assert [r["ticker"] for r in out] == ["CCC", "DDD"]   # 3 engines > 1 engine
    assert out[0]["days"] == 1 and len(out[0]["engines"]) == 3


def test_compute_qualifies_on_engine_breadth_same_day():
    # Day-one usefulness: a name flagged by 2 engines on ONE day shows;
    # a single-engine single-day name does not.
    h = _hist([("2026-07-03", "AGREE", "BB"), ("2026-07-03", "AGREE", "VS"),
               ("2026-07-03", "SOLO", "BB")])
    out = build.compute_signal_stacking(h, "2026-07-03")
    assert [r["ticker"] for r in out] == ["AGREE"]


def test_min_engines_constant_and_default():
    assert build.SIGNAL_STACK_MIN_ENGINES == 2


def test_compute_excludes_owned_and_attaches_price_name():
    h = _hist([("2026-07-01", "AAA", "BB"), ("2026-07-02", "AAA", "BB"),
               ("2026-07-01", "OWN", "BB"), ("2026-07-02", "OWN", "BB")])
    out = build.compute_signal_stacking(
        h, "2026-07-03", price_lookup={"AAA": (12.5, "$")},
        name_lookup={"AAA": "Aco"}, owned={"OWN"}, min_days=2)
    assert [r["ticker"] for r in out] == ["AAA"]
    assert out[0]["price"] == 12.5 and out[0]["ccy_symbol"] == "$"
    assert out[0]["name"] == "Aco"


def test_compute_empty_history_safe():
    assert build.compute_signal_stacking(None, "2026-07-03") == []
    assert build.compute_signal_stacking(pd.DataFrame(), "2026-07-03") == []


# --- Task 3: render_signal_stacking ------------------------------------------

def _rows():
    return [
        {"ticker": "SNDK", "name": "Sandisk", "price": 45.2, "ccy_symbol": "$",
         "days": 6, "engines": ["BB", "RE", "VS"], "hot": True},
        {"ticker": "EFX", "name": "Equifax", "price": 250.0, "ccy_symbol": "$",
         "days": 3, "engines": ["BB"], "hot": False},
    ]


def test_render_panel_has_ids_rows_and_chips():
    html = build.render_signal_stacking(_rows(), "03 Jul 2026")
    assert 'id="signal-stacking-wrap"' in html
    assert "Signal stacking" in html
    assert "as of 03 Jul 2026" in html
    assert "Seen 6d" in html and "Seen 3d" in html
    assert "$45.20" in html
    assert 'data-ticker="SNDK"' in html and "ticker-clickable" in html
    assert "ss-eng" in html
    assert "hot" in html.lower()


def test_render_empty_state():
    html = build.render_signal_stacking([], "03 Jul 2026")
    assert 'id="signal-stacking-wrap"' in html
    assert "No stacked signals yet" in html


def test_render_escapes_adversarial():
    rows = [{"ticker": "<b>", "name": "<img src=x onerror=alert(1)>",
             "price": None, "ccy_symbol": "$", "days": 2,
             "engines": ["BB"], "hot": False}]
    html = build.render_signal_stacking(rows, "x")
    assert "<img src=x" not in html
    assert "&lt;" in html
    assert "&mdash;" in html                      # missing price renders as dash


# --- Task 4/5: wiring guards -------------------------------------------------

def test_signal_stacking_computed_in_render_html():
    src = inspect.getsource(build.render_html) + build._read_asset("dashboard.css") + build._read_asset("dashboard.js")  # v3.5: page source includes the inlined assets
    assert "compute_signal_stacking(" in src
    assert "render_signal_stacking(" in src
    assert "signal_stacking_html" in src


def test_signal_stacking_button_and_toggle_in_page_source():
    src = inspect.getsource(build.render_html) + build._read_asset("dashboard.css") + build._read_asset("dashboard.js")  # v3.5: page source includes the inlined assets
    assert 'id="signal-stacking-btn"' in src
    assert "{signal_stacking_html}" in src
    assert "setupSignalStacking" in src
    assert "signalStackingOn" in src


# --- Signal-stacking non-owned names get a clickable analyst card ------------

def _universe_frame():
    return pd.DataFrame({
        "current_price": {"SNDK": 45.0, "OWN": 10.0},
        "target_mean":   {"SNDK": 60.0, "OWN": 12.0},
        "num_analysts":  {"SNDK": 15,   "OWN": 4},
        "recommendation":{"SNDK": "buy", "OWN": "hold"},
        "name":          {"SNDK": "Sandisk", "OWN": "Owned Co"},
    })


def test_build_signal_modal_cards_from_universe():
    cards = build.build_signal_modal_cards(
        ["SNDK", "OWN"], _universe_frame(), analyst=None, owned={"OWN"})
    assert "SNDK" in cards and "OWN" not in cards
    d = cards["SNDK"]
    assert "Sandisk" in d["sub"]
    # implied upside (60/45-1)*100 = 33.3%
    assert "33.3%" in d["html"]
    # no analyst target range in the universe frame -> range line omitted
    assert "Target range" not in d["html"]
    # no rating-move context here
    assert "Flagged" not in d["html"]


def test_build_signal_modal_cards_empty_safe():
    assert build.build_signal_modal_cards([], None, None, set()) == {}
    assert build.build_signal_modal_cards(["X"], pd.DataFrame(), None, set()) == {}


def test_signal_cards_merged_into_rm_analyst_in_render_html():
    src = inspect.getsource(build.render_html) + build._read_asset("dashboard.css") + build._read_asset("dashboard.js")  # v3.5: page source includes the inlined assets
    assert "build_signal_modal_cards(" in src
