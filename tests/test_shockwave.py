# tests/test_shockwave.py
import numpy as np
import pandas as pd
import pytest
import build


def test_shockwave_presets_wellformed():
    P = build.SHOCKWAVE_PRESETS
    assert isinstance(P, list) and len(P) >= 6
    labels = [p["label"] for p in P]
    assert len(labels) == len(set(labels))                      # unique labels
    has_pos = any(p["spy"] > 0 for p in P)
    has_neg = any(p["spy"] < 0 for p in P)
    assert has_pos and has_neg                                  # positive AND negative
    for p in P:
        assert set(["label", "spy", "tech", "usd", "likelihood", "recovery"]).issubset(p)
        assert p["likelihood"] in {"common", "occasional", "rare"}
        if p["spy"] < 0:
            assert isinstance(p["recovery"], str) and p["recovery"]   # drawdowns carry recovery
        else:
            assert p["recovery"] is None                              # upside: no recovery
