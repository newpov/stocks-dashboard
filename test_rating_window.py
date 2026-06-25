"""Rolling 2-week baseline for rating moves (v2.8).

Under daily CI the old mtime-based "~weekly baseline" breaks (git checkout resets
file mtime every run), so rating moves would diff against an unstable reference.
These tests cover the replacement: a committed rolling history of analyst
snapshots + a sliding ~14-day baseline selection that survives CI.
"""
import pandas as pd

import build


def _analyst(d):
    """ticker-indexed analyst frame from {ticker: (target_mean, rec)}."""
    return pd.DataFrame(
        [(t, tm, rc) for t, (tm, rc) in d.items()],
        columns=["ticker", "target_mean", "recommendation"]).set_index("ticker")


def test_append_creates_and_is_idempotent_same_day(tmp_path):
    hp = tmp_path / "hist.parquet"
    today = pd.Timestamp("2026-06-25")
    h1 = build.append_analyst_history(
        _analyst({"AAA": (100.0, "buy"), "BBB": (50.0, "hold")}), today, hp)
    assert set(h1["ticker"]) == {"AAA", "BBB"}
    assert (pd.to_datetime(h1["snapshot_date"]).dt.normalize() == today).all()
    # same-day re-run with a changed value replaces, never duplicates
    h2 = build.append_analyst_history(
        _analyst({"AAA": (110.0, "buy"), "BBB": (50.0, "hold")}), today, hp)
    assert len(h2) == 2
    assert float(h2.set_index("ticker").loc["AAA", "target_mean"]) == 110.0


def test_append_prunes_old(tmp_path):
    hp = tmp_path / "hist.parquet"
    old = pd.Timestamp("2026-06-01")
    build.append_analyst_history(_analyst({"AAA": (100.0, "buy")}), old, hp, keep_days=18)
    today = pd.Timestamp("2026-06-25")   # 24 days later, beyond keep_days
    h = build.append_analyst_history(_analyst({"AAA": (120.0, "buy")}), today, hp, keep_days=18)
    dates = set(pd.to_datetime(h["snapshot_date"]).dt.normalize())
    assert old not in dates
    assert today in dates


def test_select_baseline_ramp_up_uses_oldest(tmp_path):
    hp = tmp_path / "hist.parquet"
    d0, d1 = pd.Timestamp("2026-06-20"), pd.Timestamp("2026-06-25")
    build.append_analyst_history(_analyst({"AAA": (100.0, "buy")}), d0, hp)
    h = build.append_analyst_history(_analyst({"AAA": (130.0, "buy")}), d1, hp)
    bdate, base = build.select_rating_baseline(h, d1, window_days=14)
    assert pd.Timestamp(bdate) == d0   # only 5 days of history -> oldest available
    assert float(base.loc["AAA", "target_mean"]) == 100.0


def test_select_baseline_picks_14d_ago(tmp_path):
    hp = tmp_path / "hist.parquet"
    base_day = pd.Timestamp("2026-06-01")
    for i, px in [(0, 100.0), (14, 130.0), (20, 140.0)]:
        build.append_analyst_history(
            _analyst({"AAA": (px, "buy")}), base_day + pd.Timedelta(days=i), hp, keep_days=60)
    today = base_day + pd.Timedelta(days=20)
    h = pd.read_parquet(hp)
    bdate, base = build.select_rating_baseline(h, today, window_days=14)
    # cutoff = today-14 = day6; most recent snapshot <= day6 is day0
    assert pd.Timestamp(bdate) == base_day
    assert float(base.loc["AAA", "target_mean"]) == 100.0


def test_select_baseline_none_when_only_today():
    h = pd.DataFrame({"snapshot_date": [pd.Timestamp("2026-06-25")], "ticker": ["AAA"],
                      "target_mean": [100.0], "recommendation": ["buy"]})
    bdate, base = build.select_rating_baseline(h, pd.Timestamp("2026-06-25"))
    assert bdate is None
    assert base.empty


def test_compute_rating_moves_with_prior_df():
    prior = _analyst({"AAA": (100.0, "hold"), "BBB": (50.0, "buy")})
    current = _analyst({"AAA": (120.0, "buy"), "BBB": (50.0, "buy")})
    moves = build.compute_rating_moves(None, current, prior_df=prior)
    kinds = {(m["ticker"], m["kind"]) for m in moves}
    assert ("AAA", "target") in kinds          # +20% target move
    assert ("AAA", "recommendation") in kinds   # hold -> buy
    assert ("BBB", "target") not in kinds       # unchanged


def test_compute_rating_moves_empty_prior_df_is_safe():
    current = _analyst({"AAA": (120.0, "buy")})
    assert build.compute_rating_moves(None, current, prior_df=pd.DataFrame()) == []


def test_render_rating_moves_shows_baseline_date():
    moves = [{"ticker": "AAA", "kind": "target", "before": 100.0, "after": 120.0,
              "pct_change": 20.0, "abs_pct": 20.0, "cur_rec": "buy", "cur_target": 120.0}]
    html = build.render_rating_moves(moves, True, baseline_date=pd.Timestamp("2026-06-11"))
    assert "since 11 Jun" in html


def test_render_rating_moves_building_history_message():
    html = build.render_rating_moves([], False, baseline_date=None)
    assert "Building the 2-week history" in html


def test_seed_history_from_prior(tmp_path):
    hp = tmp_path / "hist.parquet"
    pp = tmp_path / "prior.parquet"
    _analyst({"AAA": (90.0, "hold")}).to_parquet(pp)
    today = pd.Timestamp("2026-06-25")
    assert build.seed_rating_history(pp, today, hp, window_days=14) is True
    h = pd.read_parquet(hp)
    assert (pd.to_datetime(h["snapshot_date"]).dt.normalize()
            == today - pd.Timedelta(days=14)).all()
    # then today's append gives a usable 14-day baseline immediately
    h2 = build.append_analyst_history(_analyst({"AAA": (100.0, "buy")}), today, hp)
    bdate, base = build.select_rating_baseline(h2, today, window_days=14)
    assert pd.Timestamp(bdate) == today - pd.Timedelta(days=14)
    assert float(base.loc["AAA", "target_mean"]) == 90.0
    # no-op when history already exists
    assert build.seed_rating_history(pp, today, hp) is False
