"""v2.5 #4: Rating moves ordering.

Target-price changes lead (sorted by magnitude, unchanged). The recommendation
rows below are no longer alphabetical -- they're ordered by the *resulting*
rating strength: strong buy -> buy -> hold -> sell -> (coverage dropped) last.
Re-entry ideas is untouched (still upside %).
"""
import pandas as pd

import build


def _cache(tmp_path, name, target, recs):
    df = pd.DataFrame({"target_mean": target, "recommendation": recs},
                      index=["AAA", "BBB", "CCC", "DDD", "EEE"])
    p = tmp_path / name
    df.to_parquet(p)
    return p, df


def test_rating_moves_recs_sorted_by_new_strength(tmp_path):
    nan = float("nan")
    prior_path, _ = _cache(
        tmp_path, "prior.parquet",
        target=[100.0, nan, nan, nan, nan],
        recs=["buy", "none", "none", "none", "buy"])
    _, current = _cache(
        tmp_path, "current.parquet",
        target=[120.0, nan, nan, nan, nan],          # AAA: +20% target move
        recs=["buy", "hold", "strong_buy", "buy", "none"])
    # AAA rec unchanged (buy->buy); BBB none->hold; CCC none->strong_buy;
    # DDD none->buy; EEE buy->none (coverage dropped)
    moves = build.compute_rating_moves(prior_path, current)
    assert moves[0]["ticker"] == "AAA" and moves[0]["kind"] == "target"
    recs = [m["ticker"] for m in moves if m["kind"] == "recommendation"]
    # by new rating: strong_buy(CCC) -> buy(DDD) -> hold(BBB) -> none(EEE),
    # NOT alphabetical (BBB, CCC, DDD, EEE)
    assert recs == ["CCC", "DDD", "BBB", "EEE"]


def test_rating_moves_targets_lead_and_keep_magnitude_order(tmp_path):
    nan = float("nan")
    prior_path, _ = _cache(
        tmp_path, "prior.parquet",
        target=[100.0, 100.0, nan, nan, nan],
        recs=["", "", "none", "", ""])
    _, current = _cache(
        tmp_path, "current.parquet",
        target=[90.0, 130.0, nan, nan, nan],   # AAA -10%, BBB +30%
        recs=["", "", "strong_buy", "", ""])
    moves = build.compute_rating_moves(prior_path, current)
    targets = [m["ticker"] for m in moves if m["kind"] == "target"]
    assert targets == ["BBB", "AAA"]           # +30% before -10% (magnitude desc)
    assert moves[-1]["kind"] == "recommendation"   # rec row after the targets
