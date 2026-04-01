#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
 test_vrl.py — VISHAL RAJPUT TRADE v12.15 Test Suite
 Imports ACTUAL code. No copies. No simulations.
 Run: python3 test_vrl.py
═══════════════════════════════════════════════════════════════
"""

import sys
import os
from datetime import date, datetime, timedelta
from unittest.mock import patch, MagicMock
from copy import deepcopy
import pandas as pd

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
#  LEVEL 1: UNIT TESTS — Pure Logic, No API
# ═══════════════════════════════════════════════════════════════

section("STRIKE SELECTION")

import VRL_DATA as D

# resolve_strike_for_direction — tolerance zone logic
# DTE 1+: step=100, tolerance=±40
s = D.resolve_strike_for_direction(22819, "CE", 3)
test("CE 22819 DTE3 → 22800 (within ±40 of 22800)", s == 22800, "got " + str(s))

s = D.resolve_strike_for_direction(22819, "PE", 3)
test("PE 22819 DTE3 → 22800 (within ±40 of 22800)", s == 22800, "got " + str(s))

s = D.resolve_strike_for_direction(22845, "CE", 3)
test("CE 22845 DTE3 → 22800 (outside ±40, round down)", s == 22800, "got " + str(s))

s = D.resolve_strike_for_direction(22845, "PE", 3)
test("PE 22845 DTE3 → 22900 (outside ±40, round up)", s == 22900, "got " + str(s))

s = D.resolve_strike_for_direction(22930, "CE", 3)
test("CE 22930 DTE3 → 22900 (within ±40 of 22900)", s == 22900, "got " + str(s))

s = D.resolve_strike_for_direction(22930, "PE", 3)
test("PE 22930 DTE3 → 22900 (within ±40 of 22900)", s == 22900, "got " + str(s))

s = D.resolve_strike_for_direction(22960, "CE", 3)
test("CE 22960 DTE3 → 23000 (distance=40, within tolerance)", s == 23000, "got " + str(s))

s = D.resolve_strike_for_direction(22960, "PE", 3)
test("PE 22960 DTE3 → 23000 (distance=40, round up)", s == 23000, "got " + str(s))

# DTE 0: step=50, tolerance=±20
s = D.resolve_strike_for_direction(22820, "CE", 0)
test("CE 22820 DTE0 → 22800 (within ±20 of 22800)", s == 22800, "got " + str(s))

s = D.resolve_strike_for_direction(22835, "PE", 0)
test("PE 22835 DTE0 → 22850 (outside ±20, round up)", s == 22850, "got " + str(s))

s = D.resolve_strike_for_direction(22835, "CE", 0)
test("CE 22835 DTE0 → 22850 (distance=15, within ±20)", s == 22850, "got " + str(s))

# Exactly at boundary
s = D.resolve_strike_for_direction(22840, "CE", 3)
test("CE 22840 DTE3 → boundary (distance=40)", s in (22800, 22900), "got " + str(s))

s = D.resolve_strike_for_direction(22800, "CE", 3)
test("CE 22800 DTE3 → 22800 (exactly at strike)", s == 22800, "got " + str(s))


section("RSI CONSTANTS")

test("RSI_1M_LOW = 30", D.RSI_1M_LOW == 30, "got " + str(D.RSI_1M_LOW))
test("RSI_1M_HIGH_NORMAL = 55", D.RSI_1M_HIGH_NORMAL == 55, "got " + str(D.RSI_1M_HIGH_NORMAL))
test("RSI_1M_HIGH_STRONG = 70", D.RSI_1M_HIGH_STRONG == 70, "got " + str(D.RSI_1M_HIGH_STRONG))
test("RSI_3M_LOW = 42", D.RSI_3M_LOW == 42, "got " + str(D.RSI_3M_LOW))
test("RSI_3M_HIGH = 72", D.RSI_3M_HIGH == 72, "got " + str(D.RSI_3M_HIGH))


section("SPOT REGIME — PRICE ACTION")

import numpy as np

def _make_candles_df(candle_list):
    """Build a DataFrame from (open, high, low, close) tuples for regime testing."""
    rows = []
    base = datetime(2026, 4, 1, 9, 15)
    for i, (o, h, l, c) in enumerate(candle_list):
        rows.append({"open": o, "high": h, "low": l, "close": c, "volume": 1000})
    df = pd.DataFrame(rows)
    df.index = [base + timedelta(minutes=3*i) for i in range(len(rows))]
    df.index.name = "timestamp"
    return df

# TRENDING_STRONG: higher highs + breakout
_candles_strong = [(100,102,99,101), (101,104,100,103), (103,106,102,105),
                   (105,108,104,107), (107,110,106,109), (109,112,108,111),
                   (111,114,110,113), (113,118,112,117), (117,124,116,123),  # breakout
                   (123,130,122,129)]  # big body
with patch.object(D, 'get_historical_data', return_value=_make_candles_df(_candles_strong)):
    r = D.compute_spot_regime()
    test("HH + breakout → TRENDING_STRONG", r == "TRENDING_STRONG", "got " + r)

# CHOPPY: wide range > 30pts, no HH/LL in last 3 (zigzag)
_candles_choppy = [(100,120,90,110), (110,125,85,90), (90,130,80,120),
                   (120,135,75,80), (80,125,70,115), (115,140,75,85),
                   (85,130,70,120), (120,135,80,85), (85,120,75,110),
                   (110,115,80,95)]  # last3 highs: 120,115 — NOT HH. range>30
with patch.object(D, 'get_historical_data', return_value=_make_candles_df(_candles_choppy)):
    r = D.compute_spot_regime()
    test("Zigzag wide range → CHOPPY", r == "CHOPPY", "got " + r)

# TRENDING: higher highs, no breakout (normal body size)
_candles_trend = [(100,103,99,102), (102,105,101,104), (104,107,103,106),
                  (106,109,105,108), (108,111,107,110), (110,113,109,112),
                  (112,115,111,114), (114,117,113,116), (116,119,115,118),
                  (118,121,117,120)]  # HH in last3, range=30, no breakout (bodies ~2)
with patch.object(D, 'get_historical_data', return_value=_make_candles_df(_candles_trend)):
    r = D.compute_spot_regime()
    test("Steady HH → TRENDING", r == "TRENDING", "got " + r)

# NEUTRAL: tight range < 30pts, no HH/LL
_candles_neutral = [(100,105,98,102), (102,106,99,101), (101,104,97,99),
                    (99,103,96,101), (101,105,98,100), (100,104,97,102),
                    (102,106,99,101), (101,104,97,99), (99,102,97,100),
                    (100,103,98,99)]  # last3 highs: 104,102,103 — NOT HH. range<30
with patch.object(D, 'get_historical_data', return_value=_make_candles_df(_candles_neutral)):
    r = D.compute_spot_regime()
    test("Tight range < 30 → NEUTRAL", r == "NEUTRAL", "got " + r)


section("PREMIUM FILTER CONSTANTS")

test("STRIKE_PREMIUM_MIN = 100", D.STRIKE_PREMIUM_MIN == 100, "got " + str(D.STRIKE_PREMIUM_MIN))
test("STRIKE_PREMIUM_MAX = 400", D.STRIKE_PREMIUM_MAX == 400, "got " + str(D.STRIKE_PREMIUM_MAX))


# ═══════════════════════════════════════════════════════════════
#  ENGINE TESTS
# ═══════════════════════════════════════════════════════════════

import VRL_ENGINE as E

section("PROFIT FLOORS")

def _make_trail_state(entry, peak, phase=3):
    return {
        "token": 12345, "entry_price": entry, "peak_pnl": peak,
        "trough_pnl": 0, "exit_phase": phase, "in_trade": True,
        "mode": "CONVICTION", "trail_tightened": False,
        "profit_locked": False, "_rsi_was_overbought": False,
        "dte_at_entry": 3,
    }

# Mock D.get_historical_data to avoid API calls in trail
with patch.object(D, 'get_historical_data', return_value=MagicMock(empty=True)):
    # Peak 10, running drops to 4 → PROFIT_FLOOR_10
    st = _make_trail_state(100, 10)
    should, reason, _ = E._conviction_trail(st, 104, {})
    test("Peak 10 running 4 → PROFIT_FLOOR_10", should and "FLOOR_10" in reason,
         "got " + str(should) + " " + reason)

    # Peak 10, running 6 → no exit
    st = _make_trail_state(100, 10)
    should, reason, _ = E._conviction_trail(st, 106, {})
    test("Peak 10 running 6 → no exit", not should, "got exit=" + str(should))

    # Peak 20, running 11 → PROFIT_FLOOR_20
    st = _make_trail_state(100, 20)
    should, reason, _ = E._conviction_trail(st, 111, {})
    test("Peak 20 running 11 → PROFIT_FLOOR_20", should and "FLOOR_20" in reason,
         "got " + str(should) + " " + reason)

    # Peak 20, running 13 → no exit
    st = _make_trail_state(100, 20)
    should, reason, _ = E._conviction_trail(st, 113, {})
    test("Peak 20 running 13 → no exit", not should, "got exit=" + str(should))

    # Peak 30, running 19 → PROFIT_FLOOR_30
    st = _make_trail_state(100, 30)
    should, reason, _ = E._conviction_trail(st, 119, {})
    test("Peak 30 running 19 → PROFIT_FLOOR_30", should and "FLOOR_30" in reason,
         "got " + str(should) + " " + reason)

    # Peak 50, running 29 → PROFIT_FLOOR_50 (60% of 50 = 30)
    st = _make_trail_state(100, 50)
    should, reason, _ = E._conviction_trail(st, 129, {})
    test("Peak 50 running 29 → PROFIT_FLOOR_50", should and "FLOOR_50" in reason,
         "got " + str(should) + " " + reason)

    # Peak 50, running 31 → no exit (above 60% floor)
    st = _make_trail_state(100, 50)
    should, reason, _ = E._conviction_trail(st, 131, {})
    test("Peak 50 running 31 → no exit", not should, "got exit=" + str(should))


section("STALE ENTRY CUT")

st_stale = {
    "in_trade": True, "entry_price": 200, "exit_phase": 1,
    "phase1_sl": 180, "peak_pnl": 3, "trough_pnl": -2,
    "candles_held": 3, "mode": "CONVICTION",
}

should, reason, _ = E.manage_exit(st_stale, 201, {"conv_breakeven_pts": 20})
test("3 candles + peak<5 → STALE_ENTRY", should and reason == "STALE_ENTRY",
     "got " + str(should) + " " + reason)

st_not_stale = deepcopy(st_stale)
st_not_stale["peak_pnl"] = 8
should, reason, _ = E.manage_exit(st_not_stale, 205, {"conv_breakeven_pts": 20})
test("3 candles + peak=8 → no stale", not should or reason != "STALE_ENTRY",
     "got " + str(should) + " " + reason)

st_early = deepcopy(st_stale)
st_early["candles_held"] = 2
should, reason, _ = E.manage_exit(st_early, 201, {"conv_breakeven_pts": 20})
test("2 candles + peak<5 → no stale (wait)", not should or reason != "STALE_ENTRY",
     "got " + str(should) + " " + reason)


section("PHASE 1 SL")

st_sl = {
    "in_trade": True, "entry_price": 200, "exit_phase": 1,
    "phase1_sl": 185, "peak_pnl": 0, "trough_pnl": -5,
    "candles_held": 1, "mode": "CONVICTION",
}

should, reason, _ = E.manage_exit(st_sl, 184, {"conv_breakeven_pts": 20})
test("LTP below phase1_sl → PHASE1_SL", should and reason == "PHASE1_SL",
     "got " + str(should) + " " + reason)

should, reason, _ = E.manage_exit(st_sl, 190, {"conv_breakeven_pts": 20})
test("LTP above phase1_sl → no exit", not should, "got exit=" + str(should))


section("PHASE TRANSITIONS")

st_phase = {
    "in_trade": True, "entry_price": 200, "exit_phase": 1,
    "phase1_sl": 180, "peak_pnl": 20, "trough_pnl": 0,
    "candles_held": 10, "mode": "CONVICTION",
}

E.manage_exit(st_phase, 220, {"conv_breakeven_pts": 20})
test("+20pts → Phase 1→2", st_phase["exit_phase"] == 2,
     "got phase=" + str(st_phase["exit_phase"]))

test("Phase 2 SL ratcheted = entry+2+max(0,pnl-10)", st_phase.get("phase2_sl", 0) == 212,
     "got sl=" + str(st_phase.get("phase2_sl")))


section("3-MIN GATE THRESHOLD")

# Verify the gate is set to 2/4
import inspect
src = inspect.getsource(E._check_3min)
test("3-min gate threshold = 2", "conditions_met >= 2" in src,
     "expected 'conditions_met >= 2' in _check_3min source")


section("FAIL-CLOSED")

# _check_3min should return False on exception
# We can test by passing an invalid token that causes an error
with patch.object(D, 'get_historical_data', side_effect=Exception("API error")):
    permitted, det, bonus = E._check_3min(99999, "CE", {}, 3)
    test("_check_3min exception → permitted=False (fail-closed)",
         permitted == False, "got permitted=" + str(permitted))

# _check_3min should return False on empty/short data
with patch.object(D, 'get_historical_data', return_value=pd.DataFrame()):
    permitted, det, bonus = E._check_3min(99999, "CE", {}, 3)
    test("_check_3min empty data → permitted=False",
         permitted == False, "got permitted=" + str(permitted))


section("SCORE ENTRY — MULTI-TF ADX BONUS")

det_1m = {"body_ok": True, "body_pct": 55, "rsi_ok": True,
           "rsi_rising": True, "vol_ok": True, "rsi_val": 40}
greeks = {"delta": 0.5}
profile = {"delta_min": 0.35, "delta_max": 0.65}

def _mock_spot_high_adx(interval):
    return {"adx": 30, "spread": 15, "rsi": 45}

with patch.object(D, 'get_spot_indicators', side_effect=_mock_spot_high_adx):
    score, bd = E.score_entry(det_1m, greeks, profile, 8.0, 6.0, "CE")
    test("All conditions + multi-TF ADX → score includes MTF bonus",
         bd.get("multi_tf_adx") == 1, "got multi_tf_adx=" + str(bd.get("multi_tf_adx")))
    test("Max score with all bonuses = 7", score == 7,
         "got score=" + str(score))

def _mock_spot_low_adx(interval):
    if interval == "5minute":
        return {"adx": 15, "spread": 5, "rsi": 45}
    return {"adx": 30, "spread": 15, "rsi": 45}

with patch.object(D, 'get_spot_indicators', side_effect=_mock_spot_low_adx):
    score, bd = E.score_entry(det_1m, greeks, profile, 8.0, 6.0, "CE")
    test("5m ADX < 25 → no MTF bonus", bd.get("multi_tf_adx") == 0,
         "got multi_tf_adx=" + str(bd.get("multi_tf_adx")))


section("VERSION")

test("VERSION = v12.15.1", D.VERSION == "v12.15.1", "got " + str(D.VERSION))


# ═══════════════════════════════════════════════════════════════
#  LEVEL 2: INTEGRATION TESTS — check_entry flow
# ═══════════════════════════════════════════════════════════════

section("CHECK_ENTRY — REGIME BLOCKS")

def _mock_regime_choppy():
    return "CHOPPY"

def _mock_regime_trending():
    return "TRENDING"

# Test: CHOPPY blocks entry but still populates dashboard data
with patch.object(D, 'compute_spot_regime', return_value="CHOPPY"), \
     patch.object(D, 'calculate_dte', return_value=3), \
     patch.object(D, 'get_historical_data', return_value=pd.DataFrame()), \
     patch.object(D, 'add_indicators', return_value=pd.DataFrame()), \
     patch.object(D, 'get_full_greeks', return_value={}):
    r = E.check_entry(99999, "CE", {}, 22900, 22900, date.today(), "MORNING")
    test("CHOPPY regime → not fired", r["fired"] == False, "got fired=" + str(r["fired"]))
    test("CHOPPY regime → regime set", r["regime"] == "CHOPPY", "got " + r["regime"])


# Test: UNKNOWN blocks
with patch.object(D, 'compute_spot_regime', return_value="UNKNOWN"), \
     patch.object(D, 'calculate_dte', return_value=3), \
     patch.object(D, 'get_historical_data', return_value=pd.DataFrame()), \
     patch.object(D, 'add_indicators', return_value=pd.DataFrame()), \
     patch.object(D, 'get_full_greeks', return_value={}):
    r = E.check_entry(99999, "CE", {}, 22900, 22900, date.today(), "MORNING")
    test("UNKNOWN regime → not fired", r["fired"] == False)


section("CHECK_ENTRY — PREMIUM FILTER")

# We need a more complete mock to get past regime + 3m gate + 1m signal
# to reach the premium filter. This tests the boundary.
test("Premium constants correct", D.STRIKE_PREMIUM_MIN == 100 and D.STRIKE_PREMIUM_MAX == 400)


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
