"""v3.8.2 security-review fixes. No network, no build."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

import build

ROOT = Path(build.__file__).parent
JS = (ROOT / "assets" / "dashboard.js").read_text(encoding="utf-8")


def _js_fn(name):
    i = JS.index(f"function {name}(")
    return JS[i:JS.index("\n}\n", i) + 3]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_browser_escaper_is_attribute_safe():
    # escapeNewsHtml used textContent->innerHTML, which leaves quotes alone; it
    # feeds href="..." / data-source="..." for live worker news, so a feed link
    # containing a quote could break out of the attribute and add a handler.
    js = _js_fn("escapeNewsHtml") + _js_fn("safeUrl") + """
const evil = 'https://x.test/a" onmouseover="alert(1)';
console.log(JSON.stringify([escapeNewsHtml(`<b>"'&`), safeUrl(evil), safeUrl('javascript:alert(1)')]));
"""
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    esc, url, js_url = json.loads(out)
    assert esc == "&lt;b&gt;&quot;&#39;&amp;"
    assert '"' not in url and "&quot;" in url
    assert js_url == "#"


def test_browser_escaper_matches_python_esc():
    assert build._esc("<b>\"'&") == "&lt;b&gt;&quot;&#39;&amp;"


def test_inline_json_neutralises_comment_open_and_script_close():
    # "<!--<script" inside a payload string puts the HTML parser in the
    # "script data double escaped" state, where the page's own </script> no
    # longer closes the element; "</" could close it early. Neither may survive.
    out = build._json_for_script({"t": "<!--<script></script>"})
    assert "<!--" not in out and "</" not in out
    assert json.loads(out.replace(r"<\!--", "<!--")) == {"t": "<!--<script></script>"}


def test_worker_cache_key_ignores_query_string():
    # /news?x=1, /news?x=2 ... each missed the edge cache and triggered five
    # upstream feed fetches -- a free cache-bypass / amplification lever.
    w = (ROOT / "worker" / "index.js").read_text(encoding="utf-8")
    assert "new Request(url.toString()" not in w
    assert re.search(r"new URL\(\s*[\"']/news[\"']\s*,\s*url\.origin\s*\)", w)


def test_ci_runs_do_not_race_each_other():
    wf = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    assert re.search(r"^concurrency:\s*\n\s+group:", wf, re.M)
    assert "cancel-in-progress: false" in wf


def test_source_health_flags_each_silent_source():
    # Kalshi dropped a field and its whole feed went silent for weeks with no
    # signal. Each configured source returning nothing must raise a warning.
    themes = [{"theme": "Fed", "source": "kalshi"}, {"theme": "War", "source": "polymarket"}]
    preds = [{"theme": "War", "source": "polymarket"}]
    w = build.source_health_warnings(news_items=[], pred_current=preds,
                                     pred_themes=themes, ai_fresh=pd.DataFrame())
    text = " | ".join(w)
    assert "kalshi" in text.lower() and "polymarket" not in text.lower()
    assert "news" in text.lower() and "ai capex" in text.lower()
    assert all(m.isascii() for m in w)          # printed in CI: ASCII only


def test_source_health_quiet_when_everything_arrives():
    themes = [{"theme": "Fed", "source": "kalshi"}]
    w = build.source_health_warnings(
        news_items=[{"title": "x"}], pred_current=[{"theme": "Fed", "source": "kalshi"}],
        pred_themes=themes, ai_fresh=pd.DataFrame({"ticker": ["MSFT"]}))
    assert w == []


def test_main_prints_source_health_as_ci_annotations():
    src = Path(build.__file__).read_text(encoding="utf-8")
    assert "::warning title=Source health::" in src


# --- v3.8.4: CI supply-chain hardening ---------------------------------------
WF = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")


def test_ci_token_is_not_left_in_git_config_for_the_build():
    # actions/checkout persists the write token in .git/config by default, so
    # every step -- including `python build.py`, which imports third-party
    # packages -- could read it. Only the final push step may see it.
    assert "persist-credentials: false" in WF
    assert WF.count("secrets.GITHUB_TOKEN") == 1
    push_step = WF[WF.index("Commit & push"):]
    assert "secrets.GITHUB_TOKEN" in push_step


def test_ci_actions_pinned_to_commit_shas():
    uses = re.findall(r"uses:\s*([^\s#]+)", WF)
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u) for u in uses), uses


def test_ci_installs_the_hashed_lock():
    assert "pip install --require-hashes -r requirements-ci.txt" in WF
    lock = (ROOT / "requirements-ci.txt").read_text(encoding="utf-8")
    pkgs = re.findall(r"^([A-Za-z0-9_.-]+)==\S+ \\$", lock, re.M)
    assert len(pkgs) >= 20
    # every pinned package carries at least one hash
    blocks = re.split(r"^(?=[A-Za-z0-9_.-]+==)", lock, flags=re.M)[1:]
    assert all("--hash=sha256:" in b for b in blocks)


def test_ci_lock_matches_its_direct_pins():
    pins = dict(re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)$",
                           (ROOT / "requirements-ci.in").read_text(encoding="utf-8"), re.M))
    lock = (ROOT / "requirements-ci.txt").read_text(encoding="utf-8")
    for name, ver in pins.items():
        assert re.search(rf"^{re.escape(name)}=={re.escape(ver)} ", lock, re.M | re.I), name


def test_ci_reruns_when_the_lock_changes():
    assert "'requirements-ci.txt'" in WF


def test_ci_python_matches_the_lock():
    # v3.10.1: CI moved 3.11 -> 3.14 (the author's local version). The lock
    # must be generated for the same interpreter the workflow installs.
    m = re.search(r"python-version:\s*'([\d.]+)'", WF)
    assert m and m.group(1) == "3.14"
    lock = (ROOT / "requirements-ci.txt").read_text(encoding="utf-8")
    assert f"--python-version {m.group(1)} " in lock
