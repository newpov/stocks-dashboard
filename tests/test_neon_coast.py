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


def test_hero_basket_colour_comes_from_css():
    # v3.11: the default line is now blue; the May amber lives on in Classic.
    assert "--hero-basket:" in _block(":root")
    assert "--hero-basket:#f59e0b" in _block("body.palette-classic")
    assert "const basketColor = '#f59e0b';" not in JS
    assert "--hero-basket" in JS


def test_display_font_loaded_only_for_neon():
    # v3.12: Anton is a self-hosted @font-face; browsers fetch it only when text
    # uses it, and only body.palette-neon sets it as a font.
    fonts = (ASSETS / "fonts" / "fonts.css").read_text(encoding="utf-8")
    assert 'font-family:"Anton"' in fonts
    uses = [m.start() for m in __import__("re").finditer(r'"Anton"', CSS)]
    assert uses and all(CSS.rfind("body.palette-neon{", 0, i) > CSS.rfind("}", 0, i) for i in uses)


def test_skin_is_original_not_a_copy():
    # Inspired by a genre look, never a specific game's name or lettering.
    for text in (CSS, JS):
        for banned in ("GTA", "Grand Theft", "Pricedown", "Rockstar", "Vice City"):
            assert banned.lower() not in text.lower(), banned


def test_neon_promo_runs_one_week_and_is_dismissible():
    # A "new skin" card announces the palette for a week after release, then
    # disappears on its own -- judged in the browser, so no rebuild is needed.
    m = re.search(r"const NEON_PROMO_UNTIL = '(\d{4}-\d{2}-\d{2})T23:59:59Z';", JS)
    assert m
    from datetime import date
    assert (date.fromisoformat(m.group(1)) - date(2026, 11, 20)).days == 7
    assert "neonPromoDismissed" in JS            # x / Try it remember the choice
    assert "window.applyPalette" in JS           # Try it switches the palette
    assert ".neon-promo" in CSS


def test_neon_promo_text_is_set_safely():
    block = JS[JS.index("function setupNeonPromo"):]
    block = block[:block.index("})();")]
    assert "innerHTML" not in block
