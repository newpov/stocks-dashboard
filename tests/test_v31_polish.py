"""v3.1 polish:
  #1 per-module "as of <date>" labels (industry outlook, re-entry, signal map);
  #4 signal-map dots encode |return| magnitude via opacity (fixed size kept).
(FCF yield -> see test_value_screen.py; quiz monthly reset already shipped.)
"""
import re

import build


_AS_OF = "01 Jul 2026"


# --- #1: "as of <date>" labels ------------------------------------------------

def test_signal_strip_shows_as_of():
    data = [{"ticker": "AMD", "signal": 70.0, "ret": 22.0},
            {"ticker": "SAP", "signal": -55.0, "ret": -7.0}]
    html = build.render_signal_strip(data, as_of=_AS_OF)
    assert f"as of {_AS_OF}" in html


def test_industry_outlook_shows_as_of():
    groups = [{"industry": "Tech", "avg_ret_12m": 5.0, "n_holdings": 1,
               "top_stocks": [{"ticker": "AAA", "upside": 10.0, "rec": "buy",
                               "ret_12m": 5.0, "cap_tier": "L"}]}]
    html = build.render_industry_outlook(groups, universe_size=1, as_of=_AS_OF)
    assert f"as of {_AS_OF}" in html


def test_reentry_shows_as_of():
    # Header renders even with no ranked rows (empty pool message), so the
    # as-of stamp must still appear.
    html = build.render_analyst_signals([], candidate_pool_size=5, as_of=_AS_OF)
    assert f"as of {_AS_OF}" in html


def test_as_of_optional_default_blank():
    # No as_of passed -> no stray "as of" text (back-compat with old callers).
    data = [{"ticker": "AMD", "signal": 70.0, "ret": 22.0},
            {"ticker": "SAP", "signal": -55.0, "ret": -7.0}]
    assert "as of" not in build.render_signal_strip(data)


# --- #4: signal-map magnitude via opacity ------------------------------------

def test_signal_strip_opacity_scales_with_return_magnitude():
    data = [{"ticker": "BIG", "signal": 10.0, "ret": -50.0},
            {"ticker": "SMALL", "signal": -10.0, "ret": -1.0}]
    html = build.render_signal_strip(data)
    ops = [float(m) for m in re.findall(r'opacity:([\d.]+)', html)]
    assert len(ops) == 2                       # one per dot
    assert max(ops) > min(ops)                 # the -50% dot is bolder than the -1%
    assert 0.0 <= min(ops) and max(ops) <= 1.0


def test_signal_strip_radius_still_uniform():
    # Magnitude is encoded by opacity, NOT size -> every dot keeps the same r.
    data = [{"ticker": "BIG", "signal": 10.0, "ret": -50.0},
            {"ticker": "SMALL", "signal": -10.0, "ret": -1.0}]
    html = build.render_signal_strip(data)
    radii = set(re.findall(r'<circle[^>]*\br="([\d.]+)"', html))
    assert len(radii) == 1                     # single fixed radius
