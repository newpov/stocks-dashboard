import build


def test_load_prediction_themes(tmp_path, monkeypatch):
    csv = tmp_path / "predictions.csv"
    csv.write_text("theme_label,source,series_or_tag\n"
                   "Fed decision,kalshi,KXFED\n"
                   "Crash,polymarket,will-it-crash\n", encoding="utf-8")
    monkeypatch.setattr(build, "PREDICTIONS_CSV", csv)
    themes = build.load_prediction_themes()
    assert themes == [
        {"theme": "Fed decision", "source": "kalshi", "key": "KXFED"},
        {"theme": "Crash", "source": "polymarket", "key": "will-it-crash"},
    ]


def test_pred_num():
    assert build._pred_num("0.48") == 0.48
    assert build._pred_num(None) != build._pred_num(None)  # nan != nan


def test_parse_kalshi_market():
    m = {"ticker": "KXFED-26", "title": "Fed cuts in July?",
         "yes_bid_dollars": "0.70", "yes_ask_dollars": "0.74",
         "volume_fp": "50000", "expiration_time": "2026-07-30T20:00:00Z",
         "event_ticker": "KXFED-26JUL"}
    rec = build._parse_kalshi_market(m, "Fed decision")
    assert rec["source"] == "kalshi"
    assert rec["theme"] == "Fed decision"
    assert abs(rec["probability"] - 72.0) < 0.01   # mid of 70/74 cents
    assert rec["volume"] == 50000.0
    assert "kalshi.com" in rec["url"]


def test_parse_kalshi_no_price_returns_none():
    assert build._parse_kalshi_market(
        {"title": "x", "yes_bid_dollars": "0", "yes_ask_dollars": "0"},
        "t") is None


def test_kalshi_pick_active_market():
    markets = [
        {"title": "old", "yes_bid_dollars": "0.5", "yes_ask_dollars": "0.5",
         "expiration_time": "2026-01-01T00:00:00Z", "volume_fp": "9999",
         "event_ticker": "E1"},
        {"title": "next", "yes_bid_dollars": "0.6", "yes_ask_dollars": "0.6",
         "expiration_time": "2026-09-01T00:00:00Z", "volume_fp": "9999",
         "event_ticker": "E2"},
    ]
    rec = build._kalshi_pick_active(markets, "Fed")
    assert rec["question"] == "next"   # nearest FUTURE expiry; "old" is in the past


def test_fetch_predictions_routes_and_filters(monkeypatch):
    monkeypatch.setattr(build, "fetch_kalshi", lambda th: [
        {"theme": "Fed", "question": "q", "source": "kalshi",
         "probability": 72.0, "volume": 50000.0, "end_date": "", "url": None}])
    monkeypatch.setattr(build, "fetch_polymarket", lambda th: [
        {"theme": "Crash", "question": "q", "source": "polymarket",
         "probability": 18.0, "volume": 10.0, "end_date": "", "url": None}])
    themes = [{"theme": "Fed", "source": "kalshi", "key": "KXFED"},
              {"theme": "Crash", "source": "polymarket", "key": "x"}]
    rows = build.fetch_predictions(themes)
    assert [r["theme"] for r in rows] == ["Fed"]   # thin Crash filtered by floor


def test_compute_prediction_moves():
    prior = [{"theme": "Fed", "probability": 66.0},
             {"theme": "Recession", "probability": 23.0}]
    current = [{"theme": "Fed", "question": "q", "source": "kalshi",
                "probability": 72.0, "volume": 1.0, "end_date": "", "url": None},
               {"theme": "Recession", "question": "q", "source": "kalshi",
                "probability": 31.0, "volume": 1.0, "end_date": "", "url": None}]
    rows = build.compute_prediction_moves(prior, current)
    by = {r["theme"]: r for r in rows}
    assert by["Fed"]["delta_pp"] == 6.0
    assert by["Recession"]["delta_pp"] == 8.0


def test_compute_prediction_moves_no_prior():
    current = [{"theme": "Fed", "question": "q", "source": "kalshi",
                "probability": 72.0, "volume": 1.0, "end_date": "", "url": None}]
    rows = build.compute_prediction_moves([], current)
    assert rows[0]["delta_pp"] is None     # no prior -> no delta


def test_render_market_expectations():
    rows = [
        {"theme": "US recession 2026", "question": "q", "source": "kalshi",
         "probability": 31.0, "volume": 1.0, "end_date": "", "url": "http://k",
         "delta_pp": 8.0},
        {"theme": "Market crash", "question": "q", "source": "polymarket",
         "probability": 18.0, "volume": 1.0, "end_date": "", "url": None,
         "delta_pp": -4.0},
    ]
    html = build.render_market_expectations(rows, "12 Jun 2026")
    assert "market-expectations-section" in html
    assert "US recession 2026" in html
    assert "31%" in html
    assert "kalshi" in html and "polymarket" in html
    assert "me-up" in html and "me-down" in html   # delta direction classes


def test_render_market_expectations_empty():
    assert "me-empty" in build.render_market_expectations([], "12 Jun 2026")


def test_render_market_expectations_legend():
    rows = [{"theme": "US recession 2026", "question": "q", "source": "kalshi",
             "probability": 31.0, "volume": 1.0, "end_date": "", "url": None,
             "delta_pp": 8.0}]
    html = build.render_market_expectations(rows, "12 Jun 2026")
    assert "me-legend" in html
    assert "market-implied probability" in html


def test_parse_polymarket_market():
    m = {"question": "Will the market crash in 2026?",
         "outcomes": "[\"Yes\", \"No\"]", "outcomePrices": "[\"0.18\", \"0.82\"]",
         "volume": "250000", "endDate": "2026-12-31T00:00:00Z",
         "slug": "will-it-crash"}
    rec = build._parse_polymarket_market(m, "Market crash")
    assert rec["source"] == "polymarket"
    assert abs(rec["probability"] - 18.0) < 0.01
    assert rec["volume"] == 250000.0
    assert "polymarket.com" in rec["url"]


def test_render_bigbrain_macro_fires_above_threshold():
    top = {"theme": "US recession in 2026", "probability": 31.0, "delta_pp": 8.0}
    html = build.render_bigbrain_macro(top, 0.95)
    assert "bb-macro" in html
    assert "recession" in html.lower()
    assert "8pp" in html


def test_render_bigbrain_macro_silent_below_threshold():
    top = {"theme": "x", "probability": 50.0, "delta_pp": 3.0}
    assert build.render_bigbrain_macro(top, 0.95) == ""
    assert build.render_bigbrain_macro(None, 0.95) == ""


def test_render_bigbrain_passes_macro_html():
    html = build.render_bigbrain([], "05 Jun 2026",
                                 macro_html='<div class="bb-macro">X</div>')
    assert "bb-macro" in html
