"""v2.9.7-lite (v3.5): embedded CSS/JS extracted to static assets/ files,
inlined at build time. Source-level guards — no network, no build."""
import inspect
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import build

ASSETS = Path(build.__file__).parent / "assets"
BUILD_SRC = Path(build.__file__).read_text(encoding="utf-8")

# Every name that was an f-string interpolation inside the extracted blocks.
# None may survive as a literal '{name}' in the static files ('${name}' JS
# template-literal usage is fine — hence the (?<!\$) guard).
INTERP_NAMES = [
    "data_json", "heavy_url_js", "portfolio_json", "aux_json",
    "pocket_lessons_json", "shockwave_json", "rm_analyst_json",
    "quiz_pool_json", "last_look_json", "news_worker_url_js",
    "BASE_SYMBOL", "BASE_CCY", "WATCH_COLS_DESKTOP", "WATCH_COLS_MOBILE",
    "MODAL_VB_W", "MODAL_VB_H", "MODAL_VB_PAD_L", "MODAL_VB_PAD_T",
    "MODAL_VB_PAD_R", "MODAL_VB_PAD_B",
    "SHOCKWAVE_LABEL_TOP", "SHOCKWAVE_PULSE_PCT",
]


def _no_remnants(text):
    for name in INTERP_NAMES:
        assert re.search(r"(?<!\$)\{" + name + r"\}", text) is None, name


class TestReadAsset:
    def test_read_asset_exists_and_mandates_utf8(self):
        src = inspect.getsource(build._read_asset)
        assert 'encoding="utf-8"' in src  # cp1252 lesson: never locale-default

    def test_asset_dir_is_repo_relative(self):
        assert build._ASSET_DIR == Path(build.__file__).parent / "assets"


class TestCssAsset:
    def test_css_file_exists_with_known_rule(self):
        css = (ASSETS / "dashboard.css").read_text(encoding="utf-8")
        assert "--ink:#0b0e17" in css              # sentinel: the :root palette
        assert "var(--watch-cols-desktop)" in css  # param wiring (spec 4.3)
        assert "var(--watch-cols-mobile)" in css

    def test_css_has_no_fstring_remnants(self):
        css = (ASSETS / "dashboard.css").read_text(encoding="utf-8")
        _no_remnants(css)
        assert "{{" not in css  # un-doubling left no doubled braces

    def test_css_moved_out_of_build_py(self):
        # the palette must live ONLY in the asset — no duplicated copy
        assert "--ink:#0b0e17" not in BUILD_SRC
        assert "{css_block}" in BUILD_SRC


class TestJsAsset:
    def test_js_file_exists_with_known_code(self):
        js = (ASSETS / "dashboard.js").read_text(encoding="utf-8")
        assert "function fmtMoney(v, sym)" in js       # first body function
        assert "refreshNewsFromWorker();" in js         # last body statement
        assert "sym = sym || BASE_SYMBOL;" in js        # Group C rewrite survived

    def test_js_has_no_fstring_remnants(self):
        _no_remnants((ASSETS / "dashboard.js").read_text(encoding="utf-8"))

    def test_js_moved_out_of_build_py(self):
        assert "function fmtMoney" not in BUILD_SRC
        assert "{js_body}" in BUILD_SRC
        # generated prelude stays in build.py
        assert "const DATA = {data_json};" in BUILD_SRC

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    def test_js_asset_is_valid_javascript(self):
        r = subprocess.run(
            ["node", "--check", str(ASSETS / "dashboard.js")],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


class TestExtractionIntegrity:
    def test_both_assets_utf8_decodable(self):
        # explicit strict decode — a cp1252-mangled edit would raise here
        for name in ("dashboard.css", "dashboard.js"):
            (ASSETS / name).read_text(encoding="utf-8")

    def test_build_py_fstring_shrunk(self):
        # the whole point: build.py no longer carries the big blocks
        assert len(BUILD_SRC.splitlines()) < 10_000
