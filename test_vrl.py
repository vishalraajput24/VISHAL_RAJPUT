#!/home/vishalraajput24/kite_env/bin/python3
"""
═══════════════════════════════════════════════════════════════
 test_vrl.py — VISHAL RAJPUT TRADE v13.9 Test Suite
 30 interdependent complex tests covering the full trade lifecycle.
═══════════════════════════════════════════════════════════════
"""

import sys
import os
import json
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
#  SETUP — shared state across all tests
# ═══════════════════════════════════════════════════════════════

import VRL_DATA as D
import VRL_ENGINE as E
import VRL_CHARGES as CH

# Build a realistic 1-min DataFrame that can fire entries
# 20 candles: 14 flat at 100, then a strong uptrend to 118
_base_closes = [100.0]*14 + [100.0, 103.0, 108.0, 115.0, 117.0, 118.0]
_base_opens  = [99.5]*14  + [99.5, 101.0, 106.0, 108.0, 116.0, 117.0]
_base_highs  = [101.0]*14 + [101.0, 104.0, 109.0, 116.0, 118.0, 119.0]
_base_lows   = [98.0]*14  + [98.0, 100.0, 105.0, 106.0, 114.0, 117.0]

_df_base = pd.DataFrame({
    "close": _base_closes, "open": _base_opens,
    "high": _base_highs, "low": _base_lows, "volume": [1000]*20,
})
_df_base.index = [datetime(2026,4,14,9,15) + timedelta(minutes=i) for i in range(20)]
_df_base = D.add_indicators(_df_base)

# Override key indicators at the last 2 candles for controlled testing
_df_fire = _df_base.copy()
_df_fire.iloc[-2, _df_fire.columns.get_loc("EMA_9")] = 115.0
_df_fire.iloc[-2, _df_fire.columns.get_loc("EMA_21")] = 110.0
_df_fire.iloc[-2, _df_fire.columns.get_loc("RSI")] = 62.0
_df_fire.iloc[-3, _df_fire.columns.get_loc("RSI")] = 55.0
# prev candle EMA_9 for two_green_above check
_df_fire.iloc[-3, _df_fire.columns.get_loc("EMA_9")] = 112.0

# Spot DataFrame with rising EMA for slope >= 2
_df_spot = _df_base.copy()
_df_spot = D.add_indicators(_df_spot)
_df_spot.iloc[-2, _df_spot.columns.get_loc("EMA_9")] = 24010.0
_df_spot.iloc[-7, _df_spot.columns.get_loc("EMA_9")] = 24000.0  # slope = +10

# Other side DataFrame (falling)
_df_other = _df_base.copy()
_df_other = D.add_indicators(_df_other)
_df_other.iloc[-2, _df_other.columns.get_loc("close")] = 95.0
_df_other.iloc[-2, _df_other.columns.get_loc("EMA_9")] = 100.0  # close < EMA9
_df_other.iloc[-6, _df_other.columns.get_loc("close")] = 105.0  # falling 10pts


def _mock_hd(token, tf, *args, **kwargs):
    """Route historical data calls to appropriate mock DataFrames."""
    if token == D.NIFTY_SPOT_TOKEN:
        return _df_spot
    if token == 99999:  # other side token
        return _df_other
    return _df_fire


def _make_state(entry=200, peak=0, candles=0, in_trade=True):
    return {
        "in_trade": in_trade, "entry_price": entry, "peak_pnl": peak,
        "trough_pnl": 0, "candles_held": candles, "token": 12345,
        "lot1_active": True, "lot2_active": True,
        "lots_split": False, "entry_mode": "FAST",
        "current_rsi": 50, "_candle_low": entry,
        "phase1_sl": round(entry - 12, 2), "_static_floor_sl": 0,
    }


# ═══════════════════════════════════════════════════════════════
#  TEST 1-3: FOUNDATION (version, constants, strike)
# ═══════════════════════════════════════════════════════════════

section("FOUNDATION")

test("T01: VERSION is v13.9", D.VERSION == "v13.9", "got " + str(D.VERSION))

s = D.resolve_strike_for_direction(22819, "CE", 3)
test("T02: Strike CE 22819 DTE3 → 22800", s == 22800, "got " + str(s))

s = D.resolve_strike_for_direction(22835, "PE", 0)
test("T03: Strike PE 22835 DTE0 → 22850", s == 22850, "got " + str(s))


# ═══════════════════════════════════════════════════════════════
#  TEST 4-8: ENTRY — FAST PATH (v13.9 full chain)
# ═══════════════════════════════════════════════════════════════

section("ENTRY — FAST PATH (2 green + breakout + spot slope + divergence)")

# T04: Full FAST fire with all gates passing
with patch.object(D, 'get_historical_data', side_effect=_mock_hd):
    with patch.object(D, 'add_indicators', side_effect=lambda x: x):
        r = E.check_entry(12345, "CE", 24000, 3, other_token=99999)
        test("T04: Full FAST fire — all gates pass",
             r["fired"] == True and r["entry_mode"] == "FAST",
             "fired=" + str(r["fired"]) + " mode=" + str(r["entry_mode"]))
        # Chain: capture entry price for subsequent exit tests
        _entry_price = r["entry_price"]
        test("T05: Entry price captured from curr close",
             _entry_price == 117.0, "got " + str(_entry_price))

# T06: Breakout confirm blocks when close < prev_high
_df_no_breakout = _df_fire.copy()
_df_no_breakout.iloc[-2, _df_no_breakout.columns.get_loc("close")] = 115.5  # below prev high 116
with patch.object(D, 'get_historical_data', side_effect=lambda t,tf,*a,**k:
                  _df_spot if t==D.NIFTY_SPOT_TOKEN else _df_other if t==99999 else _df_no_breakout):
    with patch.object(D, 'add_indicators', side_effect=lambda x: x):
        r = E.check_entry(12345, "CE", 24000, 3, other_token=99999)
        test("T06: Close 115.5 < prev_high 116 → breakout blocked",
             r["fired"] == False, "fired=" + str(r["fired"]))

# T07: Spot slope blocks CE when flat
_df_spot_flat = _df_spot.copy()
_df_spot_flat.iloc[-2, _df_spot_flat.columns.get_loc("EMA_9")] = 24000.5
_df_spot_flat.iloc[-7, _df_spot_flat.columns.get_loc("EMA_9")] = 24000.0  # slope = 0.5 < 2
with patch.object(D, 'get_historical_data', side_effect=lambda t,tf,*a,**k:
                  _df_spot_flat if t==D.NIFTY_SPOT_TOKEN else _df_other if t==99999 else _df_fire):
    with patch.object(D, 'add_indicators', side_effect=lambda x: x):
        r = E.check_entry(12345, "CE", 24000, 3, other_token=99999)
        test("T07: Spot slope 0.5 < 2 → CE blocked",
             r["fired"] == False, "fired=" + str(r["fired"]))

# T08: Other side above EMA9 → FAST blocked (no divergence)
_df_other_up = _df_other.copy()
_df_other_up.iloc[-2, _df_other_up.columns.get_loc("close")] = 105.0
_df_other_up.iloc[-6, _df_other_up.columns.get_loc("close")] = 100.0  # rising
with patch.object(D, 'get_historical_data', side_effect=lambda t,tf,*a,**k:
                  _df_spot if t==D.NIFTY_SPOT_TOKEN else _df_other_up if t==99999 else _df_fire):
    with patch.object(D, 'add_indicators', side_effect=lambda x: x):
        r = E.check_entry(12345, "CE", 24000, 3, other_token=99999)
        test("T08: Other side rising → divergence fail → blocked",
             r["fired"] == False, "fired=" + str(r["fired"]))


# ═══════════════════════════════════════════════════════════════
#  TEST 9-11: TIME-AWARE RSI CAP
# ═══════════════════════════════════════════════════════════════

section("RSI CAP — TIME AWARE")

cap, ses = E._get_rsi_cap(False)
test("T09: RSI cap function returns valid tuple",
     cap in (72, 75, 78) and ses in ("MORNING", "MIDDAY", "AFTERNOON"),
     "cap=" + str(cap) + " ses=" + ses)

cap_agg, _ = E._get_rsi_cap(True)
test("T10: Aggressive adds +3", cap_agg == cap + 3,
     "normal=" + str(cap) + " agg=" + str(cap_agg))

# T11: RSI above cap blocks entry
_df_rsi_high = _df_fire.copy()
_df_rsi_high.iloc[-2, _df_rsi_high.columns.get_loc("RSI")] = float(cap + 1)
_df_rsi_high.iloc[-3, _df_rsi_high.columns.get_loc("RSI")] = float(cap - 5)
with patch.object(D, 'get_historical_data', side_effect=lambda t,tf,*a,**k:
                  _df_spot if t==D.NIFTY_SPOT_TOKEN else _df_other if t==99999 else _df_rsi_high):
    with patch.object(D, 'add_indicators', side_effect=lambda x: x):
        r = E.check_entry(12345, "CE", 24000, 3, other_token=99999)
        test("T11: RSI " + str(cap+1) + " > cap " + str(cap) + " → blocked",
             r["fired"] == False, "fired=" + str(r["fired"]))


# ═══════════════════════════════════════════════════════════════
#  TEST 12-13: ENTRY CUTOFF + STOP HUNT RECOVERY
# ═══════════════════════════════════════════════════════════════

section("ENTRY CUTOFF + STOP HUNT RECOVERY")

# T12: 15:11 blocks entry
with patch.object(D, 'get_historical_data', side_effect=_mock_hd):
    with patch.object(D, 'add_indicators', side_effect=lambda x: x):
        with patch.object(D, 'is_market_open', return_value=True):
            with patch('VRL_ENGINE.datetime') as mock_dt:
                mock_dt.now.return_value = datetime(2026, 4, 14, 15, 11, 0)
                mock_dt.fromisoformat = datetime.fromisoformat
                r = E.check_entry(12345, "CE", 24000, 3, other_token=99999)
                test("T12: 15:11 entry cutoff → blocked",
                     r["fired"] == False, "fired=" + str(r["fired"]))

# T13: Stop hunt recovery allows re-entry
_sh_state = {
    "last_exit_time": (datetime.now() - timedelta(minutes=1.5)).isoformat(),
    "last_exit_direction": "CE", "last_exit_reason": "CANDLE_SL",
    "last_exit_price": 180.0, "last_exit_peak": 5.0,
    "in_trade": False, "daily_trades": 0, "daily_losses": 0, "paused": False,
}
with patch.object(D, 'is_entry_fire_window', return_value=True), \
     patch.object(D, 'is_market_open', return_value=True), \
     patch.object(D, 'is_tick_live', return_value=True):
    ok, reason = E.pre_entry_checks(None, 12345, _sh_state, 186.0, {}, "", direction="CE")
    test("T13: Stop hunt recovery (CANDLE_SL + price>exit+5 + 1min) → allowed",
         ok == True, "ok=" + str(ok) + " reason=" + reason)


# ═══════════════════════════════════════════════════════════════
#  TEST 14-16: COOLDOWN — direction-aware
# ═══════════════════════════════════════════════════════════════

section("COOLDOWN")

_cd_state = {
    "last_exit_time": (datetime.now() - timedelta(minutes=3)).isoformat(),
    "last_exit_direction": "CE", "last_exit_peak": 15.0,
    "last_exit_reason": "TRAIL_FLOOR", "last_exit_price": 220.0,
    "in_trade": False, "daily_trades": 0, "daily_losses": 0, "paused": False,
}
with patch.object(D, 'is_entry_fire_window', return_value=True), \
     patch.object(D, 'is_market_open', return_value=True), \
     patch.object(D, 'is_tick_live', return_value=True):
    ok, _ = E.pre_entry_checks(None, 12345, _cd_state, 200.0, {}, "", direction="CE")
    test("T14: Same dir CE 3min after exit → blocked", ok == False)

with patch.object(D, 'is_entry_fire_window', return_value=True), \
     patch.object(D, 'is_market_open', return_value=True), \
     patch.object(D, 'is_tick_live', return_value=True):
    ok, _ = E.pre_entry_checks(None, 12345, deepcopy(_cd_state), 200.0, {}, "", direction="PE")
    test("T15: Opposite dir PE → allowed immediately", ok == True)

_cd_state2 = deepcopy(_cd_state)
_cd_state2["last_exit_time"] = (datetime.now() - timedelta(minutes=6)).isoformat()
with patch.object(D, 'is_entry_fire_window', return_value=True), \
     patch.object(D, 'is_market_open', return_value=True), \
     patch.object(D, 'is_tick_live', return_value=True):
    ok, _ = E.pre_entry_checks(None, 12345, _cd_state2, 200.0, {}, "", direction="CE")
    test("T16: Same dir 6min elapsed → cooldown expired → allowed", ok == True)


# ═══════════════════════════════════════════════════════════════
#  TEST 17-22: EXIT CHAIN (full priority order)
# ═══════════════════════════════════════════════════════════════

section("EXIT CHAIN — interdependent priority tests")

with patch.object(D, 'get_historical_data', return_value=MagicMock(empty=True)):

    # T17: EMERGENCY_SL at -20 (highest priority)
    st = _make_state(200, peak=0, candles=1)
    ex = E.manage_exit(st, 180, {})
    test("T17: Running -20 → EMERGENCY_SL",
         len(ex) == 1 and ex[0]["reason"] == "EMERGENCY_SL")

    # T18: Running -12 → CANDLE_SL (not emergency)
    st = _make_state(200, peak=0, candles=1)
    ex = E.manage_exit(st, 188, {})
    test("T18: Running -12 → CANDLE_SL",
         len(ex) == 1 and ex[0]["reason"] == "CANDLE_SL")

    # T19: Running -11 → no exit (above SL)
    st = _make_state(200, peak=0, candles=1)
    ex = E.manage_exit(st, 189, {})
    test("T19: Running -11 → no exit", len(ex) == 0)

    # T20: STALE_ENTRY — 5 candles, peak < 3
    st = _make_state(200, peak=2, candles=5)
    ex = E.manage_exit(st, 201, {})
    test("T20: 5 candles peak 2 → STALE_ENTRY",
         len(ex) == 1 and ex[0]["reason"] == "STALE_ENTRY")

    # T21: Peak +5 → floor SL tightens to entry-6 (194)
    st = _make_state(200, peak=5, candles=3)
    ex = E.manage_exit(st, 194, {})
    test("T21: Peak +5, price 194 → PROFIT_FLOOR",
         len(ex) == 1 and ex[0]["reason"] == "PROFIT_FLOOR")

    # T22: Peak +10 → floor SL at entry+2 (202), check persistence
    st = _make_state(200, peak=10, candles=5)
    st["phase1_sl"] = 188.0  # original SL
    ex = E.manage_exit(st, 210, {})  # above floor, no exit
    test("T22: Peak +10 ratchets phase1_sl to 202 (BUG-027)",
         st.get("phase1_sl", 0) >= 202 or st.get("_static_floor_sl", 0) >= 202,
         "phase1_sl=" + str(st.get("phase1_sl")))


# ═══════════════════════════════════════════════════════════════
#  TEST 23-24: PROFIT FLOOR ENDPOINTS
# ═══════════════════════════════════════════════════════════════

section("PROFIT FLOORS — full ladder")

with patch.object(D, 'get_historical_data', return_value=MagicMock(empty=True)):

    # T23: Peak +50 → floor at entry+42, exit when price drops there
    st = _make_state(200, peak=50, candles=20)
    ex = E.manage_exit(st, 241, {})
    test("T23: Peak +50, price 241 → exit (floor 242)",
         len(ex) == 1 and ex[0]["reason"] in ("PROFIT_FLOOR", "TRAIL_FLOOR"),
         "got " + str(ex))

    # T24: Peak 20, running +11 → PROFIT_FLOOR (floor at entry+12=212, price 211<212)
    st = _make_state(200, peak=20, candles=8)
    ex = E.manage_exit(st, 211, {})
    test("T24: Peak +20, price 211 < floor 212 → PROFIT_FLOOR",
         len(ex) == 1 and ex[0]["reason"] in ("PROFIT_FLOOR", "TRAIL_FLOOR"),
         "got " + str(ex))


# ═══════════════════════════════════════════════════════════════
#  TEST 25-26: CHARGES CALCULATOR (feeds into PNL)
# ═══════════════════════════════════════════════════════════════

section("CHARGES — integrated with trade PNL")

_c = CH.calculate_charges(150, 160, 130, num_exit_orders=1)
test("T25: Gross PNL 1300 + charges > 0 + net = gross - charges",
     _c["gross_pnl"] == 1300 and _c["total_charges"] > 0
     and _c["net_pnl"] == round(_c["gross_pnl"] - _c["total_charges"], 2),
     "gross=" + str(_c["gross_pnl"]) + " charges=" + str(_c["total_charges"]))

_c2 = CH.calculate_charges(150, 140, 130, 1)
test("T26: Loss trade net more negative than gross (charges compound loss)",
     _c2["net_pnl"] < _c2["gross_pnl"],
     "net=" + str(_c2["net_pnl"]) + " gross=" + str(_c2["gross_pnl"]))


# ═══════════════════════════════════════════════════════════════
#  TEST 27-28: DATABASE LIFECYCLE
# ═══════════════════════════════════════════════════════════════

section("DATABASE — insert → query → cleanup lifecycle")

import VRL_DB as DB
import tempfile, os as _os

_orig_db = DB.DB_PATH
_tmp_db = tempfile.mktemp(suffix=".db")
DB.DB_PATH = _tmp_db
DB._initialized = False
if hasattr(DB._local, "conn"):
    DB._local.conn = None
DB.init_db()

# T27: Insert trade → query → verify → cleanup preserves trades
DB.insert_trade({"date": "2026-04-14", "entry_time": "10:00", "exit_time": "10:05",
    "symbol": "NIFTY24050CE", "direction": "CE", "mode": "FAST",
    "entry_price": 200, "exit_price": 212, "pnl_pts": 12, "pnl_rs": 1560,
    "peak_pnl": 15, "trough_pnl": -2, "exit_reason": "PROFIT_FLOOR",
    "exit_phase": 1, "score": 0, "iv_at_entry": 18, "regime": "TRENDING",
    "dte": 3, "candles_held": 5, "session": "MORNING", "strike": 24050,
    "sl_pts": 12, "spread_1m": 3, "spread_3m": 0, "delta_at_entry": 0.45,
    "bias": "BULL", "vix_at_entry": 15, "hourly_rsi": 55, "straddle_decay": 0,
    "entry_mode": "FAST"})
trades = DB.get_trades("2026-04-14")
test("T27: Insert trade + query + verify entry_mode persists",
     len(trades) == 1 and trades[0]["pnl_pts"] == 12
     and trades[0].get("entry_mode") == "FAST",
     "trades=" + str(len(trades)))

# T28: Cleanup preserves trades
DB.cleanup_old_db_data(retention_days=0)
trades2 = DB.get_trades("2026-04-14")
test("T28: cleanup_old_db_data preserves trades table",
     len(trades2) == 1, "got " + str(len(trades2)))

try:
    DB.close()
    _os.unlink(_tmp_db)
except Exception:
    pass
DB.DB_PATH = _orig_db
DB._initialized = False
if hasattr(DB._local, "conn"):
    DB._local.conn = None


# ═══════════════════════════════════════════════════════════════
#  TEST 29-30: CODEBASE INTEGRITY (cross-file consistency)
# ═══════════════════════════════════════════════════════════════

section("CODEBASE INTEGRITY")

_repo = os.path.dirname(os.path.abspath(__file__))

def _read_file(name):
    with open(os.path.join(_repo, name)) as f:
        return f.read()

_eng_src = _read_file("VRL_ENGINE.py")
_main_src = _read_file("VRL_MAIN.py")
_cmd_src = _read_file("VRL_COMMANDS.py")
_dash_src = _read_file("static/VRL_DASHBOARD.html")

# T29: All critical v13.9 features present across codebase
test("T29: Cross-file v13.9 integrity",
     "breakout_confirmed" in _eng_src
     and "spot_slope" in _eng_src
     and "spot_flat" in _eng_src
     and "last_exit_price" in _main_src
     and "aggressive_mode" in _main_src
     and "_eod_exited" in _main_src
     and "Phantom trade" in _main_src
     and "_static_floor_sl" in _eng_src
     and "PROFIT_FLOOR" in _eng_src
     and "v13.9" in _dash_src,
     "missing critical feature in one or more files")

# T30: Banner, /help, dashboard all mention breakout + slope
test("T30: Strategy text alignment across banner/help/dashboard",
     "breakout" in _main_src.lower()
     and "slope" in _main_src.lower()
     and "breakout" in _cmd_src.lower()
     and "slope" in _cmd_src.lower()
     and "breakout" in _dash_src.lower()
     and "slope" in _dash_src.lower(),
     "strategy text not aligned across files")


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
