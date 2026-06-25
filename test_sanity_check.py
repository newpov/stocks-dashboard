import json
import pandas as pd
import pytest
import sanity_check as sc


def test_position_count_floor_passes_for_real_basket():
    df = pd.DataFrame({"ticker": [f"T{i}" for i in range(15)]})
    sc.check_position_count(df, floor=10)   # no raise


def test_position_count_floor_raises_when_too_few():
    df = pd.DataFrame({"ticker": ["A", "B"]})
    with pytest.raises(SystemExit):
        sc.check_position_count(df, floor=10)


def test_output_files_pass(tmp_path):
    html = tmp_path / "index.html"
    html.write_bytes(b"x" * (600 * 1024))
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"ok": True}))
    sc.check_output_files(html, payload, min_kb=500)   # no raise


def test_output_files_raise_on_tiny_html(tmp_path):
    html = tmp_path / "index.html"
    html.write_bytes(b"x" * (10 * 1024))
    payload = tmp_path / "payload.json"
    payload.write_text("{}")
    with pytest.raises(SystemExit):
        sc.check_output_files(html, payload, min_kb=500)


def test_output_files_raise_on_bad_payload(tmp_path):
    html = tmp_path / "index.html"
    html.write_bytes(b"x" * (600 * 1024))
    payload = tmp_path / "payload.json"
    payload.write_text("{not json")
    with pytest.raises(SystemExit):
        sc.check_output_files(html, payload, min_kb=500)


def test_band_passes_when_close_to_last():
    df = pd.DataFrame({"ticker": [f"T{i}" for i in range(180)]})
    sc.check_position_count(df, last_count=185)   # no raise


def test_band_fails_on_big_drop():
    df = pd.DataFrame({"ticker": [f"T{i}" for i in range(30)]})
    with pytest.raises(SystemExit):
        sc.check_position_count(df, last_count=185)


def test_band_ignored_when_no_prior():
    df = pd.DataFrame({"ticker": [f"T{i}" for i in range(30)]})
    sc.check_position_count(df, last_count=None)   # first run -> floor only, no raise


def test_meta_roundtrip(tmp_path):
    p = tmp_path / "last_publish_meta.json"
    sc.write_last_count(185, p)
    assert sc.read_last_count(p) == 185


def test_read_last_count_missing(tmp_path):
    assert sc.read_last_count(tmp_path / "nope.json") is None
