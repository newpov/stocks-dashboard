"""Guards the CI publish fail-safe. validate_build_invariants is the load-bearing
check that turns a degraded --from-snapshot build into a non-zero exit so CI skips
the commit and the last-good page stays live. A refactor that broke it would
silently disable that protection, so pin its behaviour."""
import pandas as pd
import pytest

import build


def _ok_basket():
    return pd.Series([0.0, 1.5, 2.0])


def _ok_prices():
    return pd.DataFrame({"AAPL": [100.0, 101.0, 102.0]})


def _ok_positions():
    return pd.DataFrame({"baseline": [100.0]}, index=["AAPL"])


def test_valid_trio_passes():
    # No raise on a well-formed build.
    build.validate_build_invariants(_ok_basket(), _ok_prices(), _ok_positions())


def test_none_prices_raises():
    with pytest.raises(SystemExit):
        build.validate_build_invariants(_ok_basket(), None, _ok_positions())


def test_empty_prices_raises():
    with pytest.raises(SystemExit):
        build.validate_build_invariants(_ok_basket(), pd.DataFrame(), _ok_positions())


def test_empty_basket_raises():
    with pytest.raises(SystemExit):
        build.validate_build_invariants(pd.Series(dtype=float), _ok_prices(), _ok_positions())


def test_nan_basket_last_raises():
    with pytest.raises(SystemExit):
        build.validate_build_invariants(pd.Series([0.0, float("nan")]),
                                        _ok_prices(), _ok_positions())


def test_inf_basket_last_raises():
    with pytest.raises(SystemExit):
        build.validate_build_invariants(pd.Series([0.0, float("inf")]),
                                        _ok_prices(), _ok_positions())


def test_none_positions_raises():
    with pytest.raises(SystemExit):
        build.validate_build_invariants(_ok_basket(), _ok_prices(), None)


def test_empty_positions_raises():
    with pytest.raises(SystemExit):
        build.validate_build_invariants(_ok_basket(), _ok_prices(), pd.DataFrame())
