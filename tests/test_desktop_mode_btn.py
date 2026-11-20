"""v3.8.1: the topbar Desktop-view toggle is an icon button (v2.1), but its JS
still wrote `btn.textContent = 'Desktop view'` on load -- wiping the SVG and
cramming the words into a 36px box (clipped on every phone). Source guards."""
import re
from pathlib import Path

import build

ROOT = Path(build.__file__).parent
JS = (ROOT / "assets" / "dashboard.js").read_text(encoding="utf-8")
CSS = (ROOT / "assets" / "dashboard.css").read_text(encoding="utf-8")
SRC = Path(build.__file__).read_text(encoding="utf-8")


def _setup_block():
    i = JS.index("function setupDesktopMode")
    return JS[i:JS.index("})();", i)]


def test_toggle_never_replaces_the_icon_with_text():
    blk = _setup_block()
    assert not re.search(r"\.(textContent|innerHTML|innerText)\s*=", blk)
    assert "dataset.tooltip" in blk and "aria-label" in blk


def test_button_carries_both_icons():
    i = SRC.index('id="desktop-mode-btn"')
    btn = SRC[i:SRC.index("</button>", i)]
    assert 'class="ic-desktop"' in btn and 'class="ic-mobile"' in btn


def test_button_centres_like_other_icon_buttons():
    assert re.search(r"\.desktop-mode-btn\{display:inline-flex\}", CSS)
    assert "display:inline-block !important" not in CSS
    assert ".desktop-mode-btn.active .ic-desktop{display:none}" in CSS


def test_forced_topbar_clears_the_demo_banner():
    blk = _setup_block()
    assert "demo-banner" in blk and "placeTopbar" in blk


def test_forced_topbar_anchors_left_where_the_screen_starts():
    assert "body.force-desktop .topbar{position:fixed;top:18px;left:12px;right:auto}" in CSS
