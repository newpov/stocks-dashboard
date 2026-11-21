"""v3.11 default theme: readable neutral greys + blue, Inter / JetBrains Mono;
the May look kept as 'Classic'; chart colours follow the palette."""
import re
from pathlib import Path

import build

ROOT = Path(build.__file__).parent
CSS = (ROOT / "assets" / "dashboard.css").read_text(encoding="utf-8")
JS = (ROOT / "assets" / "dashboard.js").read_text(encoding="utf-8")
SRC = Path(build.__file__).read_text(encoding="utf-8")


def _block(selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert m, selector
    return m.group(1)


def _tok(block, name):
    return re.search(re.escape(name) + r":\s*([^;]+);", block).group(1).strip()


def _contrast(a, b):
    def lum(h):
        h = h.lstrip("#")
        rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        r, g, bb = (f(x) for x in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * bb
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_default_text_meets_small_text_contrast():
    # The May default's labels were 3.5:1; WCAG AA small text needs 4.5:1.
    root = _block(":root")
    for name in ("--text", "--text-2", "--text-dim"):
        assert _contrast(_tok(root, name), _tok(root, "--surface")) >= 4.5, name


def test_default_fonts_are_inter_and_geist_mono():
    root = _block(":root")
    assert '"Inter"' in _tok(root, "--font-ui") and '"Inter"' in _tok(root, "--font-display")
    # Geist Mono for figures: JetBrains Mono's flat-sided rounds read "squared"
    assert '"Geist Mono"' in _tok(root, "--font-mono")
    assert "family=Inter" in SRC and "family=Geist+Mono" in SRC
    assert "Instrument+Serif" not in SRC          # Classic's fonts load on demand only


def test_may_look_survives_as_classic():
    cl = _block("body.palette-classic")
    assert "--ink:#0b0e17" in cl and "--accent:#f59e0b" in cl and "Instrument Serif" in cl
    assert "Instrument+Serif" in JS                # fetched when Classic is chosen


def test_palette_order_new_default_then_neon_then_classic_last():
    order = re.search(r"const ORDER = \[([^\]]*)\]", JS).group(1)
    names = re.findall(r"'(\w+)'", order)
    assert names == ["default", "neon", "softdark", "light", "bloomberg", "classic"]
    assert "'palette-classic'" in JS


def test_hero_chart_colours_come_from_the_palette():
    i = JS.index("function renderHeroChart() {")
    body = JS[i:JS.index("\n}\n", i)]
    assert "const TH = chartTheme();" in body
    for literal in ("#6b7185", "#34d399", "#f87171", "Geist Mono"):
        assert literal not in body, literal


def test_heading_weight_is_a_token():
    assert "--display-weight:600" in _block(":root")
    assert "--display-weight:400" in _block("body.palette-classic")
    assert "--display-weight:400" in _block("body.palette-neon")   # Anton: one weight
