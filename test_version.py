"""v3.0: dashboard version read dynamically from CHANGELOG's top heading."""
import build


def test_dashboard_version_reads_top_changelog_heading(tmp_path):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("# Changelog\n\n## v3.0 — UX polish\n\nstuff\n\n## v2.9 — Old\n",
                  encoding="utf-8")
    assert build._dashboard_version(cl) == "3.0"


def test_dashboard_version_picks_first_heading_not_later_ones(tmp_path):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("## v3.0 — a\n## v2.9 — b\n## v1.0 — c\n", encoding="utf-8")
    assert build._dashboard_version(cl) == "3.0"


def test_dashboard_version_fallback_when_missing_or_unparseable(tmp_path):
    assert build._dashboard_version(tmp_path / "nope.md") == ""
    bad = tmp_path / "CHANGELOG.md"
    bad.write_text("# Changelog\n\nno version headers here\n", encoding="utf-8")
    assert build._dashboard_version(bad) == ""


def test_committed_changelog_top_is_v3():
    # The real CHANGELOG should now lead with the v3.0 entry.
    assert build._dashboard_version() == "3.0"
