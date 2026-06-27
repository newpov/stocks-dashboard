"""v2.9.3 escaping/XSS hardening. Adversarial-input tests so a regression in the
escaping discipline fails CI (the render layer previously had no hostile-input test)."""
import json

import build


# --- _esc: now quote-safe (attribute context) as well as text ---

def test_esc_escapes_angles_quotes_amp():
    assert build._esc('a<b>"c\'&') == 'a&lt;b&gt;&quot;c&#39;&amp;'
    assert build._esc(None) == ""


def test_esc_normal_text_unchanged():
    assert build._esc("Apple Inc") == "Apple Inc"
    assert build._esc("AT&T") == "AT&amp;T"


# --- safe_url: http(s) only, everything else collapses to '#' ---

def test_safe_url_allows_http_s():
    assert build.safe_url("https://x.com/a?b=1") == "https://x.com/a?b=1"
    assert build.safe_url("http://x.com") == "http://x.com"
    assert build.safe_url("  HTTPS://X.com  ") == "HTTPS://X.com"   # trimmed, case kept


def test_safe_url_blocks_dangerous_schemes():
    for bad in ("javascript:alert(1)", "data:text/html,<script>", "vbscript:x",
                "JaVaScRiPt:x", "", "/relative", "ftp://x", None):
        assert build.safe_url(bad) == "#"


def test_safe_url_escapes_quotes_in_url():
    # An http URL carrying a quote must not break out of the href attribute.
    assert '"' not in build.safe_url('https://x.com/"onmouseover=1')


# --- _json_for_script: can't terminate the <script> element early ---

def test_json_for_script_hardens_script_close():
    out = build._json_for_script({"name": "</script><img src=x onerror=alert(1)>"})
    assert "</script>" not in out          # the killer sequence is gone
    assert "<\\/script>" in out            # escaped form present
    assert json.loads(out) == {"name": "</script><img src=x onerror=alert(1)>"}  # \/ is valid JSON


def test_json_for_script_roundtrips_normal():
    obj = {"x": [1, 2], "y": "ok"}
    assert json.loads(build._json_for_script(obj, separators=(",", ":"))) == obj


# --- render_news: malicious link + title can't inject ---

def _news_item(link):
    return {"source": "Evil RSS", "link": link, "title": "<script>alert(1)</script>",
            "published_pretty": "now"}


def test_render_news_blocks_javascript_link_and_escapes_title():
    html = build.render_news([_news_item("javascript:alert(1)")])
    assert "javascript:alert" not in html
    assert 'href="#"' in html
    assert "<script>alert(1)</script>" not in html   # title escaped
    assert "&lt;script&gt;" in html


def test_render_news_keeps_real_link():
    html = build.render_news([_news_item("https://news.example.com/a")])
    assert 'href="https://news.example.com/a"' in html


# --- render_industry_outlook: None ret_12m must not render "nan%" / TypeError ---

def test_industry_outlook_none_ret_guard():
    groups = [{
        "industry": "Tech", "avg_ret_12m": 5.0, "n_holdings": 1,
        "top_stocks": [{"ticker": "AAA", "upside": None, "rec": "",
                        "ret_12m": None, "cap_tier": ""}],
    }]
    html = build.render_industry_outlook(groups, universe_size=1)
    assert "nan" not in html.lower()
    assert "&mdash;" in html   # the placeholder for the missing return
