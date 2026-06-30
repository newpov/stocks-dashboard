"""v2.9.6 label-honesty fixes: the metrics flagged in the methodology review as
misleading should now read honestly. Covers the deferred trio (analyst dispersion,
rating cold-start, predictions as-of) plus regression pins for the pure-copy relabels."""
import json
from datetime import datetime, timezone

import pandas as pd

import build


# --- M-UPSIDE: analyst target dispersion is surfaced, not hidden behind the mean ---

def test_analyst_payload_includes_dispersion():
    analyst = pd.DataFrame({
        "target_mean": [120.0], "target_high": [150.0], "target_low": [90.0],
        "num_analysts": [10], "recommendation": ["buy"],
    }, index=["AAA"])
    prices = pd.DataFrame({"AAA": [100.0, 100.0]})
    meta = pd.DataFrame({"name": ["A Co"], "industry": ["Tech"], "currency": ["USD"]},
                        index=["AAA"])
    rows = build.build_analyst_payload(["AAA"], analyst, prices, meta)
    assert rows
    r = rows[0]
    assert r["target_high"] == 150.0 and r["target_low"] == 90.0
    assert abs(r["spread_pct"] - 60.0) < 1e-6     # (150-90)/100 * 100


def test_analyst_card_shows_wide_range_flag():
    rows = [{
        "ticker": "AAA", "name": "A Co", "industry": "Tech", "currency": "USD",
        "ccy_symbol": "$", "current": 100.0, "target_mean": 120.0,
        "target_high": 150.0, "target_low": 90.0, "spread_pct": 60.0,
        "upside_pct": 20.0, "num_analysts": 10, "recommendation": "buy",
        "signal": "—", "signal_tone": "neutral", "rsi14": None,
    }]
    html = build.render_analyst_signals(rows, 1)
    assert "an-range" in html and "an-range-wide" in html   # 60% spread -> wide


def test_analyst_card_no_range_when_missing():
    rows = [{
        "ticker": "AAA", "name": "A Co", "industry": "Tech", "currency": "USD",
        "ccy_symbol": "$", "current": 100.0, "target_mean": 120.0,
        "target_high": None, "target_low": None, "spread_pct": None,
        "upside_pct": 20.0, "num_analysts": 10, "recommendation": "buy",
        "signal": "—", "signal_tone": "neutral", "rsi14": None,
    }]
    html = build.render_analyst_signals(rows, 1)
    assert "an-range" not in html


# --- M-RATE: cold-start seeded baseline is flagged provisional, not a real date ---

def test_rating_baseline_is_seeded(tmp_path):
    meta = tmp_path / "rating_seed.json"
    meta.write_text(json.dumps({"seed_date": "2026-06-12"}))
    assert build.rating_baseline_is_seeded(pd.Timestamp("2026-06-12"), meta) is True
    assert build.rating_baseline_is_seeded(pd.Timestamp("2026-06-13"), meta) is False
    assert build.rating_baseline_is_seeded(None, meta) is False
    assert build.rating_baseline_is_seeded(pd.Timestamp("2026-06-12"),
                                           tmp_path / "absent.json") is False


def test_render_rating_moves_seeded_label():
    seeded = build.render_rating_moves([], True, baseline_date=pd.Timestamp("2026-06-12"),
                                       baseline_seeded=True)
    assert "seeded baseline" in seeded and "since 12 Jun" not in seeded
    real = build.render_rating_moves([], True, baseline_date=pd.Timestamp("2026-06-12"),
                                     baseline_seeded=False)
    assert "since 12 Jun" in real and "seeded baseline" not in real


# --- M-PRED: "as of" reflects the prediction fetch date carried in the data ---

def test_prediction_moves_preserve_fetched_at():
    cur = [{"theme": "Fed", "probability": 60.0, "fetched_at": "2026-06-25"}]
    rows = build.compute_prediction_moves([], cur)
    assert rows[0]["fetched_at"] == "2026-06-25"


def test_market_expectations_renders_asof():
    rows = [{"theme": "Fed", "probability": 60.0, "question": "q",
             "source": "kalshi", "url": "", "delta_pp": None, "fetched_at": "2026-06-25"}]
    html = build.render_market_expectations(rows, "2026-06-25")
    assert "as of 2026-06-25" in html


# --- regression pins for the pure-copy relabels ---

def test_currency_note_drops_unsupported_capital_claim():
    rows = [{"ccy": "USD", "share": 0.9, "n": 9, "color": "#111"},
            {"ccy": "GBP", "share": 0.1, "n": 1, "color": "#222"}]
    html = build.render_currency_exposure(rows)
    assert "materially drags" not in html and "FX moves" in html


def test_diversification_flags_small_sample():
    data = {"avg_corr": 0.12, "n_positions": 3, "n_pairs": 3, "lookback_days": 126,
            "most_correlated": [{"a": "A", "b": "B", "corr": 0.1}],
            "best_diversifiers": [{"ticker": "A", "avg_corr": 0.1}],
            "histogram": [{"min": -1, "max": -0.75, "count": 0}], "all_pairs": []}
    meta = pd.DataFrame({"name": ["A", "B", "C"], "industry": ["x", "y", "z"],
                         "currency": ["USD"] * 3}, index=["A", "B", "C"])
    html = build.render_basket_diversification(data, meta)
    assert "only 3 names" in html


# --- M-CLOSE: "last close" must not label an unsettled same-day intraday bar ---
# A mixed London+US basket gets a row stamped for *today* as soon as London opens
# (07:00 UTC) while the US session for that day is still pending (closes ~20:00 UTC).
# Labelling that provisional row "last close" is dishonest, so a same-(UTC)-day row
# is only accepted once US markets have settled.

def test_last_settled_skips_unsettled_today():
    # Fri settled + today's (Mon) provisional row; build runs midday pre-US-close.
    idx = pd.to_datetime(["2026-06-26", "2026-06-29"])
    now = datetime(2026, 6, 29, 12, 32, tzinfo=timezone.utc)
    assert build.last_settled_close_date(idx, now=now) == pd.Timestamp("2026-06-26")


def test_last_settled_accepts_today_after_close():
    idx = pd.to_datetime(["2026-06-26", "2026-06-29"])
    now = datetime(2026, 6, 29, 22, 0, tzinfo=timezone.utc)   # after US close
    assert build.last_settled_close_date(idx, now=now) == pd.Timestamp("2026-06-29")


def test_last_settled_prior_day_row_always_settled():
    # Last row is strictly before today -> settled regardless of the hour.
    idx = pd.to_datetime(["2026-06-25", "2026-06-26"])
    now = datetime(2026, 6, 29, 8, 0, tzinfo=timezone.utc)
    assert build.last_settled_close_date(idx, now=now) == pd.Timestamp("2026-06-26")


def test_last_settled_empty_index_is_none():
    assert build.last_settled_close_date(pd.to_datetime([])) is None
