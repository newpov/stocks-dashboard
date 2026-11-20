"""v3.8 "Neon Coast" skin: a 5th stop on the palette cycle (80s neon-coast
sunset look). Source-level guards -- no network, no build."""
import re
from pathlib import Path

import build

ASSETS = Path(build.__file__).parent / "assets"
CSS = (ASSETS / "dashboard.css").read_text(encoding="utf-8")
JS = (ASSETS / "dashboard.js").read_text(encoding="utf-8")


def _block(selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert m, selector
    return m.group(1)


def test_neon_palette_defines_every_token():
    body = _block("body.palette-neon")
    for tok in ("--ink", "--ink-soft", "--surface", "--surface-2", "--border",
                "--text", "--text-2", "--text-dim", "--up", "--down", "--accent",
                "--hero-basket", "--font-display"):
        assert tok + ":" in body, tok


def test_neon_keeps_gain_loss_distinct_from_accent():
    body = _block("body.palette-neon")
    val = lambda t: re.search(re.escape(t) + r":\s*([^;]+);", body).group(1).strip().lower()
    assert len({val("--up"), val("--down"), val("--accent")}) == 3


def test_palette_cycle_includes_neon():
    assert "'neon'" in re.search(r"const ORDER = \[([^\]]*)\]", JS).group(1)
    assert "neon: 'Neon Coast'" in JS
    assert "'palette-neon'" in JS          # removed again when cycling away


def test_hero_basket_colour_comes_from_css_with_amber_default():
    assert "--hero-basket:#f59e0b" in _block(":root")   # existing palettes unchanged
    assert "const basketColor = '#f59e0b';" not in JS
    assert "--hero-basket" in JS


def test_display_font_loaded_only_for_neon():
    # Anton is fetched on demand when the skin is applied, not for every visitor.
    assert "family=Anton" in JS
    assert "Anton" not in build.Path(build.__file__).read_text(encoding="utf-8")


def test_skin_is_original_not_a_copy():
    # Inspired by a genre look, never a specific game's name or lettering.
    for text in (CSS, JS):
        for banned in ("GTA", "Grand Theft", "Pricedown", "Rockstar", "Vice City"):
            assert banned.lower() not in text.lower(), banned
