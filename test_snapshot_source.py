import build


def R(**kw):
    base = dict(demo=False, watchlist_only=False, from_snapshot=False,
                log_exists=False, snapshot_exists=False)
    base.update(kw)
    return build.resolve_basket_source(**base)


def test_watchlist_only_wins():
    assert R(watchlist_only=True, log_exists=True) == "watchlist"


def test_demo_always_uses_sample():
    assert R(demo=True, log_exists=True, snapshot_exists=True) == "csv"


def test_author_local_regenerates_then_snapshot():
    assert R(log_exists=True, snapshot_exists=True) == "log"
    assert R(log_exists=True, snapshot_exists=False) == "log"


def test_ci_uses_snapshot_only_with_flag():
    assert R(from_snapshot=True, snapshot_exists=True) == "snapshot"


def test_forker_plain_build_uses_their_csv_not_the_committed_snapshot():
    # cloner: no log, no flag, but the author's snapshot file is in their tree
    assert R(from_snapshot=False, snapshot_exists=True) == "csv"


def test_flag_without_snapshot_falls_back_to_csv():
    assert R(from_snapshot=True, snapshot_exists=False) == "csv"
