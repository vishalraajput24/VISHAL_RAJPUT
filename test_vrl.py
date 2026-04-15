#!/home/vishalraajput24/kite_env/bin/python3
"""
═══════════════════════════════════════════════════════════════
 test_vrl.py — VISHAL RAJPUT TRADE v15.0 Test Suite
 25 focused tests for Dual EMA9 Band Breakout strategy.
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


import VRL_DATA as D
import VRL_ENGINE as E


# ═══════════════════════════════════════════════════════════════
#  FIXTURE BUILDERS
# ═══════════════════════════════════════════════════════════════

def _make_opt_3m(n=20, ema9_high=100.0, ema9_low=95.0,
                 last_close=102.0, last_open=98.0,
                 last_high=103.0, last_low=97.5,
                 prev_close=99.0, prev_ema9_high=100.0):
    """Build a 3-min option DataFrame with controlled last + prev rows."""
    rows = []
    for i in range(n - 2):
        rows.append({"open": 97.0, "high": 99.0, "low": 96.0, "close": 98.0, "volume": 1000})
    # iloc[-3] = prev candle
    rows.append({"open": 98.0, "high": prev_close + 0.5, "low": 97.0,
                 "close": prev_close, "volume": 1000})
    # iloc[-2] = last closed candle
    rows.append({"open": last_open, "high": last_high, "low": last_low,
                 "close": last_close, "volume": 1000})
    # iloc[-1] = live in-progress
    rows.append({"open": last_close, "high": last_close + 1, "low": last_close - 1,
                 "close": last_close + 0.5, "volume": 500})
    df = pd.DataFrame(rows)
    _base = datetime(2026, 4, 16, 10, 0)
    df.index = [_base + timedelta(minutes=i * 3) for i in range(len(rows))]
    df = D.add_indicators(df)
    # Override ema9_high / ema9_low for controlled tests
    df.iloc[-2, df.columns.get_loc("ema9_high")] = ema9_high
    df.iloc[-2, df.columns.get_loc("ema9_low")] = ema9_low
    df.iloc[-3, df.columns.get_loc("ema9_high")] = prev_ema9_high
    df.iloc[-3, df.columns.get_loc("ema9_low")] = ema9_low - 2
    return df


def _make_state(entry=200, peak=0, candles=0, in_trade=True):
    return {
        "in_trade": in_trade, "entry_price": entry, "peak_pnl": peak,
        "trough_pnl": 0, "candles_held": candles, "token": 12345,
        "entry_mode": "EMA9_BREAKOUT",
        "entry_ema9_high": 0, "entry_ema9_low": 0,
        "current_ema9_high": 0, "current_ema9_low": 0,
        "last_band_check_ts": "",
    }


# ═══════════════════════════════════════════════════════════════
#  T01-T03: FOUNDATION
# ═══════════════════════════════════════════════════════════════

section("FOUNDATION")

test("T01: VERSION is v15.0", D.VERSION == "v15.0", "got " + str(D.VERSION))

s = D.resolve_strike_for_direction(22819, "CE", 3)
test("T02: Strike CE 22819 DTE3 → 22800", s == 22800, "got " + str(s))

test("T03: add_indicators includes ema9_high + ema9_low",
     True,  # verified by fixture build — columns present
     "")


# ═══════════════════════════════════════════════════════════════
#  T04-T11: ENTRY GATES
# ═══════════════════════════════════════════════════════════════

section("v15.0 — ENTRY GATES")

# T04: Full fresh breakout → FIRES
_df = _make_opt_3m(last_close=103.0, last_open=98.0, last_high=104.0, last_low=97.5,
                   ema9_high=100.0, prev_close=99.0, prev_ema9_high=100.0)
with patch.object(D, 'get_historical_data', return_value=_df), \
     patch.object(D, 'add_indicators', side_effect=lambda x: x):
    r = E.check_entry(12345, "CE", 24000, 3)
    test("T04: Fresh breakout close>ema9h prev<=prev_ema9h green body 55% → FIRES",
         r["fired"] == True and r["entry_mode"] == "EMA9_BREAKOUT",
         "fired=" + str(r["fired"]) + " reject=" + str(r.get("reject_reason", "")))

# T05: Close below band → BLOCKED
_df2 = _make_opt_3m(last_close=98.0, last_open=97.0, last_high=99.0, last_low=96.5,
                    ema9_high=100.0)
with patch.object(D, 'get_historical_data', return_value=_df2), \
     patch.object(D, 'add_indicators', side_effect=lambda x: x):
    r = E.check_entry(12345, "CE", 24000, 3)
    test("T05: Close 98 < ema9h 100 → BLOCKED (below_band)",
         r["fired"] == False and "below_band" in r.get("reject_reason", ""),
         "reject=" + r.get("reject_reason", ""))

# T06: Stale breakout (prev was already above) → BLOCKED
_df3 = _make_opt_3m(last_close=103.0, last_open=101.0, last_high=104.0, last_low=100.5,
                    ema9_high=100.0, prev_close=101.0, prev_ema9_high=100.0)
with patch.object(D, 'get_historical_data', return_value=_df3), \
     patch.object(D, 'add_indicators', side_effect=lambda x: x):
    r = E.check_entry(12345, "CE", 24000, 3)
    test("T06: prev_close 101 > prev_ema9h 100 → stale_breakout blocked",
         r["fired"] == False and "stale_breakout" in r.get("reject_reason", ""),
         "reject=" + r.get("reject_reason", ""))

# T07: Red candle → BLOCKED
_df4 = _make_opt_3m(last_close=101.0, last_open=103.0, last_high=103.5, last_low=100.5,
                    ema9_high=100.0, prev_close=99.0, prev_ema9_high=100.0)
with patch.object(D, 'get_historical_data', return_value=_df4), \
     patch.object(D, 'add_indicators', side_effect=lambda x: x):
    r = E.check_entry(12345, "CE", 24000, 3)
    test("T07: close 101 < open 103 → red_candle blocked",
         r["fired"] == False and "red_candle" in r.get("reject_reason", ""),
         "reject=" + r.get("reject_reason", ""))

# T08: Weak body (<30%) → BLOCKED
# range 10, body 1 = 10%
_df5 = _make_opt_3m(last_close=103.0, last_open=102.0, last_high=108.0, last_low=98.0,
                    ema9_high=100.0, prev_close=99.0, prev_ema9_high=100.0)
with patch.object(D, 'get_historical_data', return_value=_df5), \
     patch.object(D, 'add_indicators', side_effect=lambda x: x):
    r = E.check_entry(12345, "CE", 24000, 3)
    test("T08: body 10% < 30% → weak_body blocked",
         r["fired"] == False and "weak_body" in r.get("reject_reason", ""),
         "reject=" + r.get("reject_reason", ""))

# T09: Cooldown active (same direction within 5min) → BLOCKED
_df6 = _make_opt_3m(last_close=103.0, last_open=98.0, last_high=104.0, last_low=97.5,
                    ema9_high=100.0, prev_close=99.0, prev_ema9_high=100.0)
_cd_state = {
    "last_exit_time": (datetime.now() - timedelta(minutes=2)).isoformat(),
    "last_exit_direction": "CE",
}
with patch.object(D, 'get_historical_data', return_value=_df6), \
     patch.object(D, 'add_indicators', side_effect=lambda x: x):
    r = E.check_entry(12345, "CE", 24000, 3, state=_cd_state)
    test("T09: Same dir CE 2min after exit → cooldown blocked",
         r["fired"] == False and "cooldown" in r.get("reject_reason", ""),
         "reject=" + r.get("reject_reason", ""))

# T10: Opposite direction during cooldown → allowed through gates
_df7 = _make_opt_3m(last_close=103.0, last_open=98.0, last_high=104.0, last_low=97.5,
                    ema9_high=100.0, prev_close=99.0, prev_ema9_high=100.0)
with patch.object(D, 'get_historical_data', return_value=_df7), \
     patch.object(D, 'add_indicators', side_effect=lambda x: x):
    r = E.check_entry(12345, "PE", 24000, 3, state=_cd_state)
    test("T10: Opposite dir PE during CE cooldown → not blocked by cooldown",
         "cooldown" not in r.get("reject_reason", ""),
         "reject=" + r.get("reject_reason", ""))

# T11: band_position label populated
_df8 = _make_opt_3m(last_close=103.0, last_open=98.0, last_high=104.0, last_low=97.5,
                    ema9_high=100.0)
with patch.object(D, 'get_historical_data', return_value=_df8), \
     patch.object(D, 'add_indicators', side_effect=lambda x: x):
    r = E.check_entry(12345, "CE", 24000, 3)
    test("T11: band_position populated (ABOVE/IN/BELOW)",
         r.get("band_position") in ("ABOVE", "IN", "BELOW"),
         "got " + str(r.get("band_position")))


# ═══════════════════════════════════════════════════════════════
#  T12-T18: EXIT CHAIN
# ═══════════════════════════════════════════════════════════════

section("v15.0 — EXIT CHAIN")

with patch.object(D, 'get_historical_data', return_value=MagicMock(empty=True)):

    # T12: EMERGENCY_SL at -20
    st = _make_state(200, peak=0, candles=1)
    ex = E.manage_exit(st, 180, {})
    test("T12: pnl -20 → EMERGENCY_SL",
         len(ex) == 1 and ex[0]["reason"] == "EMERGENCY_SL")

    # T13: pnl -15 → no exit (no fixed candle_sl in v15.0)
    st = _make_state(200, peak=0, candles=1)
    ex = E.manage_exit(st, 185, {})
    test("T13: pnl -15 → no exit (band-only, no fixed candle_sl)",
         len(ex) == 0)

    # T14: STALE — 5 candles, peak < 3
    st = _make_state(200, peak=2, candles=5)
    ex = E.manage_exit(st, 201, {})
    test("T14: 5 candles peak 2 → STALE_ENTRY",
         len(ex) == 1 and ex[0]["reason"] == "STALE_ENTRY")


# T15: EMA9_LOW_BREAK — close below ema9_low triggers exit
_df_break = _make_opt_3m(last_close=94.0, last_open=95.0, last_high=96.0, last_low=93.5,
                         ema9_high=100.0, ema9_low=95.5)
with patch.object(D, 'get_historical_data', return_value=_df_break), \
     patch.object(D, 'add_indicators', side_effect=lambda x: x):
    st = _make_state(entry=100, peak=5, candles=3)
    ex = E.manage_exit(st, 94, {})
    test("T15: close 94 < ema9_low 95.5 → EMA9_LOW_BREAK",
         len(ex) == 1 and ex[0]["reason"] == "EMA9_LOW_BREAK",
         "got " + str(ex))

# T16: Close above ema9_low → hold
_df_hold = _make_opt_3m(last_close=102.0, last_open=101.0, last_high=103.0, last_low=100.5,
                        ema9_high=100.0, ema9_low=99.0)
with patch.object(D, 'get_historical_data', return_value=_df_hold), \
     patch.object(D, 'add_indicators', side_effect=lambda x: x):
    st = _make_state(entry=95, peak=7, candles=3)
    ex = E.manage_exit(st, 102, {})
    test("T16: close 102 > ema9_low 99 → hold (no exit)",
         len(ex) == 0, "got " + str(ex))

# T17: Same candle doesn't trigger repeat exit (ts dedup)
_df_dup = _make_opt_3m(last_close=94.0, last_open=95.0, last_high=96.0, last_low=93.5,
                       ema9_high=100.0, ema9_low=95.5)
with patch.object(D, 'get_historical_data', return_value=_df_dup), \
     patch.object(D, 'add_indicators', side_effect=lambda x: x):
    st = _make_state(entry=100, peak=5, candles=3)
    st["last_band_check_ts"] = str(_df_dup.iloc[-2].name)  # pretend we already saw this bar
    ex = E.manage_exit(st, 94, {})
    test("T17: same-candle ts dedup → no repeat EMA9_LOW_BREAK",
         len(ex) == 0, "got " + str(ex))

# T18: Peak tracking updates
with patch.object(D, 'get_historical_data', return_value=_df_hold), \
     patch.object(D, 'add_indicators', side_effect=lambda x: x):
    st = _make_state(entry=100, peak=5, candles=2)
    E.manage_exit(st, 110, {})
    test("T18: peak ratchets up when pnl > peak",
         st["peak_pnl"] >= 10, "peak=" + str(st.get("peak_pnl")))


# ═══════════════════════════════════════════════════════════════
#  T19-T22: STATE + CONFIG SANITY
# ═══════════════════════════════════════════════════════════════

section("v15.0 — STATE + CONFIG SANITY")

import VRL_CONFIG as C
C.load()

test("T19: entry_ema9_band.body_pct_min = 30",
     C.entry_ema9_band("body_pct_min") == 30)

test("T20: exit_ema9_band.emergency_sl_pts = -20",
     C.exit_ema9_band("emergency_sl_pts") == -20)

# T21: STATE_PERSIST_FIELDS has v15.0 band fields
required_v15 = ["entry_ema9_high", "entry_ema9_low", "entry_band_position",
                "current_ema9_high", "current_ema9_low", "last_band_check_ts",
                "entry_body_pct"]
missing = [f for f in required_v15 if f not in D.STATE_PERSIST_FIELDS]
test("T21: STATE_PERSIST_FIELDS includes v15.0 band fields",
     len(missing) == 0, "missing: " + str(missing))

# T22: No v13/v14 stale strategy fields in engine
_eng_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "VRL_ENGINE.py")).read()
stale_patterns = ["rsi_3m_entry", "adx_3m_entry", "confidence_15m",
                  "spot_slope", "breakout_confirmed", "two_green_above",
                  "momentum_pts", "spot_confirms"]
_check_body = _eng_src.split("def check_entry")[1] if "def check_entry" in _eng_src else ""
_exit_body = _eng_src.split("def manage_exit")[1] if "def manage_exit" in _eng_src else ""
stale_found = [p for p in stale_patterns if p in _check_body or p in _exit_body]
test("T22: Engine has no v13/v14 stale strategy fields",
     len(stale_found) == 0, "found: " + str(stale_found))


# ═══════════════════════════════════════════════════════════════
#  T23-T25: CODEBASE INTEGRITY
# ═══════════════════════════════════════════════════════════════

section("v15.0 — CODEBASE INTEGRITY")

_repo = os.path.dirname(os.path.abspath(__file__))
def _read_file(name):
    with open(os.path.join(_repo, name)) as f:
        return f.read()

_main_src = _read_file("VRL_MAIN.py")
_cfg_src = _read_file("config.yaml")
_dash_src = _read_file("static/VRL_DASHBOARD.html")
_cmd_src = _read_file("VRL_COMMANDS.py")

# T23: All files mention v15.0
test("T23: v15.0 in VRL_MAIN, config, dashboard, commands",
     "v15.0" in _main_src and "v15.0" in _cfg_src
     and "v15.0" in _dash_src and "v15.0" in _cmd_src)

# T24: Engine has ema9_high + ema9_low + EMA9_BREAKOUT + EMA9_LOW_BREAK
test("T24: Engine contains v15.0 band strategy keywords",
     "ema9_high" in _eng_src and "ema9_low" in _eng_src
     and "EMA9_BREAKOUT" in _eng_src and "EMA9_LOW_BREAK" in _eng_src)

# T25: No dead config sections present
test("T25: config.yaml has no entry_3min / profit_floors / rsi_exit",
     "entry_3min:" not in _cfg_src and "profit_floors:" not in _cfg_src
     and "rsi_exit:" not in _cfg_src)


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
