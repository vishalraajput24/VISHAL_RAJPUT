#!/home/user/kite_env/bin/python3
"""
═══════════════════════════════════════════════════════════════
 test_peak_backfill.py — Batch 4/6 BUG-J regression

 Verifies the one-shot peak_history backfill runs when (and only
 when) the bot restarts with an open trade and the saved state has
 no peak_history yet. Prevents a 15-min VELOCITY_STALL blind spot
 right after restart.
═══════════════════════════════════════════════════════════════
"""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _m in ("kiteconnect", "pyotp", "requests"):
    if _m not in sys.modules:
        _stub = MagicMock()
        if _m == "kiteconnect":
            _stub.KiteTicker  = MagicMock()
            _stub.KiteConnect = MagicMock()
        sys.modules[_m] = _stub

import pandas as pd
import VRL_DATA as D
import VRL_ENGINE as E


_passed = 0
_failed = 0


def test(name, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print("  PASS " + name)
    else:
        _failed += 1
        print("  FAIL " + name + ((" — " + detail if detail else "")))


print("=== BUG-J — peak_history backfill on startup with open trade ===")


def _make_3m(closes):
    """Build an option 3-min DataFrame with the given close series."""
    n = len(closes)
    rows = []
    for c in closes:
        rows.append({"open": c - 0.5, "high": c + 0.5,
                     "low":  c - 1.0, "close": c, "volume": 1000})
    # Add one extra live in-progress bar
    rows.append({"open": closes[-1], "high": closes[-1] + 0.5,
                 "low":  closes[-1] - 0.5, "close": closes[-1],
                 "volume": 500})
    df = pd.DataFrame(rows)
    base = datetime(2026, 4, 17, 10, 0)
    df.index = [base + timedelta(minutes=3 * i) for i in range(len(rows))]
    df = D.add_indicators(df)
    # Force ema9_high above closes so EMA9_LOW_BREAK doesn't fire
    df["ema9_high"] = max(closes) + 5
    df["ema9_low"]  = min(closes) - 5
    return df


# 1. Cold-restart path: in_trade=True, peak_history empty → backfill runs.
#    Closes [100, 105, 108, 110, 108]. Fixture sets high = close + 0.5.
#    Entry=100. highs = [100.5, 105.5, 108.5, 110.5, 108.5].
#    pnl-at-high per bar: 0.5, 5.5, 8.5, 10.5, 8.5. Running max:
#      0.5, 5.5, 8.5, 10.5, 10.5.
_closes = [100, 105, 108, 110, 108]
_df = _make_3m(_closes)
_state = {
    "in_trade": True, "entry_price": 100.0, "peak_pnl": 10.0,
    "trough_pnl": 0, "candles_held": 5, "token": 12345,
    "peak_history": [],
    "last_peak_candle_ts": "",
    "last_band_check_ts": "",
}
with patch.object(D, "get_option_3min", return_value=_df):
    E.manage_exit(_state, 108.0, {})
_seeded = _state.get("peak_history", [])
test("1. backfill seeds peak_history from bar highs vs entry",
     _seeded == [0.5, 5.5, 8.5, 10.5, 10.5],
     "got " + str(_seeded))
test("2. sentinel flag set after backfill",
     _state.get("_peak_history_backfilled") is True,
     "sentinel=" + str(_state.get("_peak_history_backfilled")))

# 3. Gate — don't re-backfill on a second manage_exit call.
with patch.object(D, "get_option_3min", return_value=_df):
    _state["peak_history"] = []   # simulate someone clearing it
    E.manage_exit(_state, 108.0, {})
test("3. sentinel prevents re-backfill",
     _state.get("peak_history") == [],
     "got " + str(_state.get("peak_history")))

# 4. Skip when peak_history already populated.
_state2 = {
    "in_trade": True, "entry_price": 100.0, "peak_pnl": 10.0,
    "trough_pnl": 0, "candles_held": 5, "token": 12345,
    "peak_history": [5, 8, 10],   # not empty
    "last_peak_candle_ts": "",
    "last_band_check_ts": "",
}
with patch.object(D, "get_option_3min", return_value=_df):
    E.manage_exit(_state2, 108.0, {})
# Either unchanged or appended once by the normal path — key test is
# that backfill didn't clobber the 3-value prefix with a 5-value seed.
_ok_4 = (_state2["peak_history"][:3] == [5, 8, 10])
test("4. backfill skipped when peak_history already has values",
     _ok_4,
     "got " + str(_state2["peak_history"]))

# 5. Backfill GATE skips when entry_price is 0 — sentinel stays False.
#    (The normal per-candle append path still runs, that's expected.)
_state3 = {
    "in_trade": True, "entry_price": 0.0, "peak_pnl": 0.0,
    "trough_pnl": 0, "candles_held": 0, "token": 12345,
    "peak_history": [],
    "last_peak_candle_ts": "",
    "last_band_check_ts": "",
}
with patch.object(D, "get_option_3min", return_value=_df):
    E.manage_exit(_state3, 100.0, {})
test("5. backfill GATE skips when entry_price is 0 (sentinel stays False)",
     not _state3.get("_peak_history_backfilled"),
     "sentinel=" + str(_state3.get("_peak_history_backfilled")))

# 6. Sentinel + peak_history persist fields registered.
test("6. state-persist fields include _peak_history_backfilled",
     "_peak_history_backfilled" in D.STATE_PERSIST_FIELDS,
     "STATE_PERSIST_FIELDS missing _peak_history_backfilled")

# 7. Losing-trade restart, pre-STALE window: bar highs stayed below entry
#    → seeded running max stays 0, peak_history stays empty, sentinel set
#    so we don't re-attempt every tick. VELOCITY_STALL dormant; STALE_ENTRY
#    will handle it a couple of candles later when candles_held hits 5.
_losing_closes = [98, 97, 96]           # highs: 98.5, 97.5, 96.5
_df_lose = _make_3m(_losing_closes)
_state_lose = {
    "in_trade": True, "entry_price": 100.0, "peak_pnl": 0.0,
    "trough_pnl": -4, "candles_held": 3, "token": 12345,
    "peak_history": [],
    "last_peak_candle_ts": "",
    "last_band_check_ts": "",
}
with patch.object(D, "get_option_3min", return_value=_df_lose):
    E.manage_exit(_state_lose, 96.0, {})
#    NB: the normal per-candle append path still runs AFTER the backfill
#    skip, so peak_history may contain at most 1 fresh value (0) — that
#    single entry can't trigger VELOCITY_STALL (needs 5+), which is the
#    whole point of the guard.
_ph7 = _state_lose.get("peak_history") or []
test("7. losing-trade backfill skipped (max peak < vs_min_peak=3)",
     len(_ph7) <= 1
     and _state_lose.get("_peak_history_backfilled") is True,
     "ph=" + str(_ph7)
     + " sentinel=" + str(_state_lose.get("_peak_history_backfilled")))


print()
print("=" * 50)
print("RESULTS: " + str(_passed) + " passed, " + str(_failed) + " failed")
sys.exit(0 if _failed == 0 else 1)
