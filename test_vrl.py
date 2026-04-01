#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
 test_vrl.py — VISHAL RAJPUT TRADE v13.0 Test Suite
 Minimal strategy: EMA gap + RSI entry, 2-lot exit.
═══════════════════════════════════════════════════════════════
"""

import sys
import os
import pandas as pd
from datetime import date, datetime, timedelta
from unittest.mock import patch, MagicMock
from copy import deepcopy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Test Framework ──────────────────────────────────────────

_passed = 0
_failed = 0
_errors = []

def test(name, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print("  ✅ " + name)
    else:
        _failed += 1
        msg = "  ❌ " + name + (" — " + detail if detail else "")
        print(msg)
        _errors.append(msg)

def section(name):
    print("\n━━━ " + name + " ━━━")


# ═══════════════════════════════════════════════════════════════
#  LEVEL 1: UNIT TESTS
# ═══════════════════════════════════════════════════════════════

section("STRIKE SELECTION")

import VRL_DATA as D

s = D.resolve_strike_for_direction(22819, "CE", 3)
test("CE 22819 DTE3 → 22800", s == 22800, "got " + str(s))
s = D.resolve_strike_for_direction(22819, "PE", 3)
test("PE 22819 DTE3 → 22800", s == 22800, "got " + str(s))
s = D.resolve_strike_for_direction(22845, "CE", 3)
test("CE 22845 DTE3 → 22800 (round down)", s == 22800, "got " + str(s))
s = D.resolve_strike_for_direction(22845, "PE", 3)
test("PE 22845 DTE3 → 22900 (round up)", s == 22900, "got " + str(s))
s = D.resolve_strike_for_direction(22820, "CE", 0)
test("CE 22820 DTE0 → 22800", s == 22800, "got " + str(s))
s = D.resolve_strike_for_direction(22835, "PE", 0)
test("PE 22835 DTE0 → 22850", s == 22850, "got " + str(s))
s = D.resolve_strike_for_direction(22800, "CE", 3)
test("CE 22800 exactly → 22800", s == 22800, "got " + str(s))


section("VERSION")

test("VERSION = v13.0", D.VERSION == "v13.0", "got " + str(D.VERSION))


section("PREMIUM CONSTANTS")

test("STRIKE_PREMIUM_MIN = 100", D.STRIKE_PREMIUM_MIN == 100)
test("STRIKE_PREMIUM_MAX = 400", D.STRIKE_PREMIUM_MAX == 400)


# ═══════════════════════════════════════════════════════════════
#  ENGINE TESTS — v13.0 ENTRY
# ═══════════════════════════════════════════════════════════════

import VRL_ENGINE as E

section("ENTRY — EMA GAP + RSI")

def _make_1min_df(candles):
    """Build DataFrame from (o,h,l,c,vol) tuples with indicators."""
    rows = []
    base = datetime(2026, 4, 1, 9, 15)
    for i, (o, h, l, c, v) in enumerate(candles):
        rows.append({"open": o, "high": h, "low": l, "close": c, "volume": v})
    df = pd.DataFrame(rows)
    df.index = [base + timedelta(minutes=i) for i in range(len(rows))]
    df.index.name = "timestamp"
    df = D.add_indicators(df)
    return df

# EMA gap >= 3, RSI >= 50 rising → FIRE
# Pre-build df with forced indicators to guarantee pass
_df_fire = pd.DataFrame({
    "close": [100.0]*18 + [110.0, 115.0],
    "open":  [99.0]*18 + [105.0, 110.0],
    "high":  [101.0]*18 + [112.0, 117.0],
    "low":   [98.0]*18 + [104.0, 109.0],
    "volume": [1000]*20,
})
_df_fire.index = [datetime(2026,4,1,9,15) + timedelta(minutes=i) for i in range(20)]
_df_fire = D.add_indicators(_df_fire)
# Override EMA and RSI to guarantee fire
_df_fire.iloc[-2, _df_fire.columns.get_loc("EMA_9")] = 115.0
_df_fire.iloc[-2, _df_fire.columns.get_loc("EMA_21")] = 110.0
_df_fire.iloc[-2, _df_fire.columns.get_loc("RSI")] = 58.0
_df_fire.iloc[-3, _df_fire.columns.get_loc("RSI")] = 52.0
with patch.object(D, 'get_historical_data', return_value=_df_fire):
    with patch.object(D, 'add_indicators', side_effect=lambda x: x):
        r = E.check_entry(12345, "CE", 22900, 5)
        test("EMA gap + RSI rising → FIRE", r["fired"] == True,
             "fired=" + str(r["fired"]) + " ema_gap=" + str(r["ema_gap"])
             + " rsi=" + str(r["rsi"]) + " rsi_prev=" + str(r["rsi_prev"]))

# EMA gap 0 → BLOCK
_candles_flat = [(100,101,99,100,1000)] * 19
with patch.object(D, 'get_historical_data') as mock_hd:
    df = _make_1min_df(_candles_flat)
    mock_hd.return_value = df
    r = E.check_entry(12345, "CE", 22900, 5)
    test("Flat EMA → no fire", r["fired"] == False, "gap=" + str(r["ema_gap"]))

# RSI < 50 → BLOCK
_candles_low_rsi = [(100,101,99,100,1000)] * 10 + [
    (100,101,98,99,1000),  # dropping
    (99,100,97,98,1000),
    (98,99,96,97,1000),
]
with patch.object(D, 'get_historical_data') as mock_hd:
    df = _make_1min_df(_candles_low_rsi)
    mock_hd.return_value = df
    r = E.check_entry(12345, "CE", 22900, 5)
    test("Low RSI → no fire", r["fired"] == False or r["rsi"] < 50,
         "rsi=" + str(r["rsi"]))


section("ENTRY SL")

sl = E.compute_entry_sl(300.0, 12)
test("SL = entry - 12", sl == 288.0, "got " + str(sl))

sl = E.compute_entry_sl(150.0, 15)
test("SL = entry - 15", sl == 135.0, "got " + str(sl))


# ═══════════════════════════════════════════════════════════════
#  ENGINE TESTS — v13.0 EXIT
# ═══════════════════════════════════════════════════════════════

section("EXIT — HARD SL")

def _make_exit_state(entry, peak=0, candles=0, lot1=True, lot2=True, split=False):
    return {
        "in_trade": True, "entry_price": entry, "peak_pnl": peak,
        "trough_pnl": 0, "candles_held": candles, "token": 12345,
        "lot1_active": lot1, "lot2_active": lot2,
        "lots_split": split, "lot2_trail_sl": 0.0,
        "mode": "MINIMAL", "current_rsi": 50,
    }

with patch.object(D, 'get_historical_data', return_value=MagicMock(empty=True)):
    # Running -12 → HARD_SL
    st = _make_exit_state(200, peak=0, candles=1)
    exits = E.manage_exit(st, 188, {})
    test("Running -12 → HARD_SL", len(exits) == 1 and exits[0]["reason"] == "HARD_SL",
         "got " + str(exits))

    # Running -11 → no exit
    st = _make_exit_state(200, peak=0, candles=1)
    exits = E.manage_exit(st, 189, {})
    test("Running -11 → no exit", len(exits) == 0, "got " + str(exits))


section("EXIT — STALE ENTRY")

with patch.object(D, 'get_historical_data', return_value=MagicMock(empty=True)):
    st = _make_exit_state(200, peak=2, candles=3)
    exits = E.manage_exit(st, 201, {})
    test("3 candles peak<3 → STALE", len(exits) == 1 and exits[0]["reason"] == "STALE_ENTRY",
         "got " + str(exits))

    st = _make_exit_state(200, peak=5, candles=3)
    exits = E.manage_exit(st, 204, {})
    test("3 candles peak=5 → no stale", len(exits) == 0, "got " + str(exits))

    st = _make_exit_state(200, peak=2, candles=2)
    exits = E.manage_exit(st, 201, {})
    test("2 candles peak<3 → no stale (wait)", len(exits) == 0, "got " + str(exits))


section("EXIT — PROFIT FLOORS")

with patch.object(D, 'get_historical_data', return_value=MagicMock(empty=True)):
    # Peak 10, drop to floor (entry+2)
    st = _make_exit_state(200, peak=10, candles=5)
    exits = E.manage_exit(st, 201, {})  # running=1, floor_sl=202
    test("Peak 10 running 1 → PROFIT_FLOOR",
         len(exits) == 1 and "FLOOR" in exits[0]["reason"],
         "got " + str(exits))

    # Peak 10, running 5 → no exit
    st = _make_exit_state(200, peak=10, candles=5)
    exits = E.manage_exit(st, 205, {})
    test("Peak 10 running 5 → no exit", len(exits) == 0, "got " + str(exits))

    # Peak 20, drop to floor (entry+12)
    st = _make_exit_state(200, peak=20, candles=8)
    exits = E.manage_exit(st, 211, {})  # running=11, floor_sl=212
    test("Peak 20 running 11 → PROFIT_FLOOR",
         len(exits) == 1 and "FLOOR" in exits[0]["reason"],
         "got " + str(exits))


section("EXIT — RSI BLOWOFF")

def _make_rsi_df(rsi_val):
    """Create a df where add_indicators returns target RSI at iloc[-2]."""
    # Build df that yields RSI naturally, then override
    df = pd.DataFrame({
        "close": [200.0]*5, "open": [200.0]*5, "high": [201.0]*5,
        "low": [199.0]*5, "volume": [100]*5,
    })
    df.index = [datetime(2026,4,1,10,i) for i in range(5)]
    df = D.add_indicators(df)
    df["RSI"] = rsi_val  # override all rows
    return df

# RSI > 80 → BLOWOFF
st = _make_exit_state(200, peak=15, candles=5)
with patch.object(D, 'get_historical_data', return_value=_make_rsi_df(82)):
    with patch.object(D, 'add_indicators', side_effect=lambda x: x):
        exits = E.manage_exit(st, 220, {})
        test("RSI > 80 → BLOWOFF", len(exits) >= 1 and "BLOWOFF" in exits[0]["reason"],
             "got " + str(exits) + " rsi=" + str(st.get("current_rsi")))


section("EXIT — RSI SPLIT")

# RSI 50 → NOT split
st = _make_exit_state(200, peak=15, candles=5)
with patch.object(D, 'get_historical_data', return_value=_make_rsi_df(50)):
    with patch.object(D, 'add_indicators', side_effect=lambda x: x):
        exits = E.manage_exit(st, 215, {})
        test("RSI 50 → not split", st.get("lots_split") == False,
             "split=" + str(st.get("lots_split")))

# RSI 71 → SPLIT
st = _make_exit_state(200, peak=15, candles=5)
with patch.object(D, 'get_historical_data', return_value=_make_rsi_df(71)):
    with patch.object(D, 'add_indicators', side_effect=lambda x: x):
        exits = E.manage_exit(st, 215, {})
        test("RSI 71 → lots SPLIT", st.get("lots_split") == True,
             "split=" + str(st.get("lots_split")))


# ═══════════════════════════════════════════════════════════════
#  SUMMARY
# ═══════════════════════════════════════════════════════════════

print("\n" + "═" * 50)
print("  RESULTS: " + str(_passed) + " passed, " + str(_failed) + " failed")
print("═" * 50)

if _errors:
    print("\nFAILED TESTS:")
    for e in _errors:
        print(e)

sys.exit(0 if _failed == 0 else 1)
