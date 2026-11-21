"""v3.12: fonts are self-hosted -- the page makes no request to Google Fonts
(which would send every visitor's IP address to Google)."""
import re
from pathlib import Path

import build

ROOT = Path(build.__file__).parent
FONTS = ROOT / "assets" / "fonts"
FACES = (FONTS / "fonts.css").read_text(encoding="utf-8")


def test_no_google_fonts_anywhere():
    for f in (ROOT / "build.py", ROOT / "assets" / "dashboard.js", ROOT / "assets" / "dashboard.css"):
        t = f.read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in t and "fonts.gstatic.com" not in t, f.name


def test_every_face_file_exists_and_is_woff2():
    files = re.findall(r'url\("__FONT_BASE__/([^"]+)"\)', FACES)
    assert len(files) >= 6
    for fn in files:
        assert (FONTS / fn).read_bytes()[:4] == b"wOF2", fn


def test_licences_ship_with_the_fonts():
    for slug in ("inter", "geistmono", "geist", "anton", "instrumentserif"):
        txt = (FONTS / f"OFL-{slug}.txt").read_text(encoding="utf-8")
        assert "SIL Open Font License" in txt


def test_font_base_differs_for_page_and_demo():
    assert 'url("fonts/inter' in build._font_face_css(False)
    assert 'url("docs/fonts/inter' in build._font_face_css(True)
    assert "__FONT_BASE__" not in build._font_face_css(False)


def test_publish_fonts_copies_files_and_licences(tmp_path):
    n = build._publish_fonts(tmp_path)
    out = {p.name for p in (tmp_path / "fonts").iterdir()}
    assert "inter-normal-400-700.woff2" in out and "OFL-inter.txt" in out
    assert n == len(out)
    assert build._publish_fonts(tmp_path) == 0          # unchanged files aren't rewritten
