import pandas as pd

import build


def _flag(fid, domain, weight, direction=0):
    return {"id": fid, "domain": domain, "weight": weight, "dir": direction,
            "pill": fid, "frag": fid}


def test_score_sums_weights_with_single_domain():
    flags = [_flag("a", "trend", 1.5), _flag("b", "trend", 1.0)]
    score, raw, n_domains = build._bb_score(flags)
    assert raw == 2.5
    assert n_domains == 1
    assert score == 2.5  # multiplier 1.0


def test_diversity_multiplier_rewards_cross_panel():
    one = [_flag("a", "trend", 1.0), _flag("b", "trend", 1.0),
           _flag("c", "trend", 1.0), _flag("d", "trend", 1.0)]
    spread = [_flag("a", "trend", 1.0), _flag("b", "flow", 1.0),
              _flag("c", "street", 1.0), _flag("d", "news", 1.0)]
    assert build._bb_score(one)[0] == 4.0           # mult 1.0
    assert build._bb_score(spread)[0] == 4.0 * 1.75  # mult 1+0.25*3


def test_score_multiplier_caps_at_two():
    flags = [_flag(f"f{i}", d, 1.0) for i, d in
             enumerate(["position", "trend", "flow", "street", "news"])]
    score, raw, n = build._bb_score(flags)
    assert n == 5
    assert score == raw * 2.0  # 1+0.25*4 = 2.0, capped


def test_score_empty():
    assert build._bb_score([]) == (0.0, 0.0, 0)


def test_severity_warn_when_bearish_on_big_position():
    flags = [_flag("top_weight", "position", 2.5, 0),
             _flag("downgrade", "street", 2.0, -1),
             _flag("downtrend", "trend", 1.0, -1)]
    assert build._bb_severity(flags) == "warn"


def test_severity_watch_on_strong_bear_without_size():
    flags = [_flag("overbought", "trend", 2.0, -1)]
    assert build._bb_severity(flags) == "watch"


def test_severity_good_when_constructive():
    flags = [_flag("upgrade", "street", 1.5, 1),
             _flag("big_upside", "street", 1.0, 1)]
    assert build._bb_severity(flags) == "good"


def test_archetype_exhausted_winner():
    assert build._bb_match_archetype(
        {"near_high", "overbought", "fading_volume"}) == "exhausted_winner"


def test_archetype_heavy_bleeder():
    assert build._bb_match_archetype(
        {"downtrend", "top_weight", "downgrade"}) == "heavy_bleeder"


def test_archetype_divergence():
    assert build._bb_match_archetype({"downgrade", "near_high"}) == "divergence"
    assert build._bb_match_archetype({"upgrade", "near_low"}) == "divergence"


def test_archetype_crowded_strength():
    assert build._bb_match_archetype(
        {"top_weight", "big_contributor", "extended"}) == "crowded_strength"


def test_archetype_quiet_breakout_excludes_downtrend():
    assert build._bb_match_archetype(
        {"unusual_volume", "upgrade"}) == "quiet_breakout"
    assert build._bb_match_archetype(
        {"unusual_volume", "upgrade", "downtrend"}) != "quiet_breakout"


def test_archetype_none_returns_none():
    assert build._bb_match_archetype({"big_move_1w"}) is None


def _row(**kw):
    base = {"status": "open", "weight": 100.0, "total_pct": 10.0,
            "1w_pct": 2.0, "1m_pct": 3.0, "3m_pct": 4.0, "latest": 50.0,
            "baseline_date": pd.Timestamp("2025-01-01")}
    base.update(kw)
    return pd.Series(base)


def _q(**kw):
    base = {"rsi14": 50.0, "sma200_dist_pct": 0.0, "range52w_pct": 50.0,
            "vol_ratio": 1.0, "atr14_pct": 2.0}
    base.update(kw)
    return pd.Series(base)


def test_flag_overbought_and_near_high_fire():
    flags = build._bb_flags_for(
        "AAA", _row(), _q(rsi14=82.0, range52w_pct=96.0), "Strong uptrend",
        None, [], [], weight_rank=10, is_top_contrib=False,
        is_bottom_contrib=False, now=pd.Timestamp("2026-06-04", tz="UTC"))
    ids = {f["id"] for f in flags}
    assert "overbought" in ids
    assert "near_high" in ids


def test_flag_overbought_does_not_fire_below_threshold():
    flags = build._bb_flags_for(
        "AAA", _row(), _q(rsi14=65.0), "Mixed", None, [], [],
        weight_rank=10, is_top_contrib=False, is_bottom_contrib=False,
        now=pd.Timestamp("2026-06-04", tz="UTC"))
    assert "overbought" not in {f["id"] for f in flags}


def test_flag_top_weight_and_downgrade():
    moves = [{"ticker": "AAA", "kind": "target", "before": 100.0,
              "after": 90.0, "pct_change": -10.0, "abs_pct": 10.0}]
    flags = build._bb_flags_for(
        "AAA", _row(), _q(), "Trending down", None, moves, [],
        weight_rank=2, is_top_contrib=False, is_bottom_contrib=False,
        now=pd.Timestamp("2026-06-04", tz="UTC"))
    ids = {f["id"] for f in flags}
    assert "top_weight" in ids
    assert "downgrade" in ids
    assert "downtrend" in ids
    assert next(f for f in flags if f["id"] == "top_weight")["weight"] == 2.5


def test_post_exit_flag_on_sold():
    row = _row(status="closed", post_exit_pct=18.0)
    flags = build._bb_flags_for(
        "AAA", row, _q(), "Trending up", None, [], [],
        weight_rank=None, is_top_contrib=False, is_bottom_contrib=False,
        now=pd.Timestamp("2026-06-04", tz="UTC"))
    f = {x["id"]: x for x in flags}
    assert "post_exit" in f
    assert "18" in f["post_exit"]["pill"]


def test_post_exit_absent_on_open():
    flags = build._bb_flags_for(
        "AAA", _row(status="open"), _q(), "Mixed", None, [], [],
        weight_rank=3, is_top_contrib=False, is_bottom_contrib=False,
        now=pd.Timestamp("2026-06-04", tz="UTC"))
    assert "post_exit" not in {x["id"] for x in flags}


def test_news_flurry_flag_removed():
    now = pd.Timestamp("2026-06-04", tz="UTC")
    items = [{"title": f"h{i}", "link": "x", "publisher": "P",
              "published": "2026-06-03T00:00:00Z"} for i in range(5)]
    flags = build._bb_flags_for(
        "AAA", _row(), _q(), "Mixed", None, [], items,
        weight_rank=10, is_top_contrib=False, is_bottom_contrib=False,
        now=now)
    assert "news_flurry" not in {f["id"] for f in flags}


def test_flag_big_upside_from_analyst():
    an = pd.Series({"target_mean": 80.0, "recommendation": "buy"})
    flags = build._bb_flags_for(
        "AAA", _row(latest=50.0), _q(), "Mixed", an, [], [],
        weight_rank=10, is_top_contrib=False, is_bottom_contrib=False,
        now=pd.Timestamp("2026-06-04", tz="UTC"))
    assert "big_upside" in {f["id"] for f in flags}  # (80/50-1)=60% >= 25%


def test_news_cite_picks_most_recent_in_window():
    now = pd.Timestamp("2026-06-04", tz="UTC")
    items = [{"title": "Older", "link": "u1", "publisher": "Reuters",
              "published": "2026-06-01T00:00:00Z"},
             {"title": "Newer", "link": "u2", "publisher": "Bloomberg",
              "published": "2026-06-03T00:00:00Z"}]
    cite = build._bb_news_cite(items, now)
    assert cite["title"] == "Newer"
    assert cite["publisher"] == "Bloomberg"
    assert cite["link"] == "u2"


def test_news_cite_omits_when_stale():
    now = pd.Timestamp("2026-06-04", tz="UTC")
    items = [{"title": "Old", "link": "u", "publisher": "P",
              "published": "2026-01-01T00:00:00Z"}]
    assert build._bb_news_cite(items, now) is None


def test_news_cite_empty_and_malformed():
    now = pd.Timestamp("2026-06-04", tz="UTC")
    assert build._bb_news_cite([], now) is None
    assert build._bb_news_cite([{"title": "x"}], now) is None  # no link/date


def test_narrative_archetype_uses_tag_and_mentions_ticker():
    flags = [_flag("near_high", "trend", 1.0),
             _flag("overbought", "trend", 2.0, -1),
             _flag("fading_volume", "flow", 1.0, -1)]
    flags[1]["frag"] = "overbought at RSI 82"
    tag, body = build._bb_narrative("AAA", flags, "exhausted_winner")
    assert tag == "Running hot"
    assert "AAA" in body


def test_archetype_and_narrative_sold():
    assert build._bb_match_archetype({"post_exit", "near_high"}) == "ran_without_you"
    flags = [_flag("post_exit", "position", 1.5, 0)]
    flags[0]["frag"] = "up 18% since you sold"
    tag, body = build._bb_narrative("PLTR", flags, "ran_without_you")
    assert tag == "Ran without you"
    assert "PLTR" in body


def test_narrative_fallback_lists_fragments():
    flags = [_flag("big_move_1w", "flow", 1.0)]
    flags[0]["frag"] = "moved +12% this week"
    tag, body = build._bb_narrative("BBB", flags, None)
    assert tag == "Signals stacking"
    assert "BBB" in body
    assert "moved +12% this week" in body


def test_compute_two_lane_top4_and_ownership():
    idx = ["AAA", "BBB", "CCC", "DDD"]
    returns = pd.DataFrame({
        "status": ["open", "open", "closed", "open"],
        "weight": [300.0, 200.0, 0.0, 100.0],
        "total_pct": [10.0, -5.0, 40.0, 80.0],
        "post_exit_pct": [float("nan"), float("nan"), 22.0, float("nan")],
        "1w_pct": [12.0, -2.0, 1.0, 1.0],
        "1m_pct": [5.0, -3.0, 2.0, 2.0],
        "3m_pct": [6.0, -4.0, 3.0, 3.0],
        "latest": [50.0, 40.0, 30.0, 30.0],
        "baseline_date": [pd.Timestamp("2024-01-01")] * 4,
    }, index=idx)
    contrib = pd.DataFrame({"contribution_pp": [3.0, -2.0, 0.0, 1.0]}, index=idx)
    quant = pd.DataFrame({
        "rsi14": [82.0, 45.0, 72.0, 50.0],
        "sma200_dist_pct": [120.0, 0.0, 30.0, 10.0],
        "range52w_pct": [96.0, 8.0, 95.0, 50.0],
        "vol_ratio": [2.5, 1.0, 1.2, 1.0],
        "atr14_pct": [3.0, 2.0, 2.0, 2.0],
    }, index=idx)
    signals = pd.DataFrame({"signal": ["Strong uptrend", "Trending down",
                                        "Near 12M high", "Mixed"]}, index=idx)
    obs = build.compute_bigbrain_observations(
        returns, meta=pd.DataFrame(), contrib=contrib, quant_metrics=quant,
        signals=signals, analyst=None, rating_moves=[], ticker_news=None)
    assert len(obs) <= 4
    owners = {o["ticker"]: o["ownership"] for o in obs}
    assert owners.get("CCC") == "sold"          # closed -> sold
    assert owners.get("AAA") == "held"          # open -> held
    for o in obs:
        assert o["ownership"] in ("held", "sold", "idea")


def test_compute_empty_when_no_positions():
    empty = pd.DataFrame(columns=["status", "weight"])
    assert build.compute_bigbrain_observations(
        empty, pd.DataFrame(), pd.DataFrame(), None, None, None, [], None) == []


def test_merge_lanes_quota_and_backfill():
    uni = [{"ticker": "U1", "score": 5.0, "raw": 4.0},
           {"ticker": "U2", "score": 4.0, "raw": 3.0},
           {"ticker": "U3", "score": 3.0, "raw": 2.0}]
    port = [{"ticker": "P1", "score": 9.0, "raw": 6.0}]
    out = build._bb_merge_lanes(uni, port, n=4, per_lane=2)
    tickers = [o["ticker"] for o in out]
    assert tickers[:2] == ["U1", "U2"]      # universe first, top 2
    assert "P1" in tickers                    # portfolio's one
    assert "U3" in tickers                    # backfilled to reach 4
    assert len(out) == 4


def test_render_board_held_and_idea():
    obs = [
        {"ticker": "NVDA", "ownership": "idea", "severity": "good",
         "title": "Setup you're missing", "body": "NVDA is stacking.",
         "pills": ["+58% 12m", "2.1x vol"],
         "cite": {"title": "Nvidia surges", "link": "http://x", "publisher": "CNBC"},
         "score": 9.0, "raw": 6.0},
        {"ticker": "BRK-B", "ownership": "held", "severity": "warn",
         "title": "Bleeding", "body": "BRK-B is #2 weight bleeding.",
         "pills": ["#2 weight", "downtrend"], "cite": None,
         "score": 7.0, "raw": 5.0},
    ]
    html = build.render_bigbrain(obs, "05 Jun 2026")
    assert "bigbrain-section" in html
    assert "bb-tier-idea" in html and "bb-tier-warn" in html
    assert 'data-ticker="BRK-B"' in html          # held is clickable
    assert 'data-ticker="NVDA"' not in html        # idea NOT clickable
    assert "not owned" in html                      # ownership badge
    assert "4 names punching above the noise this week" in html
    assert "Nvidia surges" in html
    assert 'rel="noopener noreferrer"' in html


def test_render_empty_state():
    html = build.render_bigbrain([], "05 Jun 2026")
    assert "bb-empty" in html


def test_log_bigbrain_flags_appends_dedup(tmp_path, monkeypatch):
    p = tmp_path / "bigbrain_log.csv"
    monkeypatch.setattr(build, "BIGBRAIN_LOG_CSV", p)
    obs = [{"ticker": "AMD", "ownership": "held", "title": "Running hot"},
           {"ticker": "NVDA", "ownership": "idea", "title": "Setup you're missing"}]
    prices = {"AMD": 168.4, "NVDA": 120.0}
    build.log_bigbrain_flags(obs, "2026-06-12", prices)
    build.log_bigbrain_flags(obs, "2026-06-12", prices)   # same day -> no dupes
    df = pd.read_csv(p)
    assert len(df) == 2
    assert set(df["ticker"]) == {"AMD", "NVDA"}
    assert float(df[df.ticker == "AMD"]["price"].iloc[0]) == 168.4


def test_compute_bigbrain_memory():
    log = pd.DataFrame([
        {"date": "2026-05-22", "ticker": "AMD", "ownership": "held",
         "price": "150.0", "label": "Running hot"},
        {"date": "2026-06-12", "ticker": "AMD", "ownership": "held",
         "price": "168.0", "label": "Running hot"},
        {"date": "2026-06-12", "ticker": "MU", "ownership": "held",
         "price": "100.0", "label": "x"},   # only today -> no note
    ])
    notes = build.compute_bigbrain_memory(log, {"AMD": 168.0, "MU": 100.0},
                                          "2026-06-12")
    assert "AMD" in notes
    assert "+12%" in notes["AMD"]    # 168/150-1
    assert "ago" in notes["AMD"]
    assert "MU" not in notes


def test_compute_bigbrain_memory_skips_no_price():
    log = pd.DataFrame([
        {"date": "2026-05-01", "ticker": "X", "ownership": "idea",
         "price": "10.0", "label": "y"},
        {"date": "2026-06-12", "ticker": "X", "ownership": "idea",
         "price": "", "label": "y"},
    ])
    assert build.compute_bigbrain_memory(log, {}, "2026-06-12") == {}


def test_render_bigbrain_memory_note():
    obs = [{"ticker": "AMD", "ownership": "held", "severity": "watch",
            "title": "Running hot", "body": "hot", "pills": ["RSI 71"],
            "cite": None, "score": 5.0, "raw": 4.0}]
    html = build.render_bigbrain(obs, "12 Jun 2026",
                                 memory={"AMD": "flagged 3 weeks ago &mdash; +12% since"})
    assert "bb-memory" in html
    assert "+12% since" in html


def test_quadrant_signal_score_bullish():
    q = pd.Series({"rsi14": 78.0, "sma200_dist_pct": 40.0})
    row = pd.Series({"1m_pct": 12.0, "3m_pct": 20.0})
    strength, direction = build._quadrant_signal_score(q, "Strong uptrend", row)
    assert strength > 50
    assert direction == 1


def test_quadrant_signal_score_bearish():
    q = pd.Series({"rsi14": 28.0, "sma200_dist_pct": -30.0})
    row = pd.Series({"1m_pct": -10.0, "3m_pct": -18.0})
    strength, direction = build._quadrant_signal_score(q, "Strong downtrend", row)
    assert direction == -1


def test_quadrant_signal_score_nan_safe():
    strength, direction = build._quadrant_signal_score(None, "", {})
    assert strength == 0.0
    assert direction in (1, -1)


def test_build_quadrant_data():
    returns = pd.DataFrame({
        "status": ["open", "open", "closed"],
        "weight": [300.0, 100.0, 0.0],
        "1m_pct": [10.0, -5.0, 0.0], "3m_pct": [12.0, -8.0, 0.0],
    }, index=["AMD", "SAP", "OLD"])
    quant = pd.DataFrame({"rsi14": [75.0, 30.0, 50.0],
                          "sma200_dist_pct": [40.0, -20.0, 0.0]},
                         index=["AMD", "SAP", "OLD"])
    signals = pd.DataFrame({"signal": ["Strong uptrend", "Trending down", "Mixed"]},
                           index=["AMD", "SAP", "OLD"])
    data = build.build_quadrant_data(returns, quant, signals)
    assert {d["ticker"] for d in data} == {"AMD", "SAP"}    # open only
    amd = next(d for d in data if d["ticker"] == "AMD")
    assert 0 <= amd["weight_share"] <= 1
    assert amd["direction"] == 1
    assert round(sum(d["weight_share"] for d in data), 5) == 1.0


def test_render_quadrant():
    data = [{"ticker": "AMD", "weight_share": 0.5, "strength": 80.0, "direction": 1},
            {"ticker": "SAP", "weight_share": 0.2, "strength": 70.0, "direction": -1}]
    html = build.render_quadrant(data)
    assert "quadrant-section" in html
    assert "<svg" in html
    assert html.count("<circle") == 2
    assert "dot-up" in html and "dot-down" in html
    assert 'data-ticker="AMD"' in html


def test_render_quadrant_labels_only_outliers():
    data = [{"ticker": f"T{i}", "weight_share": (i + 1) / 200.0,
             "strength": float(i * 2), "direction": 1 if i % 2 else -1}
            for i in range(40)]
    html = build.render_quadrant(data)
    assert html.count("<circle") == 40          # every holding is a dot
    assert html.count('class="q-tkr"') <= 12     # only standouts labelled


def test_render_quadrant_empty():
    assert build.render_quadrant([]) == ""
    assert build.render_quadrant([{"ticker": "A", "weight_share": 1.0,
                                   "strength": 5.0, "direction": 1}]) == ""


def test_analyst_snapshot_due(tmp_path):
    import os, time
    p = tmp_path / "prior.parquet"
    now = time.time()
    assert build._analyst_snapshot_due(p, now) is True            # missing -> due
    p.write_text("x")
    os.utime(p, (now, now))
    assert build._analyst_snapshot_due(p, now, max_age_days=6) is False   # fresh
    os.utime(p, (now - 7 * 86400, now - 7 * 86400))
    assert build._analyst_snapshot_due(p, now, max_age_days=6) is True    # stale


def test_archetype_idea():
    assert build._bb_match_archetype({"beats_your_sector", "unusual_volume"}) == "missing_idea"


def test_idea_fallback_title_is_neutral_not_missing():
    # No beats_your_sector (empty sector_avg) and no archetype match -> the
    # title must NOT over-claim "Setup you're missing"; it should read neutral.
    quant = pd.DataFrame({"rsi14": [29.0], "sma200_dist_pct": [0.0],
                          "range52w_pct": [8.0], "vol_ratio": [1.0],
                          "atr14_pct": [2.0]}, index=["UHS"])
    signals = pd.DataFrame({"signal": ["Trending down"]}, index=["UHS"])
    outlook = pd.DataFrame({"ret_12m": [5.0], "industry": ["Health Care"]},
                           index=["UHS"])
    obs = build._bb_build_universe_observations(
        ["UHS"], quant, signals, None, None, sector_avg={}, outlook=outlook)
    assert len(obs) == 1
    assert obs[0]["title"] == "On the radar"


def test_idea_missing_title_when_relational():
    # When beats_your_sector fires, the title earns "Setup you're missing".
    quant = pd.DataFrame({"rsi14": [68.0], "sma200_dist_pct": [40.0],
                          "range52w_pct": [92.0], "vol_ratio": [2.2],
                          "atr14_pct": [3.0]}, index=["UBER"])
    signals = pd.DataFrame({"signal": ["Strong uptrend"]}, index=["UBER"])
    outlook = pd.DataFrame({"ret_12m": [58.0], "industry": ["Software"]},
                           index=["UBER"])
    obs = build._bb_build_universe_observations(
        ["UBER"], quant, signals, None, None,
        sector_avg={"Software": 20.0}, outlook=outlook)
    assert obs[0]["title"] == "Setup you're missing"


def test_build_universe_observations():
    idx = ["NVDA"]
    quant = pd.DataFrame({"rsi14": [68.0], "sma200_dist_pct": [40.0],
                          "range52w_pct": [92.0], "vol_ratio": [2.2],
                          "atr14_pct": [3.0]}, index=idx)
    signals = pd.DataFrame({"signal": ["Strong uptrend"]}, index=idx)
    outlook = pd.DataFrame({"ret_12m": [58.0], "industry": ["Semis"]}, index=idx)
    obs = build._bb_build_universe_observations(
        ["NVDA"], quant, signals, None, None,
        sector_avg={"Semis": 20.0}, outlook=outlook)
    assert len(obs) == 1
    assert obs[0]["ownership"] == "idea"
    assert obs[0]["ticker"] == "NVDA"


def test_sector_avg_returns():
    returns = pd.DataFrame({"status": ["open", "open"], "total_pct": [10.0, 30.0]},
                           index=["A", "B"])
    meta = pd.DataFrame({"industry": ["Semis", "Semis"], "sector": ["Tech", "Tech"]},
                        index=["A", "B"])
    avg = build._bb_sector_avg_returns(returns, meta)
    assert round(avg["Semis"], 1) == 20.0


def test_beats_your_sector_flag():
    flags = build._bb_flags_for(
        "NVDA", _row(status="universe"), _q(), "Strong uptrend", None, [], [],
        weight_rank=None, is_top_contrib=False, is_bottom_contrib=False,
        now=pd.Timestamp("2026-06-04", tz="UTC"), beats_sector_gap=25.0,
        sector_name="Semis")
    assert "beats_your_sector" in {x["id"] for x in flags}


def test_universe_shortlist_excludes_and_ranks():
    uni = pd.DataFrame({
        "ret_12m": [60.0, 10.0, 55.0, 80.0],
        "upside": [40.0, 5.0, 35.0, 2.0],
        "num_analysts": [30, 5, 28, 3],
        "recommendation": ["buy", "hold", "strong_buy", "hold"],
    }, index=["NVDA", "XOM", "AVGO", "HELD1"])
    out = build._bb_universe_shortlist(uni, exclude={"HELD1"}, n=2)
    assert "HELD1" not in out
    assert out[0] in ("NVDA", "AVGO")   # high upside x coverage + momentum
    assert len(out) == 2
