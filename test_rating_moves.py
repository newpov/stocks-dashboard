"""v2.7 #4: Rating-moves redesign — split into two groups.

The panel now renders TWO groups so both kinds always show:
  - Price targets: target moves, upsides first then cuts by magnitude; each row
    also shows the current recommendation.
  - Recommendations: rec changes with direction (green up / red down),
    upgrades/initiations ranked above downgrades. Each row shows the current
    target as context.

compute_rating_moves still returns the raw per-kind rows (Big Brain consumes
kind=="target"/pct_change), now enriched with cur_rec/cur_target.
"""
import pandas as pd

import build


def _cache(tmp_path, name, tickers, target, recs):
    df = pd.DataFrame({"target_mean": target, "recommendation": recs}, index=tickers)
    p = tmp_path / name
    df.to_parquet(p)
    return p


def _moves(tmp_path, tickers, prior_t, cur_t, prior_r, cur_r):
    prior = _cache(tmp_path, "p.parquet", tickers, prior_t, prior_r)
    cur = _cache(tmp_path, "c.parquet", tickers, cur_t, cur_r)
    return build.compute_rating_moves(prior, pd.read_parquet(cur), max_results=999)


# --- _rec_direction (core sort logic) ---------------------------------------

def test_rec_direction():
    assert build._rec_direction("hold", "buy") == "up"          # upgrade
    assert build._rec_direction("buy", "hold") == "down"        # downgrade
    assert build._rec_direction("none", "buy") == "up"          # initiation
    assert build._rec_direction("buy", "none") == "down"        # coverage drop
    assert build._rec_direction("buy", "outperform") is None    # lateral (same rank)


# --- compute attaches current context ---------------------------------------

def test_compute_attaches_current_rec_and_target(tmp_path):
    moves = _moves(tmp_path, ["AAA"], [100.0], [120.0], ["hold"], ["buy"])
    for m in moves:
        assert m["cur_rec"] == "buy"
        assert m["cur_target"] == 120.0


# --- render: two groups -----------------------------------------------------

def test_render_has_both_group_labels(tmp_path):
    nan = float("nan")
    moves = _moves(tmp_path, ["AAA", "BBB"], [100.0, nan], [120.0, nan],
                   ["buy", "hold"], ["buy", "buy"])   # AAA target move; BBB rec change
    html = build.render_rating_moves(moves, prior_exists=True)
    assert "Price targets" in html
    assert "Recommendations" in html


def test_render_target_shows_before_after_pct_and_rec(tmp_path):
    moves = _moves(tmp_path, ["AAA"], [100.0], [120.0], ["buy"], ["buy"])
    html = build.render_rating_moves(moves, prior_exists=True)
    assert "$100.00" in html and "$120.00" in html
    assert "+20" in html
    assert "BUY" in html               # current rec shown on the target row
    assert "Price targets" in html
    assert "Recommendations" not in html   # no rec change -> no rec group


def test_render_targets_upsides_first(tmp_path):
    # UP +30%, DN -40%. Upsides must come first despite the cut being larger.
    moves = _moves(tmp_path, ["UP", "DN"], [100.0, 100.0], [130.0, 60.0],
                   ["", ""], ["", ""])
    html = build.render_rating_moves(moves, prior_exists=True)
    assert html.index("UP") < html.index("DN")


def test_render_recs_upgrades_first_cohr_before_amat(tmp_path):
    nan = float("nan")
    # COHR none->buy (initiation/upgrade); AMAT strong_buy->buy (downgrade).
    moves = _moves(tmp_path, ["AMAT", "COHR"], [nan, nan], [nan, nan],
                   ["strong_buy", "none"], ["buy", "buy"])
    html = build.render_rating_moves(moves, prior_exists=True)
    assert html.index("COHR") < html.index("AMAT")


def test_render_upgrade_and_downgrade_classes(tmp_path):
    nan = float("nan")
    up = _moves(tmp_path, ["COHR"], [nan], [nan], ["hold"], ["buy"])
    assert "rm-up" in build.render_rating_moves(up, prior_exists=True)
    down = _moves(tmp_path, ["AMAT"], [nan], [nan], ["strong_buy"], ["hold"])
    assert "rm-down" in build.render_rating_moves(down, prior_exists=True)


def test_render_two_column_layout(tmp_path):
    nan = float("nan")
    moves = _moves(tmp_path, ["AAA", "BBB"], [100.0, nan], [120.0, nan],
                   ["buy", "hold"], ["buy", "buy"])
    html = build.render_rating_moves(moves, prior_exists=True)
    assert "rm-cols" in html
    assert "rm-group--targets" in html and "rm-group--recs" in html


def test_render_upgrade_to_hold_excluded(tmp_path):
    nan = float("nan")
    # GOOD hold->buy is a valid green upgrade; WEAK sell->hold only reaches HOLD
    # and must be excluded (minimum resulting rating = buy). Does not affect cuts.
    moves = _moves(tmp_path, ["WEAK", "GOOD"], [nan, nan], [nan, nan],
                   ["sell", "hold"], ["hold", "buy"])
    html = build.render_rating_moves(moves, prior_exists=True)
    assert "GOOD" in html
    assert "WEAK" not in html


def test_render_caps_each_group_at_ten(tmp_path):
    nan = float("nan")
    n = 15
    tickers = [f"R{i:02d}" for i in range(n)]
    moves = _moves(tmp_path, tickers, [nan] * n, [nan] * n,
                   ["hold"] * n, ["buy"] * n)   # 15 hold->buy upgrades
    html = build.render_rating_moves(moves, prior_exists=True)
    assert html.count('rm-row--rec') == 10       # capped at 10


def test_render_rec_row_shows_current_target_context(tmp_path):
    # Rec change with no target move -> rec group row still shows current target.
    moves = _moves(tmp_path, ["AAA"], [200.0], [200.0], ["hold"], ["buy"])
    html = build.render_rating_moves(moves, prior_exists=True)
    assert "Recommendations" in html
    assert "$200.00" in html            # current target as context


def test_render_big_cut_survives_many_upsides(tmp_path):
    # 14 upside target raises + one big cut. "Upsides first" must not bury the
    # cut — a slot is reserved so the -50% move still appears.
    n = 14
    tickers = [f"UP{i:02d}" for i in range(n)] + ["BIGCUT"]
    prior_t = [100.0] * n + [100.0]
    cur_t = [110.0 + i for i in range(n)] + [50.0]   # raises + a -50% cut
    recs = [""] * (n + 1)
    moves = _moves(tmp_path, tickers, prior_t, cur_t, recs, recs)
    html = build.render_rating_moves(moves, prior_exists=True)
    assert "BIGCUT" in html               # reserved slot keeps the cut visible
    assert "-50.0%" in html


def test_render_empty_states():
    # v2.8: empty-state copy reflects the rolling 2-week baseline model
    assert "Building the 2-week history" in build.render_rating_moves([], prior_exists=False)
    assert "No material moves" in build.render_rating_moves([], prior_exists=True)
