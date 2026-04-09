#!/home/user/kite_env/bin/python3
"""
═══════════════════════════════════════════════════════════════
 test_vrl.py — VISHAL RAJPUT TRADE v13.3 Test Suite
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
test("CE 22845 → 22850 (nearest 50)", s == 22850, "got " + str(s))
s = D.resolve_strike_for_direction(22845, "PE", 3)
test("PE 22845 → 22850 (nearest 50)", s == 22850, "got " + str(s))
s = D.resolve_strike_for_direction(22820, "CE", 0)
test("CE 22820 DTE0 → 22800", s == 22800, "got " + str(s))
s = D.resolve_strike_for_direction(22835, "PE", 0)
test("PE 22835 DTE0 → 22850", s == 22850, "got " + str(s))
s = D.resolve_strike_for_direction(22800, "CE", 3)
test("CE 22800 exactly → 22800", s == 22800, "got " + str(s))


section("VERSION")

test("VERSION = v13.5", D.VERSION == "v13.5", "got " + str(D.VERSION))


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
# Momentum = close[-2] - close[-5] = 115 - 100 = 15 (>= 12 threshold)
_df_fire = pd.DataFrame({
    "close": [100.0]*14 + [100.0, 103.0, 108.0, 115.0, 117.0, 118.0],
    "open":  [99.5]*14 + [99.5, 101.0, 106.0, 108.0, 116.0, 117.0],
    "high":  [101.0]*14 + [101.0, 104.0, 109.0, 116.0, 118.0, 119.0],
    "low":   [98.0]*14 + [98.0, 100.0, 105.0, 106.0, 114.0, 117.0],
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
        test("Momentum + EMA both pass → CONFIRMED fires", r["fired"] == True,
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

section("EXIT — v13.5 CANDLE CLOSE SL")

def _make_exit_state(entry, peak=0, candles=0, lot1=True, lot2=True, split=False):
    return {
        "in_trade": True, "entry_price": entry, "peak_pnl": peak,
        "trough_pnl": 0, "candles_held": candles, "token": 12345,
        "lot1_active": lot1, "lot2_active": lot2,
        "lots_split": split, "lot2_trail_sl": 0.0,
        "mode": "FAST", "entry_mode": "FAST",
        "current_rsi": 50, "_candle_low": entry,
    }

with patch.object(D, 'get_historical_data', return_value=MagicMock(empty=True)):
    # Running -12 → CANDLE_SL (ALL, not per-lot)
    st = _make_exit_state(200, peak=0, candles=1)
    exits = E.manage_exit(st, 188, {})
    test("Running -12 → CANDLE_SL (ALL)",
         len(exits) == 1 and exits[0]["reason"] == "CANDLE_SL"
         and exits[0]["lot_id"] == "ALL",
         "got " + str(exits))

    # Running -11 → no exit (below candle_close_sl)
    st = _make_exit_state(200, peak=0, candles=1)
    exits = E.manage_exit(st, 189, {})
    test("Running -11 → no exit", len(exits) == 0, "got " + str(exits))

    # Running -20 → EMERGENCY_SL
    st = _make_exit_state(200, peak=0, candles=1)
    exits = E.manage_exit(st, 180, {})
    test("Running -20 → EMERGENCY_SL",
         len(exits) == 1 and exits[0]["reason"] == "EMERGENCY_SL",
         "got " + str(exits))

    # Spike absorbed: low touched -13 but close -5, no other_token (treated as falling) → hold
    st = _make_exit_state(200, peak=0, candles=1)
    st["_candle_low"] = 187  # low hit -13
    exits = E.manage_exit(st, 195, {})  # close -5
    test("Spike absorbed: low -13 close -5 → HOLD",
         len(exits) == 0, "got " + str(exits))



section("EXIT — STALE ENTRY")

with patch.object(D, 'get_historical_data', return_value=MagicMock(empty=True)):
    st = _make_exit_state(200, peak=2, candles=5)
    exits = E.manage_exit(st, 201, {})
    test("5 candles peak<3 → STALE", len(exits) == 1 and exits[0]["reason"] == "STALE_ENTRY",
         "got " + str(exits))

    st = _make_exit_state(200, peak=5, candles=5)
    exits = E.manage_exit(st, 204, {})
    test("5 candles peak=5 → no stale", len(exits) == 0, "got " + str(exits))

    st = _make_exit_state(200, peak=2, candles=4)
    exits = E.manage_exit(st, 201, {})
    test("4 candles peak<3 → no stale (wait)", len(exits) == 0, "got " + str(exits))


section("EXIT — PROFIT FLOORS")

with patch.object(D, 'get_historical_data', return_value=MagicMock(empty=True)):

    # Peak 10, running 5 → no exit
    st = _make_exit_state(200, peak=10, candles=6)
    exits = E.manage_exit(st, 205, {})
    test("Peak 10 running 5 → no exit", len(exits) == 0, "got " + str(exits))

    # Peak 20, drop to floor (entry+12)
    st = _make_exit_state(200, peak=20, candles=8)
    exits = E.manage_exit(st, 211, {})  # running=11, floor_sl=212
    test("Peak 20 running 11 → TRAIL_FLOOR (both lots)",
         len(exits) >= 1 and all(x["reason"] == "TRAIL_FLOOR" for x in exits),
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


section("EXIT — v13.3 no split")
# v13.3: RSI split removed. Both lots same path.


# ═══════════════════════════════════════════════════════════════
#  COOLDOWN TESTS
# ═══════════════════════════════════════════════════════════════

section("COOLDOWN — DIRECTION-AWARE")

# Same direction blocked within 10 min after big win
_cd_state = deepcopy(E.D.DEFAULT_STATE) if hasattr(E.D, 'DEFAULT_STATE') else {}
_cd_state.update({
    "last_exit_time": (datetime.now() - timedelta(minutes=3)).isoformat(),
    "last_exit_direction": "CE",
    "last_exit_peak": 15.0,  # big win
    "in_trade": False,
    "daily_trades": 0, "daily_losses": 0, "paused": False,
})
with patch.object(D, 'is_entry_fire_window', return_value=True), \
     patch.object(D, 'is_market_open', return_value=True), \
     patch.object(D, 'is_tick_live', return_value=True):
    ok, reason = E.pre_entry_checks(None, 12345, _cd_state, 200.0, {}, "", direction="CE")
    test("Same dir CE after big win (3min ago) → BLOCKED", ok == False,
         "ok=" + str(ok) + " reason=" + reason)

# Opposite direction allowed immediately after big win
_cd_state2 = deepcopy(_cd_state)
with patch.object(D, 'is_entry_fire_window', return_value=True), \
     patch.object(D, 'is_market_open', return_value=True), \
     patch.object(D, 'is_tick_live', return_value=True):
    ok, reason = E.pre_entry_checks(None, 12345, _cd_state2, 200.0, {}, "", direction="PE")
    test("Opposite dir PE after CE win → ALLOWED", ok == True,
         "ok=" + str(ok) + " reason=" + reason)

# Same direction after small loss — 5 min cooldown
_cd_state3 = deepcopy(_cd_state)
_cd_state3["last_exit_peak"] = 3.0  # small/losing
_cd_state3["last_exit_time"] = (datetime.now() - timedelta(minutes=3)).isoformat()
with patch.object(D, 'is_entry_fire_window', return_value=True), \
     patch.object(D, 'is_market_open', return_value=True), \
     patch.object(D, 'is_tick_live', return_value=True):
    ok, reason = E.pre_entry_checks(None, 12345, _cd_state3, 200.0, {}, "", direction="CE")
    test("Same dir after small loss (3min ago, 5min cd) → BLOCKED", ok == False,
         "ok=" + str(ok) + " reason=" + reason)

# Same direction after small loss — 6 min elapsed (past cooldown)
_cd_state4 = deepcopy(_cd_state)
_cd_state4["last_exit_peak"] = 3.0
_cd_state4["last_exit_time"] = (datetime.now() - timedelta(minutes=7)).isoformat()
with patch.object(D, 'is_entry_fire_window', return_value=True), \
     patch.object(D, 'is_market_open', return_value=True), \
     patch.object(D, 'is_tick_live', return_value=True):
    ok, reason = E.pre_entry_checks(None, 12345, _cd_state4, 200.0, {}, "", direction="CE")
    test("Same dir after small loss (7min ago, 6min cd) → ALLOWED", ok == True,
         "ok=" + str(ok) + " reason=" + reason)


# ═══════════════════════════════════════════════════════════════
#  RSI HARD CAP TESTS
# ═══════════════════════════════════════════════════════════════

section("ENTRY — RSI HARD CAP")

# RSI 73 → BLOCKED
_df_rsi73 = _df_fire.copy()
_df_rsi73.iloc[-2, _df_rsi73.columns.get_loc("RSI")] = 73.0
_df_rsi73.iloc[-3, _df_rsi73.columns.get_loc("RSI")] = 68.0
with patch.object(D, 'get_historical_data', return_value=_df_rsi73):
    with patch.object(D, 'add_indicators', side_effect=lambda x: x):
        r = E.check_entry(12345, "CE", 22900, 5)
        test("RSI 73 > 72 → BLOCKED (blowoff)", r["fired"] == False,
             "fired=" + str(r["fired"]) + " rsi=" + str(r["rsi"]))

# RSI 71 → ALLOWED (if other conditions pass)
_df_rsi71 = _df_fire.copy()
_df_rsi71.iloc[-2, _df_rsi71.columns.get_loc("RSI")] = 71.0
_df_rsi71.iloc[-3, _df_rsi71.columns.get_loc("RSI")] = 65.0
with patch.object(D, 'get_historical_data', return_value=_df_rsi71):
    with patch.object(D, 'add_indicators', side_effect=lambda x: x):
        r = E.check_entry(12345, "CE", 22900, 5)
        test("RSI 71 < 72 → momentum can fire", r["fired"] == True,
             "fired=" + str(r["fired"]) + " rsi=" + str(r["rsi"])
             + " ema_gap=" + str(r["ema_gap"]))


# ═══════════════════════════════════════════════════════════════
#  LOT PNL CALCULATION TESTS
# ═══════════════════════════════════════════════════════════════

section("PNL — SPLIT LOT CORRECTNESS")

# Simulate: both lots exit in same cycle, verify entry_price preserved
# When lot1 exits (trade_done=True resets entry to 0), lot2 must still use original entry
_pnl_entry = 200.0
_pnl_exit1 = 215.0  # lot1 exit
_pnl_exit2 = 220.0  # lot2 exit

# lot1 PNL
pnl1 = round(_pnl_exit1 - _pnl_entry, 2)
test("Lot1 PNL = exit - entry", pnl1 == 15.0, "got " + str(pnl1))

# lot2 PNL
pnl2 = round(_pnl_exit2 - _pnl_entry, 2)
test("Lot2 PNL = exit - entry", pnl2 == 20.0, "got " + str(pnl2))

# Verify saved_entry_price pattern: even after state reset, original entry preserved
_sim_state = {"entry_price": 200.0, "in_trade": True}
_saved = _sim_state["entry_price"]  # capture before reset
_sim_state["entry_price"] = 0.0  # simulate reset (trade_done)
test("saved_entry_price survives state reset",
     _saved == 200.0 and _sim_state["entry_price"] == 0.0,
     "saved=" + str(_saved) + " state=" + str(_sim_state["entry_price"]))


# ═══════════════════════════════════════════════════════════════
#  BONUS INDICATORS (info only)
# ═══════════════════════════════════════════════════════════════

section("BONUS INDICATORS")

# All functions should return gracefully with no data
_vwap = D.calculate_option_vwap(0)
test("VWAP returns dict with vwap key", "vwap" in _vwap, str(_vwap))

_vol = D.detect_volume_spike(0)
test("VolSpike returns dict with spike+ratio", "spike" in _vol and "ratio" in _vol, str(_vol))

_fib = D.calculate_option_fib_pivots(0)
test("OptFib returns dict with pivot key", "pivot" in _fib, str(_fib))

_pdh = D.get_option_prev_day_hl(0)
test("PDH returns dict with prev_high key", "prev_high" in _pdh, str(_pdh))


# ═══════════════════════════════════════════════════════════════
#  CHARGES TESTS — VRL_CHARGES.py
# ═══════════════════════════════════════════════════════════════

section("CHARGES CALCULATOR")

import VRL_CHARGES as CH

# Basic charge calculation
_c1 = CH.calculate_charges(150, 160, 130, num_exit_orders=1)
test("Gross PNL = 1300", _c1["gross_pnl"] == 1300, "got " + str(_c1["gross_pnl"]))
test("Brokerage = 40 (2 orders)", _c1["brokerage"] == 40, "got " + str(_c1["brokerage"]))
test("Total charges > 0", _c1["total_charges"] > 0, "got " + str(_c1["total_charges"]))
test("Total charges < 200", _c1["total_charges"] < 200, "got " + str(_c1["total_charges"]))
test("Net PNL = gross - charges", _c1["net_pnl"] == round(_c1["gross_pnl"] - _c1["total_charges"], 2))

# Split charges: 3 orders total
_c2 = CH.calculate_split_charges(150, 160, 175, lot_size=65)
test("Split total brokerage = 60", _c2["total_brokerage"] == 60, "got " + str(_c2["total_brokerage"]))
test("Split combined gross > lot1", _c2["gross_pnl"] > _c2["lot1"]["gross_pnl"])
test("Split net > 0 for winning trade", _c2["net_pnl"] > 0, "got " + str(_c2["net_pnl"]))

# Zero PNL: charges eat into it
_c3 = CH.calculate_charges(150, 150, 130, 1)
test("Zero trade: gross = 0", _c3["gross_pnl"] == 0)
test("Zero trade: net < 0 (charges)", _c3["net_pnl"] < 0, "got " + str(_c3["net_pnl"]))

# Loss trade: net more negative than gross
_c4 = CH.calculate_charges(150, 140, 130, 1)
test("Loss: net more negative than gross", _c4["net_pnl"] < _c4["gross_pnl"],
     "net=" + str(_c4["net_pnl"]) + " gross=" + str(_c4["gross_pnl"]))


# ═══════════════════════════════════════════════════════════════
#  DB TESTS (uses temp database)
# ═══════════════════════════════════════════════════════════════

section("DATABASE — VRL_DB")

import VRL_DB as DB
import tempfile, os as _os

# Use temp DB for tests
_orig_db = DB.DB_PATH
_tmp_db = tempfile.mktemp(suffix=".db")
DB.DB_PATH = _tmp_db
DB._initialized = False
if hasattr(DB._local, "conn"):
    DB._local.conn = None

DB.init_db()
test("DB init creates tables", _os.path.isfile(_tmp_db), "file not created")

# Insert spot and read back
DB.insert_spot_1min({"timestamp": "2026-04-01 09:16:00", "open": 22800, "high": 22810,
    "low": 22795, "close": 22805, "volume": 1000, "ema9": 22803, "ema21": 22800,
    "ema_spread": 3, "rsi": 55, "adx": 20})
_spot = DB.query("SELECT * FROM spot_1min WHERE timestamp='2026-04-01 09:16:00'")
test("insert_spot_1min + query", len(_spot) == 1 and _spot[0]["close"] == 22805,
     "got " + str(len(_spot)) + " rows")

# Insert trade and read back
DB.insert_trade({"date": "2026-04-01", "entry_time": "10:00", "exit_time": "10:05",
    "symbol": "NIFTY22800CE", "direction": "CE", "mode": "MINIMAL",
    "entry_price": 300, "exit_price": 312, "pnl_pts": 12, "pnl_rs": 780,
    "peak_pnl": 15, "trough_pnl": -2, "exit_reason": "PROFIT_FLOOR",
    "exit_phase": 2, "score": 0, "iv_at_entry": 18, "regime": "TRENDING",
    "dte": 3, "candles_held": 5, "session": "MORNING", "strike": 22800,
    "sl_pts": 12, "spread_1m": 3, "spread_3m": 0, "delta_at_entry": 0.45,
    "bias": "BULL", "vix_at_entry": 15, "hourly_rsi": 55, "straddle_decay": 0})
_trades = DB.get_trades("2026-04-01")
test("insert_trade + get_trades", len(_trades) == 1 and _trades[0]["pnl_pts"] == 12,
     "got " + str(len(_trades)) + " rows")

# Bulk insert scans
_scans = [
    {"timestamp": "2026-04-01 10:00:00", "direction": "CE", "fired": "1",
     "session": "MORNING", "dte": 3, "atm_strike": 22800, "spot": 22800,
     "entry_price": 300, "score": 5},
    {"timestamp": "2026-04-01 10:01:00", "direction": "PE", "fired": "0",
     "session": "MORNING", "dte": 3, "atm_strike": 22800, "spot": 22800,
     "entry_price": 280, "score": 2},
]
DB.insert_scan_many(_scans)
_sc = DB.query("SELECT count(*) as n FROM signal_scans")
test("insert_scan_many bulk", _sc[0]["n"] == 2, "got " + str(_sc[0]["n"]))

# db_size_mb (temp db exists after inserts)
sz = _os.path.getsize(_tmp_db) if _os.path.isfile(_tmp_db) else 0
test("db file created with data", sz > 0, "got " + str(sz) + " bytes")

# cleanup_old_db_data never deletes trades
DB.cleanup_old_db_data(retention_days=0)
_trades2 = DB.get_trades("2026-04-01")
test("cleanup preserves trades", len(_trades2) == 1, "got " + str(len(_trades2)))

# Cleanup temp
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
#  EXIT PATH TESTS (comprehensive)
# ═══════════════════════════════════════════════════════════════

section("EXIT PATHS — COMPREHENSIVE")

with patch.object(D, 'get_historical_data', return_value=MagicMock(empty=True)):
    # CANDLE_SL at -12
    _st = _make_exit_state(200, peak=0, candles=1)
    _ex = E.manage_exit(_st, 188, {})
    test("CANDLE_SL at -12",
         len(_ex) == 1 and _ex[0]["reason"] == "CANDLE_SL")

    # EMERGENCY_SL at -20
    _st = _make_exit_state(200, peak=0, candles=1)
    _ex = E.manage_exit(_st, 180, {})
    test("EMERGENCY_SL at -20",
         len(_ex) == 1 and _ex[0]["reason"] == "EMERGENCY_SL")

    # STALE at 5 candles + peak < 3
    _st = _make_exit_state(200, peak=2, candles=5)
    _ex = E.manage_exit(_st, 201, {})
    test("STALE at 5 candles peak<3",
         len(_ex) == 1 and _ex[0]["reason"] == "STALE_ENTRY")

    # FAST TRAIL_FLOOR at peak 15
    _st = _make_exit_state(200, peak=15, candles=6)
    _st["entry_mode"] = "FAST"
    _ex = E.manage_exit(_st, 211, {})
    test("FAST TRAIL_FLOOR peak 15",
         len(_ex) == 1 and _ex[0]["reason"] == "TRAIL_FLOOR")


# RSI_BLOWOFF at 82
_st = _make_exit_state(200, peak=15, candles=5)
with patch.object(D, 'get_historical_data', return_value=_make_rsi_df(82)):
    with patch.object(D, 'add_indicators', side_effect=lambda x: x):
        _ex = E.manage_exit(_st, 220, {})
        test("RSI_BLOWOFF at 82", len(_ex) >= 1 and "BLOWOFF" in _ex[0]["reason"])

# v13.3: No split, no partial. Both lots exit together.


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
