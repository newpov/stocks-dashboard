"""v3.9 honesty fixes: labels must not contradict the numbers beside them."""
import re
from pathlib import Path

import pandas as pd

import build

ROOT = Path(build.__file__).parent
JS = (ROOT / "assets" / "dashboard.js").read_text(encoding="utf-8")


def test_kalshi_theme_labels_carry_no_fixed_threshold():
    # Since v3.7.1 the Kalshi picker shows the strike nearest 50%, so a label
    # like "Bitcoin above $110k" sat over a "$99,999.99" question. The label
    # names the topic; the question text carries the level.
    th = pd.read_csv(ROOT / "predictions.csv")
    k = th[th["source"] == "kalshi"]["theme_label"]
    bad = [t for t in k if re.search(r"\d+(\.\d+)?\s*%|\$\s*\d", str(t))]
    assert bad == [], bad


def test_preset_recovery_derives_from_the_model_not_hand_set_strings():
    # Hand-set strings drifted from the model ("2008-style crash" said ~4 yrs
    # while recovery_estimate(-45) says ~5-6 years). Each preset's recovery is
    # now the model's estimate for that preset's MARKET move.
    for p in build.SHOCKWAVE_PRESETS:
        assert p["recovery"] == build.recovery_estimate(p["spy"]), p["label"]


def test_hero_recovery_always_reflects_the_basket_move():
    # The headline used to show the preset's market recovery next to the
    # basket's (often much deeper) projected move. It must use recov(basket).
    assert "active&&active.recovery?active.recovery:recov(basket)" not in JS
    assert "var rc=recov(basket);" in JS
    assert "'mkt '+p.recovery" in JS          # chip says whose recovery it is
