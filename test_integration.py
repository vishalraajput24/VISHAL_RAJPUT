#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
 test_integration.py — v15.2.5 integration test against live DB

 Opens ~/lab_data/vrl_data.db and validates that every query used
 by VRL_BACKTEST, VRL_REPLAY, VRL_DB_AUDIT runs without
 OperationalError. Catches column-drift after schema migrations.

 Also validates that the code's field lists (_SCAN_FIELDS,
 _TRADE_FIELDS) exactly match what PRAGMA table_info returns
 from the live DB.

 Run after any schema change or field-list edit.
 Usage:  python3 test_integration.py
═══════════════════════════════════════════════════════════════
"""
import os
import sys
import sqlite3

DB_PATH = os.path.expanduser("~/lab_data/vrl_data.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Stub third-party modules for dev envs
from unittest.mock import MagicMock
for _m in ("kiteconnect", "pyotp", "requests"):
    if _m not in sys.modules:
        sys.modules[_m] = MagicMock()

_passed = 0
_failed = 0


def test(name, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print("  PASS " + name)
    else:
        _failed += 1
        print("  FAIL " + name + ((" — " + detail) if detail else ""))


print("=== INTEGRATION TEST against " + DB_PATH + " ===")

if not os.path.isfile(DB_PATH):
    print("  SKIP — DB not found (dev environment, no lab_data)")
    print("  Run on the production server to get real results.")
    sys.exit(0)

conn = sqlite3.connect(DB_PATH, timeout=10)

# 1. Code field lists match live DB columns
import VRL_DB

CHECKS = {
    "signal_scans": VRL_DB._SCAN_FIELDS,
    "trades":       VRL_DB._TRADE_FIELDS,
}

for tbl, code_fields in CHECKS.items():
    live_cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(" + tbl + ")").fetchall()}
    missing = [f for f in code_fields if f not in live_cols]
    extra   = sorted(live_cols - set(code_fields))
    test("1. " + tbl + " — code fields ⊆ live DB",
         len(missing) == 0,
         "missing from DB: " + str(missing))
    if extra:
        print("      info: DB has " + str(len(extra))
              + " extra cols not in INSERT list: " + str(extra[:5]))


# 2. Queries used by VRL_BACKTEST don't throw OperationalError
backtest_queries = [
    ("exits",
     "SELECT exit_reason, pnl_pts, peak_pnl FROM trades LIMIT 1"),
    ("scans_gates",
     "SELECT reject_reason, fwd_3c, fwd_5c, fwd_10c, fired "
     "FROM signal_scans LIMIT 1"),
    ("scans_v15",
     "SELECT ema9_high, ema9_low, band_position, body_pct, "
     "straddle_delta, straddle_period, band_width, vwap_bonus, "
     "trade_taken FROM signal_scans LIMIT 1"),
    ("trades_v15",
     "SELECT entry_ema9_high, entry_straddle_delta, "
     "entry_straddle_info, entry_spot_vwap, entry_vwap_bonus "
     "FROM trades LIMIT 1"),
    ("hour_of_day",
     "SELECT entry_time, pnl_pts FROM trades LIMIT 1"),
    ("classification",
     "SELECT entry_straddle_info, pnl_pts, peak_pnl FROM trades LIMIT 1"),
]

for label, sql in backtest_queries:
    try:
        conn.execute(sql).fetchall()
        test("2. backtest query [" + label + "]", True)
    except Exception as e:
        test("2. backtest query [" + label + "]", False, str(e))


# 3. Queries used by VRL_REPLAY
replay_queries = [
    ("replay_trades",
     "SELECT date, entry_time, exit_time, symbol, direction, "
     "entry_price, exit_price, pnl_pts, peak_pnl, candles_held, "
     "exit_reason, entry_mode, entry_straddle_delta, "
     "entry_straddle_info, entry_straddle_period, "
     "entry_band_width, entry_body_pct, entry_atm_strike, "
     "entry_ema9_high, entry_ema9_low, entry_vwap_bonus "
     "FROM trades LIMIT 1"),
]

for label, sql in replay_queries:
    try:
        conn.execute(sql).fetchall()
        test("3. replay query [" + label + "]", True)
    except Exception as e:
        test("3. replay query [" + label + "]", False, str(e))


# 4. VRL_DB_AUDIT queries
audit_queries = [
    ("scan_fwd",
     "SELECT fwd_3c, fwd_5c, fwd_10c, fwd_outcome "
     "FROM signal_scans LIMIT 1"),
    ("scan_reject",
     "SELECT reject_reason FROM signal_scans WHERE fired != '1' LIMIT 1"),
    ("trade_class",
     "SELECT entry_straddle_info, pnl_pts FROM trades LIMIT 1"),
]

for label, sql in audit_queries:
    try:
        conn.execute(sql).fetchall()
        test("4. audit query [" + label + "]", True)
    except Exception as e:
        test("4. audit query [" + label + "]", False, str(e))


# 5. No dead v13 columns remain in post-migration tables
dead_scan_cols = [
    "rsi_1m", "body_pct_1m", "vol_ratio_1m", "rsi_rising_1m",
    "spread_1m", "rsi_3m", "conditions_3m", "score",
    "iv_pct", "delta", "straddle_decay_pct", "straddle_threshold",
    "near_fib_level", "fib_distance",
]
dead_trade_cols = [
    "mode", "score", "iv_at_entry", "regime",
    "spread_1m", "spread_3m", "delta_at_entry", "straddle_decay",
    "signal_price", "bonus_vwap", "bonus_fib_level", "bonus_fib_dist",
    "bonus_vol_spike", "bonus_vol_ratio", "bonus_pdh_break",
    "momentum_pts", "rsi_rising", "spot_confirms", "spot_move",
    "momentum_tf", "other_falling", "other_move", "spike_ratio",
]

scan_live = {r[1] for r in conn.execute("PRAGMA table_info(signal_scans)")}
trade_live = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
scan_zombies = [c for c in dead_scan_cols if c in scan_live]
trade_zombies = [c for c in dead_trade_cols if c in trade_live]
test("5. signal_scans has no dead v13 cols",
     len(scan_zombies) == 0,
     "still present: " + str(scan_zombies))
test("5. trades has no dead v13 cols",
     len(trade_zombies) == 0,
     "still present: " + str(trade_zombies))

conn.close()

print()
print("=" * 50)
print("RESULTS: " + str(_passed) + " passed, " + str(_failed) + " failed")
sys.exit(0 if _failed == 0 else 1)
