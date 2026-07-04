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
